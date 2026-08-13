// LDPC codec test (no radio). Three checks:
//   1. Zero-error round-trip is EXACT (encode -> decode recovers the info bits).
//   2. Full packet path over a BSC: build_packet_bits -> fec_encode -> flip bits
//      -> fec_decode(info_len) -> CRC. Compares uncoded vs conv vs LDPC (hard).
//   3. Soft-decision over an AWGN/BPSK channel: LDPC min-sum vs conv soft Viterbi.
// LLR convention (matches soft_demodulate_llr): positive LLR = bit 0.
#include "messages.hpp"
#include "fec.hpp"
#include "ldpc.hpp"
#include <random>
#include <cstdio>
#include <cmath>

int main() {
    // ── 1. Single-block exact round-trip (no channel errors) ──
    {
        LdpcCode code(256, 3);
        std::mt19937 g(7);
        std::bernoulli_distribution coin(0.5);
        int fails = 0;
        for (int t = 0; t < 200; ++t) {
            std::vector<uint8_t> info(code.k());
            for (auto& b : info) b = coin(g);
            auto cw  = code.encode(info);
            std::vector<float> llr(code.n());
            for (int i = 0; i < code.n(); ++i) llr[i] = cw[i] ? -8.f : 8.f;
            auto dec = code.decode(llr);
            if (dec != info) ++fails;
        }
        printf("[1] zero-error round-trip: %d/200 blocks mismatched  -> %s\n",
               fails, fails == 0 ? "PASS" : "FAIL");
    }

    // ── Shared packet ──
    const size_t bytes_length = 125;
    std::string payload(bytes_length, '0');
    for (size_t i = 0; i < bytes_length; ++i) payload[i] = char('0' + i % 10);
    auto pkt = build_packet_bits(payload, 2, 5);
    int info_len = (int)pkt.size();

    // ── 2. Hard-decision packet path over a BSC ──
    fec_set_type(FecType::CONV);
    auto conv_coded = fec_encode_block(pkt);
    fec_set_type(FecType::LDPC, 256);
    auto ldpc_coded = fec_encode_block(pkt);
    printf("\npacket=%d bits -> conv coded=%zu, LDPC coded=%zu (both rate 1/2)\n",
           info_len, conv_coded.size(), ldpc_coded.size());

    std::mt19937 g(1);
    printf("\n%-7s | uncoded | conv(hard) | LDPC(hard)   (300 trials each)\n", "BER");
    for (double ber : {0.01, 0.02, 0.03, 0.05, 0.08}) {
        int trials = 300, unc = 0, conv = 0, ldpc = 0;
        std::bernoulli_distribution flip(ber);
        for (int t = 0; t < trials; ++t) {
            auto u = pkt;         for (auto& b : u) if (flip(g)) b ^= 1;
            auto [ui,ut,up,uc] = decode_packet_bits(u);
            if (uc && up == payload) ++unc;

            fec_set_type(FecType::CONV);
            auto c = conv_coded;  for (auto& b : c) if (flip(g)) b ^= 1;
            auto dc = fec_decode_block(c, info_len);
            auto [ci,ct,cp,cok] = decode_packet_bits(dc);
            if (cok && cp == payload) ++conv;

            fec_set_type(FecType::LDPC, 256);
            auto l = ldpc_coded;  for (auto& b : l) if (flip(g)) b ^= 1;
            auto dl = fec_decode_block(l, info_len);
            auto [li,lt,lp,lok] = decode_packet_bits(dl);
            if (lok && lp == payload) ++ldpc;
        }
        printf("%-7.3f | %5.1f%%  | %6.1f%%    | %6.1f%%\n",
               ber, 100.0*unc/trials, 100.0*conv/trials, 100.0*ldpc/trials);
    }

    // ── 3. Soft-decision over AWGN/BPSK ──
    // Map coded bit b -> BPSK symbol s = (b?-1:+1); y = s + n(0,sigma^2);
    // LLR = 2*y/sigma^2 (positive = bit 0). Sweep Eb/N0.
    printf("\n%-8s | conv soft | LDPC soft   (300 trials each, AWGN)\n", "Eb/N0dB");
    for (double ebn0_db : {1.0, 2.0, 3.0, 4.0}) {
        double rate = 0.5;
        double ebn0 = std::pow(10.0, ebn0_db / 10.0);
        double sigma = std::sqrt(1.0 / (2.0 * rate * ebn0));   // per coded bit
        std::normal_distribution<float> noise(0.f, (float)sigma);
        int trials = 300, conv = 0, ldpc = 0;
        for (int t = 0; t < trials; ++t) {
            fec_set_type(FecType::CONV);
            auto cc = fec_encode_block(pkt);
            std::vector<float> cllr(cc.size());
            for (size_t i = 0; i < cc.size(); ++i) {
                float s = cc[i] ? -1.f : 1.f;
                float y = s + noise(g);
                cllr[i] = 2.f * y / (float)(sigma * sigma);
            }
            auto dc = fec_soft_decode_block(cllr, info_len);
            auto [ci,ct,cp,cok] = decode_packet_bits(dc);
            if (cok && cp == payload) ++conv;

            fec_set_type(FecType::LDPC, 256);
            auto lc = fec_encode_block(pkt);
            std::vector<float> lllr(lc.size());
            for (size_t i = 0; i < lc.size(); ++i) {
                float s = lc[i] ? -1.f : 1.f;
                float y = s + noise(g);
                lllr[i] = 2.f * y / (float)(sigma * sigma);
            }
            auto dl = fec_soft_decode_block(lllr, info_len);
            auto [li,lt,lp,lok] = decode_packet_bits(dl);
            if (lok && lp == payload) ++ldpc;
        }
        printf("%-8.1f | %6.1f%%   | %6.1f%%\n",
               ebn0_db, 100.0*conv/trials, 100.0*ldpc/trials);
    }
    return 0;
}
