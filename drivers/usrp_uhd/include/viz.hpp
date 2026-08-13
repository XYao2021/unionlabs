#pragma once
// ============================================================
//  viz.hpp — lightweight signal capture for visualization.
//
//  When --viz is on, key points in the TX and RX pipelines dump one
//  block of complex samples to text files ("real imag" per line):
//    phy_outputs/tx_symbols.txt   modulated symbols (TX constellation)
//    phy_outputs/tx_wave.txt      transmitted baseband waveform (time domain)
//    phy_outputs/rx_wave.txt      received burst after AGC (time domain)
//    phy_outputs/rx_symbols.txt   equalized symbols before demod (RX constellation)
//  tools/plot_viz.py reads these and plots time / spectrum (FFT of the
//  waveform) / constellation for both sides. Each file is written once
//  per run (the first block), so the overhead is negligible.
// ============================================================
#include <complex>
#include <vector>
#include <string>
#include <fstream>
#include <set>
#include <mutex>

namespace viz {

inline bool        enabled = false;
inline std::string dir     = "phy_outputs";

inline std::mutex& mtx() { static std::mutex m; return m; }
inline std::set<std::string>& done() { static std::set<std::string> s; return s; }

// Returns true only the FIRST time called with `tag` this run (thread-safe),
// so a pipeline stage saves just one block for the figure.
inline bool once(const std::string& tag) {
    std::lock_guard<std::mutex> lk(mtx());
    if (done().count(tag)) return false;
    done().insert(tag);
    return true;
}

// Save complex samples as "real imag" per line. max_n<=0 saves all.
inline void save_iq(const std::string& tag,
                    const std::vector<std::complex<float>>& x, int max_n = -1) {
    std::ofstream f(dir + "/" + tag + ".txt");
    if (!f.is_open()) return;
    int n = (max_n > 0 && max_n < (int)x.size()) ? max_n : (int)x.size();
    f << "# " << tag << "  " << n << " samples (real imag)\n";
    for (int i = 0; i < n; ++i) f << x[i].real() << " " << x[i].imag() << "\n";
    std::cout << "[VIZ] wrote " << dir << "/" << tag << ".txt (" << n << " samples)\n";
}

// Convenience: save one block per run for a given tag.
inline void capture(const std::string& tag,
                    const std::vector<std::complex<float>>& x, int max_n = -1) {
    if (enabled && once(tag)) save_iq(tag, x, max_n);
}

} // namespace viz
