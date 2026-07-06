// Per-scheme full front-end round-trip through the REAL threads (clean channel):
//   build_packet_bits -> modulate -> pulse_shaping_filter_thread -> [chan+AGC] ->
//   match_filter_thread -> TimeSync_thread(ACQ@os) -> CFO -> phase -> strip
//   preamble -> demodulate -> decode_packet_bits.
// Mirrors the real sdr_system RX sizing (recv_msg_len = data_syms). Reports
// whether each scheme detects, the extracted symbol count, and CRC/payload.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "filters.hpp"
#include "synchronization.hpp"
#include "frequency_offset.hpp"
#include "phase_offset.hpp"
#include "FIFO.hpp"
#include <random>
#include <thread>
#include <cstdio>
#include <string>
using cf = std::complex<float>;
using Blk = std::pair<size_t, std::vector<cf>>;

static bool run(const char* scheme, double cfo_hz, double phase_deg, float sigma) {
    const int m = 5, num_taps = 151;
    const int U = 2, D = 1;
    const double roll = 0.25, symrate = 0.8e6;
    const double tx_rate = symrate * U / D, rx_rate = tx_rate;
    const int os = (int)std::lround(rx_rate / symrate);
    const size_t bytes_length = 125;

    ModulationType mt;
    try { mt = string_to_mod_type(scheme); } catch (...) { printf("%-8s unknown\n", scheme); return false; }
    Modulator mod(mt);
    int bps = mod.get_bits_per_symbol();
    auto pre = generate_msequence_preamble(m); int P = (int)pre.size();

    // payload + framing (same as main.cpp)
    std::string payload(bytes_length, '0');
    for (size_t i = 0; i < bytes_length; i++) payload[i] = char('0' + (i % 10));
    auto tx_bits = build_packet_bits(payload, 2, 5);
    int packet_bits = (int)tx_bits.size();
    int data_syms   = (packet_bits + bps - 1) / bps;      // recv_msg_len

    std::atomic<bool> stop{false};
    MutexFIFO<Blk> mod_fifo, shaped_fifo, agc_fifo, filtered_fifo, synced_fifo;

    bool add = true; auto pkt = mod.modulate(tx_bits, pre, add);
    mod_fifo.push({0, pkt});
    std::thread ps(pulse_shaping_filter_thread, std::ref(mod_fifo), std::ref(shaped_fifo),
        std::string("rrc"), symrate, tx_rate, num_taps, U, D, roll, 1, std::ref(stop), std::string("transmitter"));
    Blk shaped; for (int i = 0; i < 500 && !shaped_fifo.pop(shaped); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    stop.store(true); ps.join(); stop.store(false);
    if (shaped.second.empty()) { printf("%-8s no shaped output\n", scheme); return false; }

    // channel + AGC(RMS->1)
    std::mt19937 g(7); double cfo_rad = 2.0 * M_PI * cfo_hz / (symrate * os);
    double ph = phase_deg * M_PI / 180.0;
    std::normal_distribution<float> nz(0.f, sigma); std::uniform_real_distribution<float> lo(-0.05f, 0.05f);
    std::vector<cf> y; int pad = 19;
    for (int i = 0; i < pad; i++) y.push_back(cf(lo(g), lo(g)));
    for (size_t k = 0; k < shaped.second.size(); ++k) { cf s = shaped.second[k] * std::polar(1.0f, (float)(ph + cfo_rad * (double)k)); s += cf(nz(g), nz(g)); y.push_back(s); }
    for (int i = 0; i < pad; i++) y.push_back(cf(lo(g), lo(g)));
    double pw = 0; for (auto& s : y) pw += std::norm(s); float rms = std::sqrt(pw / y.size());
    for (auto& s : y) s /= rms;
    agc_fifo.push({0, y});

    std::thread mf(match_filter_thread, std::ref(agc_fifo), std::ref(filtered_fifo),
        std::string("rrc"), symrate, rx_rate, num_taps, 1, 1, roll, 1, std::ref(stop), std::string("receiver"));
    std::thread ts(TimeSync_thread, std::ref(filtered_fifo), std::ref(synced_fifo),
        std::ref(pre), (size_t)U, (size_t)D, os, std::ref(stop), data_syms, 15.0f);

    Blk aligned; bool got = false;
    for (int i = 0; i < 1200; i++) { if (synced_fifo.pop(aligned)) { got = true; break; } std::this_thread::sleep_for(std::chrono::milliseconds(2)); }
    stop.store(true); mf.join(); ts.join();
    if (!got) { printf("%-8s bps=%d data_syms=%d : *** NO DETECT / TimeSync empty ***\n", scheme, bps, data_syms); return false; }

    auto al = aligned.second;
    CFOCorrector cfo(symrate, 1, pre, CFOCorrector::Method::PILOT_AIDED);
    auto c = cfo.correct(al);
    PhaseOffsetCorrector poc(mod, pre, P, true, 0.02f, 0.707f, PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto pc = poc.correct(c);
    std::vector<cf> data(pc.begin() + P, pc.end());
    auto rx_bits = mod.demodulate(data);
    auto [idx, tot, rpayload, crc_ok] = decode_packet_bits(rx_bits);

    printf("%-8s bps=%d data_syms=%d | aligned=%zu (want %d) demod_bits=%zu | idx=%d tot=%d payload_len=%zu CRC=%s %s\n",
        scheme, bps, data_syms, al.size(), P + data_syms, rx_bits.size(),
        (int)idx, (int)tot, rpayload.size(), crc_ok ? "OK" : "FAIL",
        (crc_ok && rpayload == payload) ? "[OK]" : "[BAD]");
    return crc_ok;
}

int main() {
    const char* schemes[] = {"BPSK","QPSK","8-PSK","16-QAM","32-QAM","64-QAM"};
    printf("=== clean channel, small CFO+phase ===\n");
    for (auto s : schemes) run(s, 500.0, 20.0, 0.01f);
    return 0;
}
