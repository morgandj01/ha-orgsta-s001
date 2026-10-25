"""BLE session with the ORGSTA S001. Connects on demand, prints, disconnects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import CHAR_FLOW, CHAR_NOTIFY, CHAR_WRITE
from .protocol import FrameDecoder, JobBuilder, PaperDetails, Status, parse_paper_details, parse_status

_LOGGER = logging.getLogger(__name__)

STATUS_TIMEOUT = 6.0
PAPER_TIMEOUT = 3.0
COMPLETION_TIMEOUT = 40.0
MAX_CHUNK = 128
CHUNK_DELAY = 0.02


class PrinterError(Exception):
    """Printer reported a problem or could not be reached."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key


class S001Session:
    """One connection: status query, optional job, completion wait."""

    def __init__(
        self,
        ble_device: BLEDevice,
        on_status: Callable[[Status], None],
    ) -> None:
        self._device = ble_device
        self._on_status = on_status
        self._decoder = FrameDecoder()
        self._client: BleakClientWithServiceCache | None = None
        self._status_event = asyncio.Event()
        self._last: Status | None = None
        self._saw_printing = False
        self._done = asyncio.Event()
        self._paper_event = asyncio.Event()
        self.paper: PaperDetails | None = None

    # ---- notifications ----
    def _notify(self, _char, data: bytearray) -> None:
        for frame in self._decoder.feed(bytes(data)):
            if (pd := parse_paper_details(frame)) is not None:
                self.paper = pd
                self._paper_event.set()
                continue
            st = parse_status(frame)
            if st is None:
                continue
            self._last = st
            self._status_event.set()
            if st.printing:
                self._saw_printing = True
            elif self._saw_printing:
                self._done.set()
            try:
                self._on_status(st)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("status callback failed")

    async def __aenter__(self) -> "S001Session":
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self._device.name or "S001",
                max_attempts=3,
            )
            await self._client.start_notify(CHAR_NOTIFY, self._notify)
            try:
                await self._client.start_notify(CHAR_FLOW, lambda *_: None)
            except BleakError:
                pass
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            await self._close()
            raise PrinterError("connect", str(err)) from err
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    @property
    def mtu_chunk(self) -> int:
        mtu = getattr(self._client, "mtu_size", 23) or 23
        return max(20, min(MAX_CHUNK, mtu - 3))

    async def _write(self, data: bytes) -> None:
        assert self._client is not None
        size = self.mtu_chunk
        for off in range(0, len(data), size):
            await self._client.write_gatt_char(CHAR_WRITE, data[off : off + size], response=True)
            await asyncio.sleep(CHUNK_DELAY)

    async def query_status(self, builder: JobBuilder) -> Status | None:
        self._status_event.clear()
        await self._write(builder.status_query())
        try:
            await asyncio.wait_for(self._status_event.wait(), STATUS_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            return self._last
        return self._last

    async def query_paper(self, builder: JobBuilder) -> PaperDetails | None:
        """Il tag del rotolo, solo se la stampante dice che è originale. Mai un errore: senza risposta None."""
        if self._last is None or not self._last.valid_paper:
            return None
        self._paper_event.clear()
        try:
            await self._write(builder.paper_query())
            await asyncio.wait_for(self._paper_event.wait(), PAPER_TIMEOUT)
        except (BleakError, TimeoutError, asyncio.TimeoutError):
            return None
        return self.paper

    async def send_job(self, frames: list[bytes], wait: bool = True) -> None:
        self._saw_printing = False
        self._done.clear()
        try:
            await self._write(b"".join(frames))
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            raise PrinterError("write", str(err)) from err
        if not wait:
            return
        try:
            await asyncio.wait_for(self._done.wait(), COMPLETION_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            if self._last is not None and self._last.errors:
                raise PrinterError(self._last.errors[0])
            _LOGGER.debug("no completion status within %ss, assuming printed", COMPLETION_TIMEOUT)
        if self._last is not None and (self._last.cover_open or self._last.paper_out):
            raise PrinterError(self._last.errors[0])


def check_ready(status: Status | None) -> None:
    if status is None:
        return
    if status.cover_open:
        raise PrinterError("cover_open")
    if status.paper_out:
        raise PrinterError("paper_out")
    if status.overheating:
        raise PrinterError("overheating")
