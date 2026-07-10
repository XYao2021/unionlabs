// lora_loopback_test.cpp — prove the LoRa/CSS receiver decodes.
// Build: c++ -std=c++17 -O2 -I../include lora_loopback_test.cpp -o /tmp/lora_test
#include "lora.hpp"
#include <cstdio>
#include <random>
#include <vector>

using lora::cf;

static int ber(const std::vector<uint8_t>& a, const std::vector<uint8_t>& b) {
    size_t n = std::min(a.size(), b.size());
    int e = 0;
    for (size_t i = 0; i < n; ++i) e += (a[i] != b[i]);
    e += (int)(std::max(a.size(), b.size()) - n);   // length mismatch counts as errors
    return e;
}

static std::vector<uint8_t> rand_bits(int n, std::mt19937& rng) {
    std::vector<uint8_t> b(n);
    for (auto& x : b) x = rng() & 1;
    return b;
}

// add complex AWGN at a given per-sample SNR (dB), signal power ~1
static void add_noise(std::vector<cf>& s, double snr_db, std::mt19937& rng) {
    double sigma = std::sqrt(0.5 / std::pow(10.0, snr_db / 10.0));
    std::normal_distribution<float> g(0.0f, (float)sigma);
    for (auto& x : s) x += cf(g(rng), g(rng));
}

int main() {
    std::mt19937 rng(12345);
    const int SF = 8, NPRE = 8, NBITS = 8 * SF * 10;   // 10 payload bytes worth
    int fails = 0;

    // 1) clean, aligned loopback -> must be exact
    {
        auto bits = rand_bits(NBITS, rng);
        auto tx = lora::modulate(bits, SF, NPRE);
        auto rx = lora::demodulate(tx, SF, NBITS, NPRE);
        int e = ber(bits, rx);
        printf("[1] clean loopback           SF=%d bits=%d  errors=%d  -> %s\n",
               SF, NBITS, e, e == 0 ? "PASS" : "FAIL");
        fails += (e != 0);
    }

    // 2) timing offset (prepend junk samples) -> sync must find the frame
    {
        auto bits = rand_bits(NBITS, rng);
        auto tx = lora::modulate(bits, SF, NPRE);
        std::vector<cf> rx(137, cf(0.01f, -0.02f));    // leading junk
        rx.insert(rx.end(), tx.begin(), tx.end());
        auto out = lora::demodulate(rx, SF, NBITS, NPRE);
        int e = ber(bits, out);
        printf("[2] +137-sample timing offset  errors=%d  -> %s\n",
               e, e == 0 ? "PASS" : "FAIL");
        fails += (e != 0);
    }

    // 3) AWGN sweep — LoRa should decode well below 0 dB SNR (processing gain)
    for (double snr : {0.0, -5.0, -10.0}) {
        int tot = 0, N = 5;
        for (int t = 0; t < N; ++t) {
            auto bits = rand_bits(NBITS, rng);
            auto tx = lora::modulate(bits, SF, NPRE);
            add_noise(tx, snr, rng);
            auto out = lora::demodulate(tx, SF, NBITS, NPRE);
            tot += ber(bits, out);
        }
        double b = (double)tot / (N * NBITS);
        printf("[3] AWGN %5.0f dB SNR          mean BER=%.4f  -> %s\n",
               snr, b, b < 0.02 ? "PASS" : (b < 0.15 ? "MARGINAL" : "FAIL"));
        if (snr >= -5.0) fails += (b >= 0.02);          // must be clean at >= -5 dB
    }

    // 4) integer CFO
    {
        auto bits = rand_bits(NBITS, rng);
        auto tx = lora::modulate(bits, SF, NPRE);
        int cfo = 3;
        for (size_t n = 0; n < tx.size(); ++n) {
            double ph = lora::TWO_PI * (double)cfo / (1 << SF) * (double)n;
            tx[n] *= cf((float)std::cos(ph), (float)std::sin(ph));
        }
        auto out = lora::demodulate(tx, SF, NBITS, NPRE);
        int e = ber(bits, out);
        printf("[4] +%d-bin integer CFO         errors=%d  -> %s\n",
               cfo, e, e == 0 ? "PASS" : "FAIL");
        fails += (e != 0);
    }

    printf("\n%s (%d check(s) failed)\n", fails == 0 ? "ALL PASS" : "SOME FAILED", fails);
    return fails == 0 ? 0 : 1;
}
