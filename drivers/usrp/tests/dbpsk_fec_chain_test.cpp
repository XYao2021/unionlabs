// Differential + FEC round trip through the REAL threads on a CLEAN channel.
//
// scheme_frontend_test covers only coherent schemes (BPSK..64-QAM) and runs
// without FEC, so the combination the radio actually uses -- DBPSK with rate-1/2
// convolutional coding, 2076 coded bits -- had no coverage at all. On hardware
// that combination loses exactly one symbol partway through every burst: the
// coded bits match up to a fixed index and the remainder realigns at shift +1.
//
// Build (needs fftw3f, no UHD):
//   g++ -std=c++17 -O2 -include atomic -include cstdint -I tests/stub -I include \
//       -o /tmp/dfc tests/dbpsk_fec_chain_test.cpp src/modulator.cpp src/filters.cpp \
//       src/synchronization.cpp -lfftw3f -lfftw3f_threads -lpthread && /tmp/dfc
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "fec.hpp"
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

static bool run(const char* scheme, bool use_fec, size_t bytes_length) {
    const int m = 5, num_taps = 151;
    const int U = 2, D = 1;
    const double roll = 0.25, symrate = 0.8e6;
    const double tx_rate = symrate * U / D, rx_rate = tx_rate;
    const int os = (int)std::lround(rx_rate / symrate);

    ModulationType mt;
    try { mt = string_to_mod_type(scheme); } catch (...) { printf("%-7s unknown\n", scheme); return false; }
    Modulator mod(mt);
    const int bps = mod.get_bits_per_symbol();
    const bool differential = (std::string(scheme).size() && scheme[0] == 'D');
    auto pre = generate_msequence_preamble(m); const int P = (int)pre.size();

    std::string payload = "CHUNK-4 ";
    while (payload.size() < bytes_length) payload += "abcdefghijklmnopqrstuvwxyz0123456789 ";
    payload.resize(bytes_length);

    auto info  = build_packet_bits(payload, 3, 5);
    auto coded = use_fec ? fec_encode_block(info) : info;
    const int data_syms = ((int)coded.size() + bps - 1) / bps;

    std::atomic<bool> stop{false};
    MutexFIFO<Blk> mod_fifo, shaped_fifo, agc_fifo, filtered_fifo, synced_fifo;

    bool add = true; auto pkt = mod.modulate(coded, pre, add);
    mod_fifo.push({0, pkt});
    std::thread ps(pulse_shaping_filter_thread, std::ref(mod_fifo), std::ref(shaped_fifo),
        std::string("rrc"), symrate, tx_rate, num_taps, U, D, roll, 1, std::ref(stop), std::string("transmitter"));
    Blk shaped; for (int i = 0; i < 800 && !shaped_fifo.pop(shaped); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    stop.store(true); ps.join(); stop.store(false);
    if (shaped.second.empty()) { printf("%-7s no shaped output\n", scheme); return false; }

    // Clean channel: tiny padding + AGC only. No noise, no CFO, no phase error,
    // so anything that goes wrong here is ours.
    std::mt19937 g(7); std::uniform_real_distribution<float> lo(-0.001f, 0.001f);
    std::vector<cf> y; const int pad = 19;
    for (int i = 0; i < pad; i++) y.push_back(cf(lo(g), lo(g)));
    for (auto& s : shaped.second) y.push_back(s);
    for (int i = 0; i < pad; i++) y.push_back(cf(lo(g), lo(g)));
    double pw = 0; for (auto& s : y) pw += std::norm(s);
    float rms = std::sqrt(pw / y.size()); for (auto& s : y) s /= rms;
    agc_fifo.push({0, y});

    std::thread mf(match_filter_thread, std::ref(agc_fifo), std::ref(filtered_fifo),
        std::string("rrc"), symrate, rx_rate, num_taps, 1, 1, roll, 1, std::ref(stop), std::string("receiver"));
    std::thread ts(TimeSync_thread, std::ref(filtered_fifo), std::ref(synced_fifo),
        std::ref(pre), (size_t)U, (size_t)D, os, std::ref(stop), data_syms, 15.0f);

    Blk aligned; bool got = false;
    for (int i = 0; i < 1500; i++) { if (synced_fifo.pop(aligned)) { got = true; break; }
        std::this_thread::sleep_for(std::chrono::milliseconds(2)); }
    stop.store(true); mf.join(); ts.join();
    if (!got) { printf("%-7s fec=%d : NO DETECT\n", scheme, (int)use_fec); return false; }

    auto al = aligned.second;
    CFOCorrector cfo(symrate, 1, pre, CFOCorrector::Method::PILOT_AIDED);
    auto c = cfo.correct(al);
    PhaseOffsetCorrector poc(mod, pre, P, !differential, 0.02f, 0.707f,
                             PhaseOffsetCorrector::EstimationMethod::PREAMBLE);
    auto pc = poc.correct(c);
    // Differential needs the last preamble symbol as the reference, exactly as
    // ACQ hands 2077 symbols to the demodulator for 2076 data symbols.
    std::vector<cf> data(pc.begin() + (differential ? P - 1 : P), pc.end());
    auto rx_bits = mod.demodulate(data);

    size_t n = std::min(rx_bits.size(), coded.size()), first_bad = n, errs = 0;
    for (size_t i = 0; i < n; ++i) if (rx_bits[i] != coded[i]) { if (first_bad == n) first_bad = i; ++errs; }

    auto dec = use_fec ? fec_decode_block(rx_bits, (int)info.size()) : rx_bits;
    auto [idx, tot, rp, crc_ok] = decode_packet_bits(dec);

    printf("%-7s fec=%d bytes=%zu | syms=%d aligned=%zu demod=%zu | coded errs=%zu/%zu"
           " first_bad=%s | CRC=%s %s\n",
        scheme, (int)use_fec, bytes_length, data_syms, al.size(), rx_bits.size(),
        errs, n, (first_bad == n ? std::string("none") : std::to_string(first_bad)).c_str(),
        crc_ok ? "OK" : "FAIL", (crc_ok && rp == payload) ? "[OK]" : "[BAD]");
    return crc_ok;
}

int main(int argc, char** argv) {
    // `sweep` walks the payload size up until the chain stops decoding. That is
    // the only honest way to answer "how large can a chunk be": the framing sets
    // no limit of its own -- the receiver derives the payload length from the bit
    // count -- so the ceiling is wherever sync, the filters or the detector give
    // out, which is a measurement rather than a constant.
    if (argc > 1 && std::string(argv[1]) == "sweep") {
        printf("=== payload sweep, CLEAN channel (no noise, no CFO, no phase) ===\n");
        for (const char* s : {"DBPSK", "DQPSK"}) {
            printf("\n-- %s, FEC on --\n", s);
            for (size_t n : {64u, 125u, 250u, 500u, 1000u, 2000u, 4000u, 8000u})
                run(s, true, n);
        }
        return 0;
    }
    printf("=== CLEAN channel: no noise, no CFO, no phase error ===\n");
    for (bool fec : {false, true})
        for (const char* s : {"BPSK", "DBPSK", "QPSK", "DQPSK"})
            run(s, fec, 125);
    return 0;
}
