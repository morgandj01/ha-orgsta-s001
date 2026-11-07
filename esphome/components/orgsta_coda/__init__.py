"""La coda della stampantina ORGSTA S001 dentro un proxy Bluetooth ESPHome.

HA consegna qui le etichette intere (raster compresso con zlib) con l'azione `orgsta_metti`; il proxy le tiene in
memoria e le stampa da solo, collegato alla stampantina col suo `ble_client`, una pagina alla volta e con la risposta
di ogni pezzo. Se il collegamento cade a metà, quella pagina si rifà da capo. Vedi README.md del repository.
"""

import esphome.codegen as cg
from esphome.components import ble_client
import esphome.config_validation as cv
from esphome.const import CONF_ID

AUTO_LOAD = ["json"]
DEPENDENCIES = ["ble_client", "api"]
CODEOWNERS = ["@morgandj01"]

CONF_MEMORIA_KB = "memoria_kb"
CONF_LIBERA_KB = "libera_kb"

orgsta_ns = cg.esphome_ns.namespace("orgsta_coda")
OrgstaCoda = orgsta_ns.class_("OrgstaCoda", cg.Component, ble_client.BLEClientNode)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(OrgstaCoda),
            # quanta memoria al massimo per le etichette in coda (compresse: una 12x40 col QR è circa 0,7 KB)
            cv.Optional(CONF_MEMORIA_KB, default=64): cv.int_range(min=8, max=512),
            # quanta memoria deve restare sempre libera per il resto (Wi-Fi, Bluetooth, API, luci)
            cv.Optional(CONF_LIBERA_KB, default=36): cv.int_range(min=16, max=256),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(ble_client.BLE_CLIENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
    cg.add(var.set_memoria(config[CONF_MEMORIA_KB] * 1024, config[CONF_LIBERA_KB] * 1024))
