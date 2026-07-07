#pragma once
// ============================================================
//  fec.hpp
//
//  Forward Error Correction and BER measurement infrastructure.
//
//  Contents
//  ────────
//  1. PRBSGenerator      – pseudo-random bit sequence (PRBS-23).
//                          Produces a known repeatable bit stream
//                          for BER measurement without file I/O.
//
//  2. ConvolutionalEncoder – rate-1/2, constraint-length-7 (K=7)
//                           convolutional encoder.
//                           Polynomials: G1=0171, G2=0133 (octal)
//                           (standard NASA / 802.11 polynomials).
//
//  3. ViterbiDecoder      – hard-decision Viterbi decoder for the
//                           rate-1/2 K=7 code above.
//                           Also provides soft-decision input
//                           (pass LLRs from soft_demodulate_llr()).
//
//  4. BERMeasurer         – accumulates bit errors across many blocks
//                           and reports BER + 95% Wilson CI.
//
//  5. fec_encode_thread   – pipeline stage: raw bits → encoded bits.
//  6. fec_decode_thread   – pipeline stage: decoded bits → raw bits.
//
//  Rate-1/2 means every input bit becomes 2 output bits, so the
//  symbol count doubles at the modulator.  After Viterbi decoding
//  the output is back at the original rate.
// ============================================================

#include <cstdint>
#include <vector>
#include <array>
#include <limits>
#include <algorithm>
#include <numeric>
#include <cmath>
#include <iostream>
#include <atomic>
#include <thread>
#include <chrono>
#include <mutex>
#include "FIFO.hpp"


// ─────────────────────────────────────────────────────────────
//  1.  PRBSGenerator
//
//  PRBS-23: maximal-length sequence of period 2^23 - 1.
//  Polynomial: x^23 + x^18 + 1.
//  Returns repeatable pseudo-random bits; period ≈ 8 M bits.
// ─────────────────────────────────────────────────────────────
class PRBSGenerator {
public:
    explicit PRBSGenerator(uint32_t seed = 0x00000001u) : state_(seed & 0x7FFFFFu) {
        if (state_ == 0) state_ = 1;  // all-zero state is invalid
    }

    uint8_t next_bit() {
        uint8_t bit = static_cast<uint8_t>((state_ >> 22) & 1u);
        // Feedback: taps at 23 and 18 (1-indexed from MSB → shift = 22, 17)
        uint32_t feedback = ((state_ >> 22) ^ (state_ >> 17)) & 1u;
        state_ = ((state_ << 1) | feedback) & 0x7FFFFFu;
        return bit;
    }

    std::vector<uint8_t> generate(size_t num_bits) {
        std::vector<uint8_t> bits(num_bits);
        for (auto& b : bits) b = next_bit();
        return bits;
    }

    void reset(uint32_t seed = 0x00000001u) {
        state_ = seed & 0x7FFFFFu;
        if (state_ == 0) state_ = 1;
    }

private:
    uint32_t state_;
};


// ─────────────────────────────────────────────────────────────
//  2.  ConvolutionalEncoder
//
//  Rate 1/2, K=7, polynomials G1=0171₈, G2=0133₈ (octal).
//  Output is interleaved: [out1_bit0, out2_bit0, out1_bit1, …]
// ─────────────────────────────────────────────────────────────
class ConvolutionalEncoder {
public:
    static constexpr int K  = 7;            // constraint length
    static constexpr int G1 = 0171;         // octal
    static constexpr int G2 = 0133;         // octal
    static constexpr int NUM_STATES = 1 << (K-1);  // 64

    ConvolutionalEncoder() : shift_reg_(0) {
        std::cout << "[ConvEncoder] Rate=1/2  K=" << K
                  << "  G1=" << std::oct << G1
                  << "  G2=" << G2 << std::dec << "\n";
    }

    // Encode a vector of bits.
    // Flushes the shift register with (K-1)=6 zero bits at the end
    // so the decoder can return to the zero state (tail-biting not used).
    // Output length = 2 * (num_input_bits + K-1).
    std::vector<uint8_t> encode(const std::vector<uint8_t>& bits) {
        std::vector<uint8_t> out;
        out.reserve(2 * (bits.size() + K - 1));
        for (uint8_t b : bits)        clock_bit(b, out);
        for (int i = 0; i < K-1; ++i) clock_bit(0, out);  // flush
        return out;
    }

    void reset() { shift_reg_ = 0; }

private:
    uint8_t shift_reg_;   // K-1 = 6 bits of state

    static uint8_t parity(uint8_t x) {
        x ^= x >> 4; x ^= x >> 2; x ^= x >> 1;
        return x & 1u;
    }

    void clock_bit(uint8_t b, std::vector<uint8_t>& out) {
        // Output from the CURRENT state + input bit (reg = state | b<<(K-1)),
        // THEN shift the register. This must match the Viterbi trellis, which
        // uses reg = s | (b<<(K-1)) for the current state s. (The old code shifted
        // first and output from the NEXT state, so encoder ≠ decoder → the code
        // failed even with no channel errors.)
        uint8_t reg = static_cast<uint8_t>(shift_reg_ | (b << (K-1)));
        out.push_back(parity(static_cast<uint8_t>(reg & G1)));
        out.push_back(parity(static_cast<uint8_t>(reg & G2)));
        shift_reg_ = static_cast<uint8_t>(((shift_reg_ >> 1) | (b << (K-2))) & 0x3Fu);
    }
};


// ─────────────────────────────────────────────────────────────
//  3.  ViterbiDecoder
//
//  Hard-decision or soft-decision (LLR) Viterbi decoder.
//  Decodes the rate-1/2 K=7 code from ConvolutionalEncoder.
//  Output length = (encoded_bits/2) - (K-1).
// ─────────────────────────────────────────────────────────────
class ViterbiDecoder {
public:
    static constexpr int K         = ConvolutionalEncoder::K;
    static constexpr int NUM_STATES = ConvolutionalEncoder::NUM_STATES;  // 64
    static constexpr int G1        = ConvolutionalEncoder::G1;
    static constexpr int G2        = ConvolutionalEncoder::G2;

    ViterbiDecoder() {
        build_trellis();
        std::cout << "[ViterbiDecoder] K=" << K
                  << "  States=" << NUM_STATES << "\n";
    }

    // Hard-decision decode.
    // encoded : interleaved output bits from the encoder.
    std::vector<uint8_t> decode_hard(const std::vector<uint8_t>& encoded) {
        int num_symbols = static_cast<int>(encoded.size()) / 2;
        return viterbi(encoded, num_symbols, false, {});
    }

    // Soft-decision decode using LLRs.
    // llrs : one LLR per encoded bit (positive = bit 0 more likely).
    std::vector<uint8_t> decode_soft(const std::vector<float>& llrs) {
        int num_symbols = static_cast<int>(llrs.size()) / 2;
        return viterbi({}, num_symbols, true, llrs);
    }

private:
    // Trellis entry: for each (state, input_bit) → (next_state, out1, out2)
    struct TrellisEdge {
        int next_state;
        uint8_t out1, out2;
    };
    std::array<std::array<TrellisEdge, 2>, NUM_STATES> trellis_;

    static uint8_t parity(int x) {
        x ^= x >> 4; x ^= x >> 2; x ^= x >> 1; return x & 1;
    }

    void build_trellis() {
        for (int s = 0; s < NUM_STATES; ++s) {
            for (int b = 0; b < 2; ++b) {
                // next state: shift register shift
                int next = ((s >> 1) | (b << (K-2))) & (NUM_STATES-1);
                int reg  = s | (b << (K-1));
                trellis_[s][b] = {
                    next,
                    parity(reg & G1),
                    parity(reg & G2)
                };
            }
        }
    }

    std::vector<uint8_t> viterbi(
        const std::vector<uint8_t>& hard,
        int num_symbols,
        bool use_soft,
        const std::vector<float>& soft)
    {
        const float INF = std::numeric_limits<float>::max() / 2.0f;

        std::vector<float> path_metric(NUM_STATES, INF);
        path_metric[0] = 0.0f;

        // Survivor: for each time step and state, store the PREDECESSOR state on
        // the best path (not just the input bit — two predecessors share the same
        // (bit,next_state), so a bit-only survivor is ambiguous). The decoded
        // input bit is recovered as the top bit of the current state.
        std::vector<std::vector<uint8_t>> survivors(num_symbols,
                                                     std::vector<uint8_t>(NUM_STATES, 0));

        for (int t = 0; t < num_symbols; ++t) {
            std::vector<float> new_metric(NUM_STATES, INF);

            uint8_t h1 = use_soft ? 0 : hard[2*t];
            uint8_t h2 = use_soft ? 0 : hard[2*t+1];
            float   s1 = use_soft ? soft[2*t]   : 0.0f;
            float   s2 = use_soft ? soft[2*t+1] : 0.0f;

            for (int s = 0; s < NUM_STATES; ++s) {
                if (path_metric[s] >= INF) continue;
                for (int b = 0; b < 2; ++b) {
                    const auto& e = trellis_[s][b];
                    float branch;
                    if (use_soft) {
                        // Soft: branch metric = -LLR contribution
                        // bit=0 → add +LLR; bit=1 → add -LLR
                        float c1 = (e.out1 == 0) ?  s1 : -s1;
                        float c2 = (e.out2 == 0) ?  s2 : -s2;
                        branch = -(c1 + c2);  // negate: lower is better
                    } else {
                        // Hard: Hamming distance
                        branch = static_cast<float>((e.out1 ^ h1) + (e.out2 ^ h2));
                    }
                    float total = path_metric[s] + branch;
                    if (total < new_metric[e.next_state]) {
                        new_metric[e.next_state] = total;
                        survivors[t][e.next_state] = static_cast<uint8_t>(s);  // predecessor
                    }
                }
            }
            path_metric = new_metric;
        }

        // Traceback from state 0 (encoder was flushed to zero). The decoded input
        // bit at step t is the bit shifted into the top of state_{t+1}, i.e. its
        // top bit; then step back to the stored predecessor.
        std::vector<uint8_t> decoded(num_symbols);
        int state = 0;
        for (int t = num_symbols - 1; t >= 0; --t) {
            decoded[t] = static_cast<uint8_t>((state >> (K - 2)) & 1);
            state = survivors[t][state];
        }

        // Remove the K-1 flush bits
        int data_bits = num_symbols - (K - 1);
        if (data_bits <= 0) return {};
        decoded.resize(data_bits);
        return decoded;
    }
};


// ─────────────────────────────────────────────────────────────
//  4.  BERMeasurer
//
//  Thread-safe accumulator.  Call update() from any thread.
//  Call report() to get current BER + confidence interval.
// ─────────────────────────────────────────────────────────────
class BERMeasurer {
public:
    BERMeasurer() : total_bits_(0), error_bits_(0) {}

    void update(const std::vector<uint8_t>& tx_bits,
                const std::vector<uint8_t>& rx_bits)
    {
        size_t N = std::min(tx_bits.size(), rx_bits.size());
        size_t errs = 0;
        for (size_t i = 0; i < N; ++i)
            if (tx_bits[i] != rx_bits[i]) ++errs;

        std::lock_guard<std::mutex> lock(mtx_);
        total_bits_ += N;
        error_bits_ += errs;
    }

    struct BERReport {
        double ber;          // point estimate
        double ci_low;       // 95% Wilson CI lower bound
        double ci_high;      // 95% Wilson CI upper bound
        size_t total_bits;
        size_t error_bits;
    };

    BERReport report() const {
        std::lock_guard<std::mutex> lock(mtx_);
        if (total_bits_ == 0) return {0, 0, 0, 0, 0};

        double p  = static_cast<double>(error_bits_) / total_bits_;
        double n  = static_cast<double>(total_bits_);
        double z  = 1.96;  // 95% CI z-score
        double z2 = z * z;
        // Wilson score interval
        double denom = 1.0 + z2/n;
        double centre = (p + z2/(2.0*n)) / denom;
        double margin = z * std::sqrt(p*(1-p)/n + z2/(4.0*n*n)) / denom;

        std::cout << "[BERMeasurer] Bits=" << total_bits_
                  << "  Errors=" << error_bits_
                  << "  BER=" << p
                  << "  95%CI=[" << std::max(0.0, centre-margin)
                  << ", " << centre+margin << "]\n";

        return {p,
                std::max(0.0, centre - margin),
                centre + margin,
                total_bits_,
                error_bits_};
    }

    void reset() {
        std::lock_guard<std::mutex> lock(mtx_);
        total_bits_ = error_bits_ = 0;
    }

private:
    mutable std::mutex mtx_;
    size_t total_bits_;
    size_t error_bits_;
};


// ─────────────────────────────────────────────────────────────
//  Convenience one-shot helpers (rate-1/2 K=7).
//  Encode expands N bits → 2*(N+6). Decode (hard) recovers N bits.
//  Reused static objects (single-threaded application use in main).
// ─────────────────────────────────────────────────────────────
inline int fec_encoded_len(int nbits) { return 2 * (nbits + (ConvolutionalEncoder::K - 1)); }

inline std::vector<uint8_t> fec_encode_block(const std::vector<uint8_t>& bits) {
    static ConvolutionalEncoder enc;
    enc.reset();
    return enc.encode(bits);
}
inline std::vector<uint8_t> fec_decode_block(const std::vector<uint8_t>& coded) {
    static ViterbiDecoder dec;
    return dec.decode_hard(coded);
}


// ─────────────────────────────────────────────────────────────
//  5 & 6.  Pipeline threads
// ─────────────────────────────────────────────────────────────

// TX: raw bits → encoded bits
void fec_encode_thread(
    MutexFIFO<std::vector<uint8_t>>& raw_fifo,
    MutexFIFO<std::vector<uint8_t>>& encoded_fifo,
    std::atomic<bool>& stop_sign)
{
    ConvolutionalEncoder enc;
    std::vector<uint8_t> bits;
    size_t count = 0;
    std::cout << "[fec_encode_thread] Started\n";
    while (!stop_sign || raw_fifo.size() > 0) {
        if (!raw_fifo.pop(bits)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }
        enc.reset();
        auto coded = enc.encode(bits);
        encoded_fifo.push(std::move(coded));
        ++count;
    }
    std::cout << "[fec_encode_thread] Stopped.  Encoded " << count << " blocks.\n";
}

// RX: decoded bits → raw bits (Viterbi)
void fec_decode_thread(
    MutexFIFO<std::pair<size_t, std::vector<uint8_t>>>& encoded_fifo,
    MutexFIFO<std::pair<size_t, std::vector<uint8_t>>>& decoded_fifo,
    std::atomic<bool>& stop_sign)
{
    ViterbiDecoder dec;
    std::pair<size_t, std::vector<uint8_t>> msg;
    size_t count = 0;
    std::cout << "[fec_decode_thread] Started\n";
    while (!stop_sign || encoded_fifo.size() > 0) {
        if (!encoded_fifo.pop(msg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }
        auto decoded = dec.decode_hard(msg.second);
        decoded_fifo.push({msg.first, std::move(decoded)});
        ++count;
    }
    std::cout << "[fec_decode_thread] Stopped.  Decoded " << count << " blocks.\n";
}
