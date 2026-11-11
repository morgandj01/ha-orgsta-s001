#pragma once

#ifdef USE_ESP32

#include <deque>
#include <string>
#include <vector>

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/json/json_util.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/string_ref.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#ifdef USE_WIFI
#include "esphome/components/wifi/wifi_component.h"
#endif

namespace esphome::orgsta_coda {

namespace espbt = esphome::esp32_ble_tracker;

// Un'etichetta in coda: il raster (righe da 12 byte, come lo fa l'integrazione) compresso con zlib, e quante copie
struct Voce {
  int32_t indice{0};
  int32_t copie{1};
  uint8_t modo{1};  // 0 carta continua, 1 etichette col buco, 2 etichette nere (come PaperMode)
  uint8_t densita{9};
  uint8_t velocita{25};
  uint8_t inizio{24};
  uint16_t righe{0};
  std::vector<uint8_t> z;
};

enum class Fase : uint8_t {
  LIBERA,   // niente da fare, scollegata
  COLLEGO,  // aspetto che il ble_client si colleghi e trovi la stampante
  STATO,    // chiesto lo stato prima della prima pagina
  SCRIVO,   // mando i pezzi della pagina, uno alla volta con la risposta
  ASPETTO,  // pagina mandata: aspetto che la stampante dica «ho finito»
  ATTESA,   // stampato tutto quello che c'è, ne aspetto altre dello stesso lavoro
  FATTO,    // lavoro finito: resto collegata ancora un po' (resta_ms_) per il prossimo
  ERRORE,
};

class OrgstaCoda : public Component, public ble_client::BLEClientNode {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }

  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

  void set_esclusiva(bool e) { this->esclusiva_ = e; }
  void set_memoria(uint32_t massimo, uint32_t libera) {
    this->memoria_max_ = massimo;
    this->libera_min_ = libera;
  }

  // le azioni dell'API (stampantina.yaml)
  void metti(StringRef lavoro, int32_t indice, int32_t totale, int32_t copie, int32_t modo, int32_t densita,
             int32_t velocita, int32_t inizio, int32_t righe, int32_t resta, StringRef dati, JsonObject root);
  void stato(JsonObject root);
  void annulla(StringRef lavoro);

 protected:
  void vai_(Fase f);
  void errore_(const char *perche);
  void ricomincia_pagina_();
  void prepara_pagina_();
  bool scrivi_(const uint8_t *dati, size_t n);
  void manda_stato_();
  void leggi_notifica_(const uint8_t *dati, size_t n);
  void pagina_fatta_();
  void scollega_();
  bool collegata_() const { return this->pronta_ && this->node_state == espbt::ClientState::ESTABLISHED; }
  size_t in_coda_byte_() const;
  bool ultima_pagina_() const;
  void frame_(uint8_t cmd, const uint8_t *payload, uint16_t n);
  // 0.9.0 (Morgan, 11 novembre: «deve prendersi tutta l'antenna bluetooth per lui»): mentre stampa il proxy spegne il
  // Wi-Fi e la ricerca Bluetooth, così la radio è tutta per la stampantina; finito (o fermo) riaccende tutto
  bool pronta_a_partire_() const;
  void radio_tutta_mia_();
  void radio_restituita_();
  bool esclusiva_{true};
  bool in_esclusiva_{false};
  bool scan_fermo_{false};
  bool piena_{false};
  uint32_t t_esclusiva_{0};
  uint32_t t_ultima_metti_{0};

  // il lavoro
  std::string lavoro_;
  std::deque<Voce> coda_;
  int32_t totale_{0};
  int32_t ricevute_{0};
  int32_t voci_fatte_{0};
  int32_t pagine_fatte_{0};
  int32_t copia_{0};
  bool primo_{true};
  uint32_t resta_ms_{8000};
  std::string errore_txt_;
  uint8_t tentativi_{0};

  // la stampante
  uint16_t h_notifica_{0};
  uint16_t h_scrivi_{0};
  uint16_t h_flusso_{0};
  uint16_t mtu_{23};
  bool pronta_{false};

  // la pagina che si sta mandando
  std::vector<uint8_t> pagina_;
  size_t off_{0};
  size_t ultimo_pezzo_{0};
  bool aspetto_risposta_{false};
  uint32_t t_scritto_{0};
  uint8_t seq_{0};

  // 0.8.1 (11 novembre, «stampa a pezzi»): come è andata l'ultima pagina, per le prove. Se tra un pezzo e l'altro passa
  // troppo la testina si ferma e riparte: è lì che l'etichetta esce a pezzi
  uint32_t t_pagina_{0};
  uint32_t ms_invio_{0};
  uint32_t pausa_max_{0};
  uint32_t pezzi_{0};
  uint32_t byte_pagina_{0};
  uint32_t rifatte_{0};
  bool mtu_chiesto_{false};
  // 0.9.1 (11 novembre, la spia sull'app originale): su ff03 la stampantina dice quanti pacchetti può ancora prendere
  // («01 07» libera, «01 01» uno in più dopo ogni pacchetto) e quanto può essere grande un pacchetto («02 c0 00» = 192).
  // L'app aspetta sempre il permesso; senza, il buffer trabocca: pezzi, doppioni e inizio rovinato
  int16_t credito_{-1};   // -1 = non ancora detto
  uint16_t pezzo_max_{192};
  uint32_t t_credito_{0};
  HighFrequencyLoopRequester veloce_;

  // lo stato della stampante (l'ultima risposta 0x80/0x81/0xFF)
  std::vector<uint8_t> rx_;
  uint8_t st_cmd_{0};
  std::vector<uint8_t> st_;
  uint16_t bits_{0};
  bool nuovo_stato_{false};
  bool vista_stampa_{false};

  Fase fase_{Fase::LIBERA};
  uint32_t t_fase_{0};
  uint32_t memoria_max_{64 * 1024};
  uint32_t libera_min_{36 * 1024};
};

}  // namespace esphome::orgsta_coda

#endif  // USE_ESP32
