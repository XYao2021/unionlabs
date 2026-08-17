/*
 * lora_phy.ino — PHY layer for SX1276 LoRa module  (Teensy 4.0 + RFM95)
 *
 * Responsibilities (PHY-only, no MAC logic here):
 *   - Configure radio (SF, CR, BW, frequency, TX power) on demand
 *   - Transmit a raw frame supplied by the bridge
 *   - Receive frames and report (payload, SNR, RSSI, CRC ok/fail)
 *   - Report PER over a sliding window per request
 *
 * Serial protocol (line-based ASCII, 115200 baud) — bridge <-> Teensy:
 *
 *   bridge -> teensy:
 *     CFG SF=<7..12> CR=<5..8> P=<2..20> BW=<125000|250000|500000> FQ=<Hz>
 *     TX  ID=<u32> LEN=<n> HEX=<2n hex chars>
 *     RXON
 *     RXOFF
 *     STAT                                   -- request PER stats and reset window
 *     PING
 *     RESET                                  -- re-init the radio (SX1276 hard reset)
 *     REBOOT                                 -- restart the MCU (full setup() again)
 *
 *   teensy -> bridge:
 *     OK CFG
 *     OK TX  ID=<u32> TOA_MS=<int>           -- airtime of the frame just sent
 *     RX  LEN=<n> HEX=<...> SNR=<float> RSSI=<int> CRC=<OK|FAIL>
 *     STAT TX=<n> ACK=<n> RXOK=<n> RXBAD=<n> REINIT=<n> DEAF_MS=<n>
 *     PONG
 *     OK RESET
 *     ERR <reason>
 *
 * Uses Sandeep Mistry's LoRa library (https://github.com/sandeepmistry/arduino-LoRa).
 *
 * ============================================================================
 * 2026-08-13 REWRITE — why the old sketch went deaf (the "must reboot the Pi"
 * problem, and very likely the inactive-lane probes failing at PER=100%).
 * ============================================================================
 *
 * (1) THE RX STATE MACHINE WAS INCONSISTENT — this was the real bug.
 *     The old sketch mixed the library's two mutually exclusive RX models:
 *     it armed CONTINUOUS RX with LoRa.receive() and then polled with
 *     LoRa.parsePacket(). Inside the library, parsePacket() does:
 *
 *         if (a packet was received)      { ...; idle(); }   // -> STANDBY
 *         else if (opmode != RX_SINGLE)   { opmode = RX_SINGLE; }
 *
 *     So the first poll DESTROYS continuous mode (forces single-RX), and after
 *     every successfully received packet the radio is left in STANDBY. The old
 *     poll_rx() returned without re-arming, so the only things that ever put
 *     the radio back into a listening state were RXON, CFG and the tail of
 *     handle_tx(). A node that TRANSMITS keeps re-arming itself and looks
 *     healthy; a node that only RECEIVES goes deaf after one packet and stays
 *     deaf until the next reboot. That is exactly the TX->RX->TX role-switch
 *     failure: serial responsive, TX fine, RX dead, reboot cures it.
 *
 *     FIX: pick ONE model and stick to it. This sketch uses the library's
 *     POLLING contract only: parsePacket() is the sole RX entry point and it
 *     re-arms single-RX on every call, so the deaf window is one loop
 *     iteration instead of forever. LoRa.receive() is never called. (Polling
 *     was chosen over the DIO0/onReceive interrupt path deliberately: it does
 *     not depend on DIO0 being wired on every board, which we cannot verify
 *     remotely. If DIO0 is confirmed on all 8 nodes, the ISR path in
 *     handleDio0Rise() keeps the radio in RX_CONTINUOUS and is strictly
 *     better — see NOTE at the bottom.)
 *
 * (2) THE LOOP COULD STALL FOR A FULL SECOND.
 *     Serial.readStringUntil('\n') blocks until a newline OR the 1000 ms
 *     stream timeout. Any partial line froze the loop for up to a second with
 *     nothing servicing the radio, so single-RX timed out and sat deaf.
 *     FIX: non-blocking character-at-a-time line assembly; the loop never
 *     blocks and never allocates.
 *
 * (3) TIME-ON-AIR WAS WRONG (Semtech formula, two separate errors).
 *       n_payload = 8 + max(ceil((8*PL - 4*SF + 28 + 16*CRC - 20*IH)
 *                                / (4*(SF - 2*DE))) * (CR+4), 0)
 *     The old code subtracted the -20*IH term unconditionally, but IH=1 means
 *     IMPLICIT header and this PHY uses EXPLICIT headers (IH=0). It also used
 *     4*(SF-2) at every SF, i.e. it assumed low-data-rate-optimize is always
 *     on; DE=1 only when the symbol time exceeds 16 ms (SF11/SF12 at BW125).
 *     At SF12/8B that under-reported airtime by ~17% (827 vs 991 ms). The
 *     server sizes its slot turnaround from TOA_MS, so an under-estimate lets
 *     the next transmission start while the air is still busy — a plausible
 *     source of the high-SNR CRC failures we have been calling collisions.
 *     FIX: exact Semtech formula with correct IH/DE handling.
 *
 * (4) TX->RX TURNAROUND ORDER.
 *     The old handle_tx() printed "OK TX ..." and only then re-armed the
 *     receiver, so the node was deaf for the duration of the serial write
 *     (~2 ms) right when the peer may answer. FIX: re-arm first, print after.
 *
 * (5) NO WAY TO RECOVER A WEDGED RADIO WITHOUT A REBOOT.
 *     LoRa.begin() (the only SX1276 hard reset) ran solely in setup(), and
 *     CFG merely rewrote registers. FIX: an explicit RESET command, a REBOOT
 *     command, and an automatic RX watchdog that re-inits the radio after
 *     RX_WATCHDOG_MS of silence while RX is enabled. REINIT= in STAT counts
 *     how often the watchdog fired, so we can measure whether (4) still
 *     happens rather than guessing.
 *
 * FRAME LAYOUT (set by server/frame.py; the sketch is payload-agnostic):
 *     next_hop(1) prev_hop(1) dst(1) src(1) ttl(1) seq(2) payload(...)
 *
 * CRC-FAIL CAVEAT (unchanged): the library only returns a packet from
 * parsePacket() when the CRC is valid, so CRC failures never surface as RX
 * events here — they appear as silent timeouts at the server's slot loop,
 * which calls optimizer.note_failure() on the rx_fut timeout path.
 */

#include <SPI.h>
#include <LoRa.h>

// Pinout — Teensy 4.0 + RFM95
static const int PIN_SS   = 10;
static const int PIN_RST  = 9;
static const int PIN_DIO0 = 2;

// Default radio config (overridden by CFG at runtime)
static long g_freq = 915000000L;   // US ISM band; use 868E6 for EU
static long g_bw   = 125000L;
static int  g_sf   = 9;
static int  g_cr   = 5;            // denom: 5..8 -> 4/5..4/8
static int  g_pwr  = 14;           // dBm

static const int  PREAMBLE_SYMS   = 8;      // library default; keep in sync
static const bool EXPLICIT_HEADER = true;   // this PHY uses explicit headers
static const bool CRC_ON          = true;   // apply_config() calls enableCrc()

// Re-init the radio if RX is enabled but nothing has been heard for this long.
// Generous by design: it is a safety net for a wedged radio, not a policy knob.
static const uint32_t RX_WATCHDOG_MS = 120000UL;

// Counters (reset on each STAT request)
static uint32_t cnt_tx = 0, cnt_tx_ok = 0, cnt_rx_ok = 0, cnt_rx_bad = 0;
static uint32_t cnt_reinit = 0;             // watchdog-triggered radio re-inits

static bool     g_rx_enabled = false;
static uint32_t g_last_rx_ms = 0;           // last successful RX (or RX enable)

#define SERIAL_BAUD 115200

// ---------- non-blocking line reader -----------------------------------------
// TX lines carry up to 255 bytes as hex: 2*255 + ~40 for the header/keys.
static const size_t LINEBUF = 600;
static char   g_line[LINEBUF];
static size_t g_len = 0;

// ---------- helpers -----------------------------------------------------------

static int hex_to_byte(char hi, char lo) {
  auto v = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  int h = v(hi), l = v(lo);
  if (h < 0 || l < 0) return -1;
  return (h << 4) | l;
}

static void print_hex(const uint8_t* buf, int n) {
  for (int i = 0; i < n; i++) {
    if (buf[i] < 0x10) Serial.print('0');
    Serial.print(buf[i], HEX);
  }
}

// Find "KEY=" in the line and return a pointer just past it, or nullptr.
static const char* kv_find(const char* line, const char* key) {
  const char* p = strstr(line, key);
  return p ? p + strlen(key) : nullptr;
}

static long kv_long(const char* line, const char* key, long dflt) {
  const char* p = kv_find(line, key);
  return p ? strtol(p, nullptr, 10) : dflt;
}

static int kv_int(const char* line, const char* key, int dflt) {
  return (int)kv_long(line, key, (long)dflt);
}

// ---------- time on air (Semtech SX1276, LoRa modulation) ---------------------
//
//   t_sym      = 2^SF / BW
//   t_preamble = (n_pre + 4.25) * t_sym
//   n_payload  = 8 + max(ceil((8*PL - 4*SF + 28 + 16*CRC - 20*IH)
//                             / (4*(SF - 2*DE))) * (CR + 4), 0)
//   DE = 1 when t_sym > 16 ms (low-data-rate optimize; SF11/SF12 @ BW125)
//   IH = 1 only in IMPLICIT header mode (0 here)
static long time_on_air_ms(int payload_len) {
  const float bw    = (float)g_bw;
  const float sf    = (float)g_sf;
  const float cr    = (float)(g_cr - 4);            // 1..4
  const float t_sym = ((float)(1UL << g_sf)) / bw * 1000.0f;   // ms

  const int de = (t_sym > 16.0f) ? 1 : 0;           // NOT "always 1" (old bug)
  const int ih = EXPLICIT_HEADER ? 0 : 1;           // NOT "always 1" (old bug)

  const float t_preamble = ((float)PREAMBLE_SYMS + 4.25f) * t_sym;

  const float num = 8.0f * (float)payload_len - 4.0f * sf + 28.0f
                    + (CRC_ON ? 16.0f : 0.0f) - 20.0f * (float)ih;
  const float den = 4.0f * (sf - 2.0f * (float)de);

  float n_sym = ceil(num / den) * (cr + 4.0f);
  if (n_sym < 0.0f) n_sym = 0.0f;
  const float n_payload = 8.0f + n_sym;

  return (long)(t_preamble + n_payload * t_sym + 0.5f);
}

// ---------- radio control ----------------------------------------------------

static void apply_config() {
  LoRa.idle();
  LoRa.setSpreadingFactor(g_sf);
  LoRa.setCodingRate4(g_cr);
  LoRa.setSignalBandwidth(g_bw);
  LoRa.setTxPower(g_pwr, PA_OUTPUT_PA_BOOST_PIN);   // PA_BOOST for RFM95
  LoRa.setFrequency(g_freq);
  LoRa.setPreambleLength(PREAMBLE_SYMS);
  LoRa.enableCrc();
  // NOTE: no LoRa.receive() here. Arming continuous RX and then polling with
  // parsePacket() is precisely the mix that wedged the old sketch; the poll in
  // loop() re-arms single-RX by itself. See header note (1).
}

// Full SX1276 re-init: LoRa.begin() pulses PIN_RST itself, which is the only
// real hardware reset of the radio. Everything else just rewrites registers.
//
// g_radio_up guard: LoRa.end() calls sleep() -> an SPI register write. Before the
// FIRST LoRa.begin() there is no SPI.begin() and _ss is not an OUTPUT yet, so on
// Teensy 4 that write stalls the SPI peripheral and the sketch hangs in setup()
// before printing anything. (Found the hard way: it bricked all 8 nodes into
// silence on the first flash of this rewrite.) Only tear down a radio that is up.
static bool g_radio_up = false;

static bool radio_init() {
  if (g_radio_up) LoRa.end();
  g_radio_up = false;
  LoRa.setPins(PIN_SS, PIN_RST, PIN_DIO0);
  if (!LoRa.begin(g_freq)) return false;
  g_radio_up = true;
  apply_config();
  g_last_rx_ms = millis();
  return true;
}

// ---------- command handlers -------------------------------------------------

static void handle_cfg(const char* line) {
  g_sf   = constrain(kv_int(line,  "SF=", g_sf),  7, 12);
  g_cr   = constrain(kv_int(line,  "CR=", g_cr),  5, 8);
  g_pwr  = constrain(kv_int(line,  "P=",  g_pwr), 2, 20);
  g_bw   = kv_long(line, "BW=", g_bw);
  g_freq = kv_long(line, "FQ=", g_freq);
  apply_config();
  Serial.println(F("OK CFG"));
}

static void handle_tx(const char* line) {
  const uint32_t id  = (uint32_t)kv_long(line, "ID=", 0);
  const int      len = kv_int(line, "LEN=", 0);
  const char*    hex = kv_find(line, "HEX=");

  if (len <= 0 || len > 255 || hex == nullptr) { Serial.println(F("ERR TX_BAD_HEX")); return; }
  // hex must supply exactly 2*len characters before the end of the line
  if ((int)strlen(hex) < 2 * len) { Serial.println(F("ERR TX_BAD_HEX")); return; }

  uint8_t buf[255];
  for (int i = 0; i < len; i++) {
    const int b = hex_to_byte(hex[2 * i], hex[2 * i + 1]);
    if (b < 0) { Serial.println(F("ERR TX_HEX_PARSE")); return; }
    buf[i] = (uint8_t)b;
  }

  const long toa = time_on_air_ms(len);

  LoRa.beginPacket();          // library drops to STANDBY internally
  LoRa.write(buf, len);
  LoRa.endPacket();            // blocking; returns on TxDone, leaves STANDBY
  cnt_tx++; cnt_tx_ok++;

  // TURNAROUND: get back to listening BEFORE spending ~2 ms on the serial
  // write, so we do not miss a peer that answers immediately. One parsePacket()
  // call re-arms single-RX (and cannot report a packet this early).
  if (g_rx_enabled) LoRa.parsePacket();

  Serial.print(F("OK TX ID="));
  Serial.print(id);
  Serial.print(F(" TOA_MS="));
  Serial.println(toa);
}

static void handle_stat() {
  const uint32_t deaf = g_rx_enabled ? (millis() - g_last_rx_ms) : 0;
  Serial.print(F("STAT TX="));   Serial.print(cnt_tx);
  Serial.print(F(" ACK="));      Serial.print(cnt_tx_ok);
  Serial.print(F(" RXOK="));     Serial.print(cnt_rx_ok);
  Serial.print(F(" RXBAD="));    Serial.print(cnt_rx_bad);
  Serial.print(F(" REINIT="));   Serial.print(cnt_reinit);   // watchdog fires
  Serial.print(F(" DEAF_MS="));  Serial.println(deaf);       // silence so far
  cnt_tx = cnt_tx_ok = cnt_rx_ok = cnt_rx_bad = 0;
}

// ---------- RX handling ------------------------------------------------------

// Sole RX entry point. parsePacket() re-arms single-RX whenever the radio is
// not already in it, so calling this every loop iteration keeps the receiver
// listening; the deaf window is one loop pass. Never call LoRa.receive().
static void poll_rx() {
  const int len = LoRa.parsePacket();
  if (len <= 0) return;

  uint8_t buf[255];
  int n = 0;
  while (LoRa.available() && n < (int)sizeof(buf)) buf[n++] = (uint8_t)LoRa.read();

  const float snr  = LoRa.packetSnr();
  const int   rssi = LoRa.packetRssi();
  cnt_rx_ok++;
  g_last_rx_ms = millis();

  Serial.print(F("RX LEN="));  Serial.print(n);
  Serial.print(F(" HEX="));    print_hex(buf, n);
  Serial.print(F(" SNR="));    Serial.print(snr, 2);
  Serial.print(F(" RSSI="));   Serial.print(rssi);
  Serial.println(F(" CRC=OK"));   // library only surfaces CRC-valid packets

  // The packet left the radio in STANDBY (library calls idle() on RX). Re-arm
  // immediately instead of waiting for the next loop pass — this single line
  // is what the old sketch was missing.
  LoRa.parsePacket();
}

// Safety net: if RX has been enabled but silent for RX_WATCHDOG_MS, hard-reset
// the radio. A healthy node hears beacons far more often than this, so firing
// means something genuinely wedged; REINIT= in STAT makes that measurable.
static void rx_watchdog() {
  if (!g_rx_enabled) return;
  if ((uint32_t)(millis() - g_last_rx_ms) < RX_WATCHDOG_MS) return;
  cnt_reinit++;
  if (radio_init()) Serial.println(F("OK RESET WATCHDOG"));
  else              Serial.println(F("ERR LORA_REINIT"));
  g_last_rx_ms = millis();
}

// ---------- dispatch ---------------------------------------------------------

static void handle_line(char* line) {
  if (line[0] == '\0') return;
  if      (!strncmp(line, "CFG",   3)) handle_cfg(line);
  else if (!strncmp(line, "TX",    2)) handle_tx(line);
  else if (!strncmp(line, "RXON",  4)) {
    g_rx_enabled = true; g_last_rx_ms = millis();
    LoRa.parsePacket();                       // arm single-RX now
    Serial.println(F("OK RXON"));
  }
  else if (!strncmp(line, "RXOFF", 5)) { g_rx_enabled = false; LoRa.idle(); Serial.println(F("OK RXOFF")); }
  else if (!strncmp(line, "STAT",  4)) handle_stat();
  else if (!strncmp(line, "PING",  4)) Serial.println(F("PONG"));
  else if (!strncmp(line, "RESET", 5)) {
    // Software equivalent of a power cycle for the RADIO, no Pi reboot needed.
    if (radio_init()) Serial.println(F("OK RESET"));
    else              Serial.println(F("ERR LORA_REINIT"));
  }
  else if (!strncmp(line, "REBOOT", 6)) {
    // Full MCU restart -> setup() runs again. Cortex-M AIRCR SYSRESETREQ.
    Serial.println(F("OK REBOOT"));
    Serial.flush();
    delay(20);
#if defined(SCB_AIRCR)
    SCB_AIRCR = 0x05FA0004;
#else
    radio_init();                              // fallback: radio-only reset
#endif
  }
  else { Serial.print(F("ERR UNKNOWN ")); Serial.println(line); }
}

// ---------- main -------------------------------------------------------------

void setup() {
  Serial.begin(SERIAL_BAUD);
  // Do NOT spin forever waiting for a host: a node that boots with no bridge
  // attached must still come up and start listening.
  const uint32_t t0 = millis();
  while (!Serial && (millis() - t0) < 3000) { ; }

  if (!radio_init()) {
    // Keep retrying rather than hanging forever in a while(true) — a transient
    // SPI/power fault at boot should not require a physical power cycle.
    while (!radio_init()) { Serial.println(F("ERR LORA_INIT")); delay(1000); }
  }
  Serial.println(F("PONG"));   // boot banner the bridge waits on
}

void loop() {
  // 1. Serial: consume everything available, non-blocking, one line at a time.
  //    (The old readStringUntil() could block the loop — and therefore the
  //    radio — for up to 1000 ms on a partial line.)
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_len > 0) { g_line[g_len] = '\0'; handle_line(g_line); g_len = 0; }
    } else if (g_len < LINEBUF - 1) {
      g_line[g_len++] = c;
    } else {
      g_len = 0;                                  // overlong line: drop it
      Serial.println(F("ERR LINE_TOO_LONG"));
    }
    if (g_rx_enabled) poll_rx();                  // stay responsive mid-burst
  }

  // 2. Service RX
  if (g_rx_enabled) poll_rx();

  // 3. Recover a wedged radio without anyone having to reboot the Pi
  rx_watchdog();
}

/*
 * NOTE — the strictly better RX path, once DIO0 is confirmed wired on all 8
 * nodes. The library's interrupt path keeps the radio in RX_CONTINUOUS and
 * never drops to STANDBY, so there is no deaf window at all:
 *
 *     LoRa.onReceive(on_rx_isr);   // sets a flag + copies the payload
 *     LoRa.receive();              // continuous; do NOT call parsePacket()
 *
 * with the Serial printing done from loop(), not the ISR. Verify DIO0 first:
 * if it is not connected, onReceive() never fires and the node goes totally
 * deaf — which is why this sketch stays on the polling path by default.
 */
