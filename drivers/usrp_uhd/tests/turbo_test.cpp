// turbo_test.cpp — standalone check of the turbo codec (no radio).
//   1. Zero-noise round-trip is exact.
//   2. BER + block-error vs Eb/N0 over BPSK-AWGN, vs uncoded, with avg iterations.
// LLR convention: positive = bit 0.
#include "turbo.hpp"
#include <random>
#include <cstdio>
#include <cmath>

int main(int argc, char** argv) {
    int K = (argc > 1) ? std::atoi(argv[1]) : 256;
    int iters = (argc > 2) ? std::atoi(argv[2]) : 6;
    TurboCode tc(K, iters);
    int k = tc.k(), n = tc.n();
    std::mt19937 g(1);
    std::bernoulli_distribution coin(0.5);

    // 1. exact round-trip (no noise)
    int fails = 0;
    for (int t = 0; t < 100; ++t) {
        std::vector<uint8_t> u(k); for (auto& b : u) b = coin(g);
        auto c = tc.encode(u);
        std::vector<float> llr(n);
        for (int i = 0; i < n; ++i) llr[i] = c[i] ? -10.f : 10.f;
        if (tc.decode(llr) != u) ++fails;
    }
    printf("[1] zero-noise round-trip: %d/100 mismatched -> %s\n\n",
           fails, fails == 0 ? "PASS" : "FAIL");

    // 2. BER vs Eb/N0 (rate 1/2 -> Es/N0 = 0.5*Eb/N0)
    printf("%-8s | uncoded BER | turbo BER  blk-err  avg-iters\n", "Eb/N0dB");
    for (double ebn0_db : {0.0, 1.0, 2.0, 3.0, 4.0}) {
        double R = 0.5, ebn0 = std::pow(10.0, ebn0_db / 10.0);
        double sigma = std::sqrt(1.0 / (2.0 * R * ebn0));
        std::normal_distribution<float> noise(0.f, (float)sigma);
        long ub_err = 0, ub_tot = 0, tb_err = 0, tb_tot = 0, blk_err = 0, it_sum = 0;
        int blocks = 200;
        for (int t = 0; t < blocks; ++t) {
            std::vector<uint8_t> u(k); for (auto& b : u) b = coin(g);
            auto c = tc.encode(u);
            std::vector<float> llr(n);
            for (int i = 0; i < n; ++i) {
                float s = c[i] ? -1.f : 1.f, y = s + noise(g);
                llr[i] = 2.f * y / (float)(sigma * sigma);        // positive = bit 0
            }
            // uncoded reference: hard-slice the systematic bits
            for (int i = 0; i < k; ++i) { ub_err += ((llr[i] < 0) ? 1 : 0) != u[i]; ++ub_tot; }
            int used = 0; auto d = tc.decode(llr, &used); it_sum += used;
            int be = 0; for (int i = 0; i < k; ++i) { be += (d[i] != u[i]); ++tb_tot; }
            tb_err += be; if (be) ++blk_err;
        }
        printf("%-8.1f | %.5f     | %.6f   %4.1f%%   %5.2f\n",
               ebn0_db, (double)ub_err / ub_tot, (double)tb_err / tb_tot,
               100.0 * blk_err / blocks, (double)it_sum / blocks);
    }
    return 0;
}
