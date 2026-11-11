#include "orgsta_coda.h"

#ifdef USE_ESP32

#include <esp_heap_caps.h>
#include <esp_gatt_common_api.h>
#include <algorithm>
#include <cstring>
#include <cstdarg>

#include "esphome/core/application.h"
#include "esphome/core/alloc_helpers.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

// l'inflate di miniz sta nella ROM dei chip ESP32 (tinfl_decompress): niente codice in più nel firmware
#include "miniz.h"

namespace esphome::orgsta_coda {

static const char *const TAG = "orgsta_coda";

// la stampante (protocol.py dell'integrazione: stesse costanti)
static const uint16_t SERVIZIO = 0xFF00;
static const uint16_t CAR_NOTIFICA = 0xFF01;
static const uint16_t CAR_SCRIVI = 0xFF02;
static const uint16_t CAR_FLUSSO = 0xFF03;
static const uint8_t INIZIO = 0x64;
static const uint8_t FINE = 0x9B;
static const size_t RIGA = 12;              // 96 punti
static const size_t RIGHE_PEZZO = 24;       // come l'app originale (11 novembre): 24 righe per comando
static const size_t PEZZO_MAX = 192;        // come l'app (e «02 c0 00» della stampantina)
static const uint32_t PAUSA_MS = 20;        // CHUNK_DELAY
static const int16_t IN_VOLO_MAX = 2;       // 0.9.3: come l'app, mai più di 2 pacchetti senza il «01 01»
static const uint32_t PAUSA_PERMESSO_MS = 25;
static const uint32_t COLLEGA_MS = 25000;   // per trovarla e collegarsi
static const uint32_t STATO_MS = 6000;      // STATUS_TIMEOUT
static const uint32_t STAMPA_MS = 40000;    // COMPLETION_TIMEOUT
static const uint32_t RISPOSTA_MS = 8000;   // una scrittura senza risposta
static const uint32_t ATTESA_MS = 120000;   // le altre etichette dello stesso lavoro (col Wi-Fi che riparte)
static const uint8_t TENTATIVI = 3;
static const uint32_t DOPO_CONSEGNA_MS = 700;  // la risposta dell'ultima consegna parte prima di spegnere il Wi-Fi
static const uint32_t ESCLUSIVA_MS = 60000;    // al massimo col Wi-Fi spento, più ESCLUSIVA_PAGINA_MS per etichetta
static const uint32_t ESCLUSIVA_PAGINA_MS = 45000;

static const char *nome_fase(Fase f) {
  switch (f) {
    case Fase::LIBERA:
      return "libera";
    case Fase::COLLEGO:
      return "collego";
    case Fase::STATO:
      return "stato";
    case Fase::SCRIVO:
      return "stampa";
    case Fase::ASPETTO:
      return "stampa";
    case Fase::ATTESA:
      return "attesa";
    case Fase::FATTO:
      return "fatto";
    case Fase::ERRORE:
      return "errore";
  }
  return "?";
}

static uint32_t memoria_libera() { return heap_caps_get_free_size(MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL); }

void OrgstaCoda::setup() {
  // 0.9.1: senza questo ESP-IDF resta a mtu 23 e ogni pacchetto è di 20 byte (l'app ne manda 192)
  esp_ble_gatt_set_local_mtu(247);
  // il collegamento lo apre solo questa coda, quando ha qualcosa da stampare
  this->parent()->set_auto_connect(false);
}

void OrgstaCoda::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Coda della stampantina ORGSTA S001:\n"
                "  Stampante: %s\n"
                "  Memoria per la coda: %u KB, sempre libera: %u KB",
                this->parent()->address_str(), (unsigned) (this->memoria_max_ / 1024),
                (unsigned) (this->libera_min_ / 1024));
}

size_t OrgstaCoda::in_coda_byte_() const {
  size_t n = 0;
  for (const auto &v : this->coda_)
    n += v.z.size();
  return n;
}

bool OrgstaCoda::ultima_pagina_() const {
  if (this->coda_.empty())
    return true;
  const Voce &v = this->coda_.front();
  return v.indice == this->totale_ - 1 && this->copia_ >= v.copie - 1;
}

void OrgstaCoda::vai_(Fase f) {
  if (f != this->fase_)
    ESP_LOGD(TAG, "%s -> %s", nome_fase(this->fase_), nome_fase(f));
  this->fase_ = f;
  this->t_fase_ = millis();
  // 0.8.1: mentre manda la pagina il giro di ESPHome non aspetta i suoi ~16 ms tra un pezzo e l'altro (con tante
  // scritture piccole le pause si sommano e la testina resta senza righe)
  if (f == Fase::STATO || f == Fase::SCRIVO)
    this->veloce_.start();
  else
    this->veloce_.stop();
}

void OrgstaCoda::scollega_() {
  this->parent()->set_auto_connect(false);
  this->pronta_ = false;
  auto st = this->parent()->state();
  if (st != espbt::ClientState::IDLE && st != espbt::ClientState::INIT)
    this->parent()->disconnect();
}

bool OrgstaCoda::pronta_a_partire_() const {
  if (this->coda_.empty())
    return false;
  if (!this->esclusiva_ || this->in_esclusiva_)
    return true;
  // tutte le etichette del lavoro (o la coda piena) e l'ultima risposta ad HA già partita
  if (this->ricevute_ < this->totale_ && !this->piena_)
    return false;
  return millis() - this->t_ultima_metti_ > DOPO_CONSEGNA_MS;
}

void OrgstaCoda::radio_tutta_mia_() {
  if (!this->esclusiva_ || this->in_esclusiva_)
    return;
  this->in_esclusiva_ = true;
  this->t_esclusiva_ = millis();
  ESP_LOGI(TAG, "radio tutta per la stampantina: Wi-Fi spento fino alla fine della stampa (%d etichette)",
           (int) this->coda_.size());
#ifdef USE_WIFI
  if (wifi::global_wifi_component != nullptr)
    wifi::global_wifi_component->disable();
#endif
}

void OrgstaCoda::radio_restituita_() {
  if (!this->in_esclusiva_)
    return;
  this->in_esclusiva_ = false;
  ESP_LOGI(TAG, "stampa finita in %u ms: riaccendo Wi-Fi e ricerca", (unsigned) (millis() - this->t_esclusiva_));
  if (this->scan_fermo_ && esp32_ble_tracker::global_esp32_ble_tracker != nullptr) {
    this->scan_fermo_ = false;
    esp32_ble_tracker::global_esp32_ble_tracker->set_scan_continuous(true);
    esp32_ble_tracker::global_esp32_ble_tracker->start_scan_if_idle();
  }
#ifdef USE_WIFI
  if (wifi::global_wifi_component != nullptr)
    wifi::global_wifi_component->enable();
#endif
}

void OrgstaCoda::errore_(const char *perche) {
  this->nota_("errore %s (off %u di %u)", perche, (unsigned) this->off_, (unsigned) this->pagina_.size());
  ESP_LOGW(TAG, "lavoro %s fermo: %s (%d/%d etichette, %d pagine)", this->lavoro_.c_str(), perche,
           (int) this->voci_fatte_, (int) this->totale_, (int) this->pagine_fatte_);
  this->errore_txt_ = perche;
  this->coda_.clear();
  this->pagina_.clear();
  this->pagina_.shrink_to_fit();
  this->scollega_();
  this->vai_(Fase::ERRORE);
  this->radio_restituita_();
}

// ---- le azioni ----

void OrgstaCoda::metti(StringRef lavoro, int32_t indice, int32_t totale, int32_t copie, int32_t modo,
                       int32_t densita, int32_t velocita, int32_t inizio, int32_t righe, int32_t resta,
                       StringRef dati, JsonObject root) {
  std::string id(lavoro.c_str(), lavoro.size());
  auto rifiuta = [&](const char *perche) {
    root["ok"] = false;
    root["perche"] = perche;
    this->stato(root);
  };
  if (id.empty() || totale < 1 || indice < 0 || indice >= totale || righe < 1 || righe > 4000) {
    rifiuta("dati");
    return;
  }
  bool occupata = this->fase_ != Fase::LIBERA && this->fase_ != Fase::FATTO && this->fase_ != Fase::ERRORE;
  if (id != this->lavoro_) {
    if (occupata) {
      rifiuta("occupata");
      return;
    }
    // un lavoro nuovo: la connessione di prima (FATTO) si tiene, il resto si azzera
    this->lavoro_ = id;
    this->coda_.clear();
    this->totale_ = totale;
    this->ricevute_ = 0;
    this->voci_fatte_ = 0;
    this->pagine_fatte_ = 0;
    this->copia_ = 0;
    this->tentativi_ = 0;
    this->errore_txt_.clear();
    this->piena_ = false;
    if (this->fase_ == Fase::ERRORE)
      this->vai_(Fase::LIBERA);
  } else if (this->fase_ == Fase::ERRORE) {
    rifiuta("errore");
    return;
  }
  if (indice < this->ricevute_) {  // HA l'ha mandata due volte (risposta persa): c'è già
    root["ok"] = true;
    this->stato(root);
    return;
  }
  if (indice > this->ricevute_) {
    rifiuta("ordine");
    return;
  }
  // base64 → zlib, senza copie in più: la stringa sta nel buffer dell'API
  size_t massimo = dati.size() * 3 / 4 + 3;
  if (this->in_coda_byte_() + massimo > this->memoria_max_ || memoria_libera() < this->libera_min_ + massimo + 2048) {
    this->piena_ = true;
    rifiuta("piena");
    return;
  }
  Voce v;
  v.z.resize(massimo);
  size_t n = base64_decode(reinterpret_cast<const uint8_t *>(dati.c_str()), dati.size(), v.z.data(), v.z.size());
  if (n < 2) {
    rifiuta("dati");
    return;
  }
  v.z.resize(n);
  v.z.shrink_to_fit();
  v.indice = indice;
  v.copie = std::clamp<int32_t>(copie, 1, 50);
  v.modo = (uint8_t) std::clamp<int32_t>(modo, 0, 2);
  v.densita = (uint8_t) std::clamp<int32_t>(densita, 0, 255);
  v.velocita = (uint8_t) std::clamp<int32_t>(velocita, 0, 255);
  v.inizio = (uint8_t) std::clamp<int32_t>(inizio, 0, 120);
  v.righe = (uint16_t) righe;
  this->coda_.push_back(std::move(v));
  this->ricevute_ = indice + 1;
  this->totale_ = totale;
  this->resta_ms_ = (uint32_t) std::clamp<int32_t>(resta, 0, 120) * 1000;
  this->t_ultima_metti_ = millis();
  root["ok"] = true;
  this->stato(root);
}

void OrgstaCoda::nota_(const char *fmt, ...) {
  char b[96];
  int n = snprintf(b, sizeof(b), "%u ", (unsigned) millis());
  va_list a;
  va_start(a, fmt);
  vsnprintf(b + n, sizeof(b) - n, fmt, a);
  va_end(a);
  ESP_LOGI(TAG, "%s", b);
  this->diario_.emplace_back(b);
  while (this->diario_.size() > 48)
    this->diario_.pop_front();
}

void OrgstaCoda::stato(JsonObject root) {
  root["fase"] = nome_fase(this->fase_);
  root["lavoro"] = this->lavoro_;
  root["totale"] = this->totale_;
  root["ricevute"] = this->ricevute_;
  root["fatte"] = this->voci_fatte_;
  root["pagine"] = this->pagine_fatte_;
  root["in_coda"] = (uint32_t) this->coda_.size();
  root["byte"] = (uint32_t) this->in_coda_byte_();
  root["posto"] = (uint32_t) (this->memoria_max_ > this->in_coda_byte_() ? this->memoria_max_ - this->in_coda_byte_() : 0);
  root["libera"] = memoria_libera();
  root["collegata"] = this->collegata_();
  root["errore"] = this->errore_txt_;
  // l'ultima pagina mandata (0.8.1): per capire se la stampante resta senza dati a metà
  root["mtu"] = this->mtu_;
  root["ms_invio"] = this->ms_invio_;
  root["pausa_max"] = this->pausa_max_;
  root["pezzi"] = this->pezzi_;
  root["byte_pagina"] = this->byte_pagina_;
  root["rifatte"] = this->rifatte_;
  root["esclusiva"] = this->esclusiva_;
  JsonArray d = root["diario"].to<JsonArray>();
  for (auto &s : this->diario_)
    d.add(s);
  if (!this->st_.empty()) {
    root["st_cmd"] = this->st_cmd_;
    root["st"] = format_hex(this->st_);
  }
}

void OrgstaCoda::annulla(StringRef lavoro) {
  std::string id(lavoro.c_str(), lavoro.size());
  if (!id.empty() && id != this->lavoro_)
    return;
  if (this->fase_ == Fase::LIBERA || this->fase_ == Fase::ERRORE)
    return;
  this->errore_("annullata");
}

// ---- la stampante ----

void OrgstaCoda::frame_(uint8_t cmd, const uint8_t *payload, uint16_t n) {
  auto &p = this->pagina_;
  p.push_back(INIZIO);
  p.push_back(cmd);
  p.push_back(this->seq_);
  this->seq_ = (this->seq_ + 1) % 64;
  p.push_back(n & 0xFF);
  p.push_back(n >> 8);
  if (n)
    p.insert(p.end(), payload, payload + n);
  p.insert(p.end(), 4, 0);  // la stampante accetta integrità 0
  p.push_back(FINE);
}

void OrgstaCoda::manda_stato_() {
  this->pagina_.clear();
  this->frame_(0x10, nullptr, 0);
  this->off_ = 0;
  this->nuovo_stato_ = false;
}

// la pagina della prima etichetta in coda, uguale a JobBuilder.build_page dell'integrazione
void OrgstaCoda::prepara_pagina_() {
  const Voce &v = this->coda_.front();
  const size_t serve = (size_t) v.righe * RIGA;
  std::vector<uint8_t> raster(serve);
  auto *dec = new tinfl_decompressor;  // ~11 KB: sullo stack del loop non ci sta
  tinfl_init(dec);
  size_t in_n = v.z.size(), out_n = serve;
  tinfl_status s = tinfl_decompress(dec, v.z.data(), &in_n, raster.data(), raster.data(), &out_n,
                                    TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
  delete dec;
  if (s != TINFL_STATUS_DONE || out_n != serve) {
    ESP_LOGW(TAG, "etichetta %d non si apre (tinfl %d, %u di %u byte)", (int) v.indice, (int) s, (unsigned) out_n,
             (unsigned) serve);
    this->errore_("dati");
    return;
  }
  const bool primo = this->primo_;
  const bool ultima = this->ultima_pagina_();
  const bool tag = v.modo != 0;
  this->seq_ = 0;
  this->pagina_.clear();
  this->pagina_.reserve(serve + serve / (RIGA * RIGHE_PEZZO) * 10 + 80);
  if (primo) {
    uint8_t b = v.velocita;
    this->frame_(0x0A, &b, 1);
    b = v.densita;
    this->frame_(0x09, &b, 1);
    uint8_t carta[2] = {0x01, v.modo};
    this->frame_(0x28, carta, 2);
    if (tag) {
      uint8_t sp[3] = {2, 800 & 0xFF, 800 >> 8};
      this->frame_(0x03, sp, 3);
    } else {
      uint8_t ind[2] = {12, 0};
      this->frame_(0x04, ind, 2);
    }
  }
  if (tag && v.inizio > 0) {
    uint8_t av[2] = {v.inizio, 0};
    this->frame_(0x02, av, 2);
  }
  const size_t passo = RIGA * RIGHE_PEZZO;
  for (size_t o = 0; o < serve; o += passo)
    this->frame_(0x00, raster.data() + o, (uint16_t) std::min(passo, serve - o));
  if (tag) {
    uint8_t sp[3] = {(uint8_t) (ultima ? 1 : 0), 800 & 0xFF, 800 >> 8};
    this->frame_(0x03, sp, 3);
  } else {
    uint8_t av[2] = {52, 0};
    this->frame_(0x02, av, 2);
    if (ultima) {
      uint8_t av2[2] = {12, 0};
      this->frame_(0x02, av2, 2);
    }
  }
  this->off_ = 0;
  this->aspetto_risposta_ = false;
  this->vista_stampa_ = false;
  this->t_pagina_ = millis();
  this->ms_invio_ = 0;
  this->pausa_max_ = 0;
  this->pezzi_ = 0;
  this->byte_pagina_ = this->pagina_.size();
}

bool OrgstaCoda::scrivi_(const uint8_t *dati, size_t n) {
  // 0.9.1: col permesso di ff03 si scrive come l'app, senza aspettare la risposta: è il permesso a fare da freno
  const bool col_permesso = this->credito_ >= 0;
  auto ret = esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(), this->h_scrivi_,
                                      n, const_cast<uint8_t *>(dati),
                                      col_permesso ? ESP_GATT_WRITE_TYPE_NO_RSP : ESP_GATT_WRITE_TYPE_RSP,
                                      ESP_GATT_AUTH_REQ_NONE);
  if (ret != ESP_OK) {
    this->nota_("scrittura rifiutata %d (%u byte, credito %d)", ret, (unsigned) n, (int) this->credito_);
    return false;
  }
  const uint32_t ora = millis();
  if (this->pezzi_ > 0 && ora - this->t_scritto_ > this->pausa_max_)
    this->pausa_max_ = ora - this->t_scritto_;
  this->pezzi_++;
  this->t_scritto_ = ora;
  if (col_permesso) {
    this->credito_--;
    this->in_volo_++;
    this->off_ += n;           // niente risposta da aspettare
    this->aspetto_risposta_ = false;
  } else {
    this->aspetto_risposta_ = true;
  }
  return true;
}

void OrgstaCoda::ricomincia_pagina_() {
  // collegamento caduto o scrittura fallita: la pagina si rifà da capo, mai mezza
  this->tentativi_++;
  if (this->tentativi_ > TENTATIVI) {
    this->errore_("write");
    return;
  }
  this->nota_("pagina %d da rifare (tentativo %d, off %u)", (int) this->pagine_fatte_ + 1, (int) this->tentativi_,
              (unsigned) this->off_);
  this->rifatte_++;
  this->pagina_.clear();
  this->aspetto_risposta_ = false;
  this->primo_ = true;
  this->scollega_();
  this->vai_(Fase::COLLEGO);
  this->parent()->set_auto_connect(true);
}

void OrgstaCoda::pagina_fatta_() {
  this->pagine_fatte_++;
  this->tentativi_ = 0;
  this->copia_++;
  if (!this->coda_.empty() && this->copia_ >= this->coda_.front().copie) {
    this->coda_.pop_front();
    this->voci_fatte_++;
    this->copia_ = 0;
  }
  this->pagina_.clear();
  if (!this->coda_.empty()) {
    this->prepara_pagina_();
    if (this->fase_ != Fase::ERRORE)
      this->vai_(Fase::SCRIVO);
  } else if (this->voci_fatte_ >= this->totale_) {
    ESP_LOGI(TAG, "lavoro %s finito: %d etichette, %d pagine", this->lavoro_.c_str(), (int) this->voci_fatte_,
             (int) this->pagine_fatte_);
    this->pagina_.shrink_to_fit();
    this->vai_(Fase::FATTO);
    if (this->in_esclusiva_) {   // finito e confermato dalla stampante: si stacca e si riaccende tutto
      this->scollega_();
      this->primo_ = true;
      this->radio_restituita_();
    }
  } else {
    this->vai_(Fase::ATTESA);
    if (this->in_esclusiva_) {   // la coda era piena: HA manda le altre col Wi-Fi riacceso
      this->scollega_();
      this->primo_ = true;
      this->piena_ = false;
      this->radio_restituita_();
    }
  }
}

void OrgstaCoda::leggi_notifica_(const uint8_t *dati, size_t n) {
  auto &b = this->rx_;
  b.insert(b.end(), dati, dati + n);
  while (!b.empty()) {
    auto it = std::find(b.begin(), b.end(), INIZIO);
    if (it == b.end()) {
      b.clear();
      break;
    }
    b.erase(b.begin(), it);
    if (b.size() < 5)
      break;
    size_t plen = b[3] | (b[4] << 8);
    size_t tot = plen + 10;
    if (tot > 512) {
      b.erase(b.begin());
      continue;
    }
    if (b.size() < tot)
      break;
    if (b[tot - 1] != FINE) {
      b.erase(b.begin());
      continue;
    }
    uint8_t cmd = b[1];
    const uint8_t *p = b.data() + 5;
    bool buono = (cmd == 0x80 || cmd == 0x81) ? plen == 8 : (cmd == 0xFF ? (plen == 7 || plen == 8 || plen == 12) : false);
    if (buono) {
      this->st_cmd_ = cmd;
      this->st_.assign(p, p + plen);
      this->bits_ = p[2] | (p[3] << 8);
      this->nuovo_stato_ = true;
      if (this->bits_ & (1 << 3))
        this->vista_stampa_ = true;
    }
    b.erase(b.begin(), b.begin() + tot);
  }
  if (b.size() > 1024)
    b.clear();
}

void OrgstaCoda::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                     esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_CFG_MTU_EVT:
      if (param->cfg_mtu.status == ESP_GATT_OK)
        this->mtu_ = param->cfg_mtu.mtu;
      this->nota_("mtu %u (stato %d)", (unsigned) param->cfg_mtu.mtu, (int) param->cfg_mtu.status);
      break;
    case ESP_GATTC_OPEN_EVT:
      // 0.8.1: pezzi più grandi = meno pause, la testina non si ferma a metà (con mtu 23 un'etichetta da 4 cm sono più
      // di 200 scritture). Se lo chiede già ESPHome la risposta arriva lo stesso in CFG_MTU
      if (param->open.status == ESP_GATT_OK && !this->mtu_chiesto_) {
        this->mtu_chiesto_ = true;
        esp_ble_gattc_send_mtu_req(gattc_if, param->open.conn_id);
      }
      break;
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto *n = this->parent()->get_characteristic(SERVIZIO, CAR_NOTIFICA);
      auto *w = this->parent()->get_characteristic(SERVIZIO, CAR_SCRIVI);
      auto *f = this->parent()->get_characteristic(SERVIZIO, CAR_FLUSSO);
      if (n == nullptr || w == nullptr) {
        ESP_LOGW(TAG, "non è una S001: niente servizio ff00");
        this->node_state = espbt::ClientState::ESTABLISHED;
        this->errore_("servizio");
        break;
      }
      this->h_notifica_ = n->handle;
      this->h_scrivi_ = w->handle;
      this->h_flusso_ = f != nullptr ? f->handle : 0;
      this->parent()->register_for_notify(this->h_notifica_);
      if (this->h_flusso_)
        this->parent()->register_for_notify(this->h_flusso_);
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (param->reg_for_notify.handle == this->h_notifica_) {
        this->pronta_ = param->reg_for_notify.status == ESP_GATT_OK;
        this->node_state = espbt::ClientState::ESTABLISHED;
        this->nota_("collegata (mtu %u)", (unsigned) this->mtu_);
      }
      break;
    case ESP_GATTC_NOTIFY_EVT:
      if (param->notify.handle == this->h_notifica_) {
        this->leggi_notifica_(param->notify.value, param->notify.value_len);
      } else if (this->h_flusso_ && param->notify.handle == this->h_flusso_ && param->notify.value_len >= 2) {
        const uint8_t *v = param->notify.value;
        this->nota_("ff03 %s (credito %d, off %u)", format_hex(v, param->notify.value_len).c_str(),
                    (int) this->credito_, (unsigned) this->off_);
        if (v[0] == 0x01) {
          // «01 07» quando il buffer è vuoto (si riparte da lì), «01 01» per ogni pacchetto digerito
          if (v[1] > 1 || this->credito_ < 0) {
            this->credito_ = v[1];
            this->in_volo_ = 0;
          } else {
            this->credito_ = std::min<int16_t>(this->credito_ + v[1], 16);
            this->in_volo_ = std::max<int16_t>(0, this->in_volo_ - v[1]);
          }
          this->t_credito_ = millis();
        } else if (v[0] == 0x02 && param->notify.value_len >= 3) {
          uint16_t m = v[1] | (v[2] << 8);
          if (m >= 20)
            this->pezzo_max_ = m;
        }
      }
      break;
    case ESP_GATTC_WRITE_CHAR_EVT:
      if (param->write.handle != this->h_scrivi_ || !this->aspetto_risposta_)
        break;
      this->aspetto_risposta_ = false;
      if (param->write.status != ESP_GATT_OK) {
        this->nota_("pezzo non preso (%d)", param->write.status);
        if (this->fase_ == Fase::SCRIVO || this->fase_ == Fase::STATO)
          this->ricomincia_pagina_();
        break;
      }
      this->off_ += this->ultimo_pezzo_;
      break;
    case ESP_GATTC_DISCONNECT_EVT:
    case ESP_GATTC_CLOSE_EVT: {
      if (event == ESP_GATTC_DISCONNECT_EVT)
        this->nota_("staccata: motivo 0x%x, credito %d, %u/%u byte", (unsigned) param->disconnect.reason,
                    (int) this->credito_, (unsigned) this->off_, (unsigned) this->pagina_.size());
      bool era = this->pronta_;
      this->pronta_ = false;
      this->credito_ = -1;
      this->in_volo_ = 0;
      this->aspetto_risposta_ = false;
      this->mtu_ = 23;
      this->mtu_chiesto_ = false;
      this->rx_.clear();
      if (era)
        ESP_LOGI(TAG, "scollegata dalla stampante");
      if (this->fase_ == Fase::STATO || this->fase_ == Fase::SCRIVO || this->fase_ == Fase::ASPETTO)
        this->ricomincia_pagina_();
      else if (this->fase_ == Fase::FATTO || this->fase_ == Fase::ATTESA)
        this->primo_ = true;
      break;
    }
    default:
      break;
  }
}

void OrgstaCoda::loop() {
  const uint32_t ora = millis();
  const uint32_t in_fase = ora - this->t_fase_;
  if (this->in_esclusiva_) {
    // collegata: anche la ricerca si ferma, la radio parla solo con la stampantina
    if (!this->scan_fermo_ && this->collegata_() && esp32_ble_tracker::global_esp32_ble_tracker != nullptr) {
      this->scan_fermo_ = true;
      esp32_ble_tracker::global_esp32_ble_tracker->stop_scan();
    }
    const uint32_t massimo = ESCLUSIVA_MS + ESCLUSIVA_PAGINA_MS * (uint32_t) std::max<int32_t>(1, this->totale_);
    if (ora - this->t_esclusiva_ > massimo) {
      this->errore_("tempo");
      return;
    }
  }
  switch (this->fase_) {
    case Fase::LIBERA:
    case Fase::FATTO:
      if (!this->coda_.empty() && !this->pronta_a_partire_()) {
        break;  // aspetta tutte le etichette del lavoro
      }
      if (!this->coda_.empty()) {
        this->radio_tutta_mia_();
        if (this->collegata_() && !this->primo_) {
          this->prepara_pagina_();
          if (this->fase_ != Fase::ERRORE)
            this->vai_(Fase::SCRIVO);
        } else if (this->collegata_()) {
          this->manda_stato_();
          this->vai_(Fase::STATO);
        } else {
          this->primo_ = true;
          this->vai_(Fase::COLLEGO);
          this->parent()->set_auto_connect(true);
        }
      } else if (this->fase_ == Fase::FATTO && in_fase > this->resta_ms_ && this->pronta_) {
        this->scollega_();
        this->primo_ = true;
      }
      break;

    case Fase::ATTESA:
      if (!this->coda_.empty() && this->pronta_a_partire_()) {
        this->radio_tutta_mia_();
        if (this->collegata_()) {
          this->prepara_pagina_();
          if (this->fase_ != Fase::ERRORE)
            this->vai_(Fase::SCRIVO);
        } else {
          this->primo_ = true;
          this->vai_(Fase::COLLEGO);
          this->parent()->set_auto_connect(true);
        }
      } else if (in_fase > ATTESA_MS) {
        this->errore_("attesa");
      }
      break;

    case Fase::COLLEGO:
      if (this->collegata_()) {
        this->manda_stato_();
        this->vai_(Fase::STATO);
      } else if (in_fase > COLLEGA_MS) {
        this->errore_("connect");
      }
      break;

    case Fase::STATO:
      if (!this->collegata_())
        break;
      if (this->off_ < this->pagina_.size()) {
        if (this->credito_ == 0)
          break;
        if (!this->aspetto_risposta_) {
          this->ultimo_pezzo_ = this->pagina_.size() - this->off_;
          if (!this->scrivi_(this->pagina_.data() + this->off_, this->ultimo_pezzo_))
            this->ricomincia_pagina_();
        } else if (ora - this->t_scritto_ > RISPOSTA_MS) {
          this->ricomincia_pagina_();
        }
        break;
      }
      if (this->h_flusso_ && this->credito_ < 0 && in_fase < STATO_MS + 4000)
        break;  // 0.9.1: il permesso arriva qualche secondo dopo il collegamento, si stampa solo con quello
      if (this->nuovo_stato_ || in_fase > STATO_MS) {
        const uint16_t b = this->bits_;
        if (b & (1 << 8)) {
          this->errore_("cover_open");
        } else if (b & (1 << 11)) {
          this->errore_("paper_out");
        } else if (b & (1 << 10)) {
          this->errore_("overheating");
        } else {
          this->primo_ = true;
          this->prepara_pagina_();
          if (this->fase_ != Fase::ERRORE)
            this->vai_(Fase::SCRIVO);
        }
      }
      break;

    case Fase::SCRIVO: {
      if (!this->collegata_())
        break;  // l'evento di scollegamento ricomincia la pagina
      if (this->aspetto_risposta_) {
        if (ora - this->t_scritto_ > RISPOSTA_MS)
          this->ricomincia_pagina_();
        break;
      }
      if (this->off_ >= this->pagina_.size()) {
        this->ms_invio_ = ora - this->t_pagina_;
        this->nota_("pagina mandata: %u byte, %u pezzi, %u ms, pausa max %u, mtu %u", (unsigned) this->byte_pagina_,
                    (unsigned) this->pezzi_, (unsigned) this->ms_invio_, (unsigned) this->pausa_max_,
                    (unsigned) this->mtu_);
        this->primo_ = false;
        this->vista_stampa_ = false;
        this->nuovo_stato_ = false;
        this->vai_(Fase::ASPETTO);
        break;
      }
      if (this->credito_ > 0 && (this->in_volo_ >= IN_VOLO_MAX || ora - this->t_scritto_ < PAUSA_PERMESSO_MS)) {
        if (ora - std::max(this->t_scritto_, this->t_credito_) > STAMPA_MS)
          this->ricomincia_pagina_();
        break;
      }
      if (this->credito_ == 0) {
        // la stampantina ha il buffer pieno: si aspetta il suo «01 ..» (anche secondi, mentre stampa)
        if (ora - std::max(this->t_scritto_, this->t_credito_) > STAMPA_MS)
          this->ricomincia_pagina_();
        break;
      }
      if (this->credito_ < 0 && ora - this->t_scritto_ < PAUSA_MS)
        break;
      size_t pezzo = std::max<size_t>(20, std::min<size_t>(std::min<size_t>(PEZZO_MAX, this->pezzo_max_),
                                                           this->mtu_ > 3 ? this->mtu_ - 3 : 20));
      this->ultimo_pezzo_ = std::min(pezzo, this->pagina_.size() - this->off_);
      if (!this->scrivi_(this->pagina_.data() + this->off_, this->ultimo_pezzo_))
        this->ricomincia_pagina_();
      break;
    }

    case Fase::ASPETTO: {
      const uint16_t b = this->bits_;
      const bool stampa = b & (1 << 3);
      if (this->vista_stampa_ && this->nuovo_stato_ && !stampa) {
        if (b & (1 << 8)) {
          this->errore_("cover_open");
        } else if (b & (1 << 11)) {
          this->errore_("paper_out");
        } else {
          this->pagina_fatta_();
        }
      } else if (in_fase > STAMPA_MS) {
        if (b & (1 << 8)) {
          this->errore_("cover_open");
        } else if (b & (1 << 11)) {
          this->errore_("paper_out");
        } else if (b & (1 << 10)) {
          this->errore_("overheating");
        } else {
          ESP_LOGD(TAG, "nessun «finito» in %us: la do per stampata", (unsigned) (STAMPA_MS / 1000));
          this->pagina_fatta_();
        }
      }
      this->nuovo_stato_ = false;
      break;
    }

    case Fase::ERRORE:
      break;
  }
}

}  // namespace esphome::orgsta_coda

#endif  // USE_ESP32
