// Regression: the RX matched filter must treat every detected burst as an
// INDEPENDENT capture.
//
// Bursts arrive from the energy detector separated in time by an arbitrary idle
// gap, so they are not a contiguous stream. match_filter_thread used to carry
// the previous burst's overlap state into the next one whenever two consecutive
// captures happened to have the same length, splicing stale samples onto the
// head of the new burst (the first num_taps-1 samples). Two identical bursts
// must filter to identical output.
//
// Build (needs fftw3f, no UHD/radio):
//   g++ -std=c++17 -O2 -include atomic -include cstdint \
//       -I tests/stub -I include -o /tmp/mfbi \
//       tests/mf_burst_independence_test.cpp src/filters.cpp \
//       -lfftw3f -lfftw3f_threads -lpthread && /tmp/mfbi
#include <atomic>
#include <cstdint>
#include <complex>
#include <cstdio>
#include <cmath>
#include <thread>
#include <vector>
#include "fifo.hpp"

void match_filter_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>&,
                         MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>&,
                         const std::string, const double, const double,
                         const int, const int, const int, const double, const int,
                         std::atomic<bool>&, const std::string);

int main() {
    const double symbol_rate = 400000, sample_rate = 800000;
    const int num_taps = 65, N = 600;

    std::vector<std::complex<float>> burst(N);
    for (int i = 0; i < N; ++i)
        burst[i] = {std::sin(0.17f * i) + 0.4f * std::sin(1.13f * i),
                    std::cos(0.23f * i) + 0.4f * std::cos(0.71f * i)};

    MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>> in, out;
    std::atomic<bool> stop{false};
    std::thread th(match_filter_thread, std::ref(in), std::ref(out), std::string("rrc"),
                   symbol_rate, sample_rate, num_taps, 1, 1, 0.35, 1,
                   std::ref(stop), std::string("rx"));

    in.push({1, burst});
    in.push({2, burst});                 // same length, later in time
    std::this_thread::sleep_for(std::chrono::milliseconds(1200));
    stop = true;
    th.join();

    std::vector<std::pair<size_t, std::vector<std::complex<float>>>> got;
    std::pair<size_t, std::vector<std::complex<float>>> m;
    while (out.pop(m)) got.push_back(m);
    if (got.size() < 2) { std::printf("RESULT: only %zu blocks out [BAD]\n", got.size()); return 2; }

    const auto& A = got[0].second; const auto& B = got[1].second;
    size_t n = std::min(A.size(), B.size()), ndiff = 0, first = (size_t)-1, last = 0;
    double worst = 0;
    for (size_t i = 0; i < n; ++i) {
        double d = std::abs(A[i] - B[i]);
        if (d > 1e-4) { ++ndiff; if (first == (size_t)-1) first = i; last = i; }
        if (d > worst) worst = d;
    }
    bool ok = (ndiff == 0 && A.size() == B.size());
    std::printf("\nmf burst independence | out=%zu/%zu differing=%zu/%zu worst=%.6f",
                A.size(), B.size(), ndiff, n, worst);
    if (ndiff) std::printf(" range=[%zu..%zu]", first, last);
    std::printf("  %s\n", ok ? "[OK]" : "[BAD] stale state across bursts");
    return ok ? 0 : 1;
}
