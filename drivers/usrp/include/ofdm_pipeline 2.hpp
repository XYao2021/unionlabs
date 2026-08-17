#pragma once
// ============================================================
//  ofdm_pipeline.hpp — OFDM waveform as FIFO pipeline stages.
//
//  OFDM replaces the single-carrier modulate + the whole RX
//  front-end (match filter → timing → CFO → phase → channel-eq):
//  the OFDM frame IS the baseband waveform, and OFDM::receive()
//  does frame sync, CFO and per-subcarrier equalization itself.
//
//  TX:  tx_bits → ofdm_modulation_thread   → shaped_fifo → transmit
//  RX:  receive → energy → AGC → ofdm_demodulation_thread → rx_bits
// ============================================================
#include <atomic>
#include <thread>
#include <chrono>
#include <string>
#include <vector>
#include <complex>
#include <cstdint>
#include <limits>
#include <algorithm>

#include "FIFO.hpp"
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "ofdm.hpp"
#include "viz.hpp"

// bits → QAM symbols → OFDM frame (time-domain baseband) → out_fifo.
// The frame is scaled so its peak ≈ tx_peak (OFDM has high PAPR; keep the DAC
// out of clipping — the RX AGC restores the level).
inline void ofdm_modulation_thread(
    MutexFIFO<std::vector<uint8_t>>& in_fifo,
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& out_fifo,
    std::string& scheme, int fft_size, int cp_len, float tx_peak,
    std::atomic<bool>& stop_sign)
{
    Modulator mod(string_to_mod_type(scheme));
    OFDM ofdm(fft_size, cp_len);
    std::vector<uint8_t> bits;
    size_t block_id = 0;

    DrainGate gate;
    while (gate.keep_going(stop_sign, in_fifo)) {
        if (!in_fifo.pop(bits)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        std::vector<std::complex<float>> ep{std::complex<float>(1, 0)};
        bool add = false;                          // no single-carrier preamble
        auto qam   = mod.modulate(bits, ep, add);  // bits → QAM symbols
        auto frame = ofdm.modulate(qam);           // → [SC | chest | data] time samples

        viz::capture("tx_symbols", qam);           // TX constellation
        viz::capture("tx_wave", frame, 2000);      // TX waveform (time/spectrum)

        float peak = 0.0f;
        for (auto& s : frame) peak = std::max(peak, std::abs(s));
        float scale = (peak > 1e-6f) ? tx_peak / peak : 1.0f;
        for (auto& s : frame) s *= scale;

        std::cout << "[OFDM MOD] block " << block_id << ": " << qam.size()
                  << " QAM syms -> " << frame.size() << " samples (peak "
                  << tx_peak << ")\n";
        out_fifo.push({block_id++, frame});
    }
    std::cout << "[OFDM MOD] Thread stopped.\n";
}

// burst (after energy detect + AGC) → OFDM.receive() (sync + CFO + equalize) →
// QAM demod → bits. num_qam = data QAM symbols per message chunk.
inline void ofdm_demodulation_thread(
    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& in_fifo,
    MutexFIFO<std::pair<size_t, std::vector<uint8_t>>>& out_fifo,
    std::string& scheme, int fft_size, int cp_len, int num_qam,
    std::atomic<bool>& stop_sign,
    // Optional soft-decision output: when non-null (fec_soft), push one LLR per
    // coded bit here in lockstep with the bits block, so the sink's soft LDPC/
    // turbo decoder gets real reliabilities instead of silently falling back to
    // hard-decision. LLRs come from soft_demodulate_llr on the EQUALIZED symbols
    // (positive = bit 0). Without this the OFDM path only ever emitted hard bits.
    MutexFIFO<std::pair<size_t, std::vector<float>>>* llr_fifo = nullptr)
{
    Modulator mod(string_to_mod_type(scheme));
    OFDM ofdm(fft_size, cp_len);
    std::pair<size_t, std::vector<std::complex<float>>> msg;

    DrainGate gate;
    while (gate.keep_going(stop_sign, in_fifo)) {
        if (!in_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        int start = -1; float cfo = 0.0f;
        auto qam  = ofdm.receive(msg.second, num_qam, &start, &cfo);
        viz::capture("rx_wave", msg.second, 2000);   // RX burst (time/spectrum)
        viz::capture("rx_symbols", qam);             // RX constellation (equalized)
        if ((int)qam.size() < num_qam) {
            std::cout << "[OFDM DEMOD] block " << msg.first << ": short ("
                      << qam.size() << "/" << num_qam << ") — skipping\n";
            continue;
        }
        auto bits = mod.demodulate(qam);
        std::cout << "[OFDM DEMOD] block " << msg.first << ": frame@" << start
                  << " CFO=" << cfo << " sc -> " << qam.size() << " QAM -> "
                  << bits.size() << " bits\n";
        out_fifo.push({msg.first, bits});

        if (llr_fifo) {
            // Decision-directed noise-variance estimate (mean distance² to the
            // nearest constellation point, per I/Q) → well-scaled LLRs.
            const auto& C = mod.get_constellation();
            double e = 0;
            for (const auto& s : qam) {
                float best = std::numeric_limits<float>::max();
                for (const auto& c : C) best = std::min(best, std::norm(s - c));
                e += best;
            }
            float nvar = std::max(1e-3f, (float)(e / std::max<size_t>(1, qam.size())) / 2.0f);
            auto llr = soft_demodulate_llr(qam, mod, nvar);
            llr_fifo->push({msg.first, std::move(llr)});
        }
    }
    std::cout << "[OFDM DEMOD] Thread stopped.\n";
}
