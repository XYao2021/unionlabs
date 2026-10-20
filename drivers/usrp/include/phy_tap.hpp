#pragma once
// ============================================================
//  phy_tap.hpp — per-stage IQ taps for the RX chain.
//
//  --rx-tap <stage>[,<stage>...] writes the complex samples leaving a stage to
//    <viz-dir>/<scheme>/taps/<stage>/block_<id>.txt     ("real imag" per line)
//
//  WHY THIS EXISTS, when --viz already captures four points: viz answers "does
//  the link work" -- one block per tag, after AGC, for a constellation figure.
//  It cannot answer "what did the detector actually gate", because
//    * rx_wave is taken AFTER FeedforwardAGC normalises the burst to RMS 1.0,
//      so absolute amplitude -- the whole point of studying detection -- is
//      gone before it is written; and
//    * viz::once() keeps only the FIRST block, so you cannot compare bursts.
//  A tap is raw (whatever the stage emitted, unscaled) and keeps up to
//  `max_blocks` of them.
//
//  The predecessor was save_block_to_txt(), still present and still commented
//  out at eight call sites. It wrote to "debugging/" RELATIVE TO THE WORKING
//  DIRECTORY and failed per block with "cannot open file" when that folder did
//  not exist -- which is why the calls are all commented out rather than behind
//  a flag. Taps create their directories up front (main) and print the absolute
//  path once, so the files are findable from any working directory.
//
//  A tap does NOT cut the chain: the stage pushes downstream exactly as before,
//  so a tapped run is otherwise identical to an untapped one and CRC/BER still
//  mean something. Cutting is a separate flag (--rx-stop-after), not this one.
//
//  Stages are the RX pipeline's IQ-producing stages, in order. `eq` is the LAST
//  one: demodulation emits bits, not samples, so there is nothing for an IQ tap
//  to take after it (see save_bits_to_txt for that).
// ============================================================
#include <complex>
#include <vector>
#include <string>
#include <set>
#include <map>
#include <mutex>
#include <fstream>
#include <iostream>

namespace phytap {

// Set once from main() before the pipeline threads start, read-only thereafter
// -- the same discipline phy_log.hpp documents, so no synchronisation is needed
// on `dir`/`max_blocks`/the enabled set once the threads are running.
inline std::string dir;                  // <viz-dir>/<scheme>/taps
inline int         max_blocks = 20;      // per stage; <=0 means unlimited

inline std::mutex& mtx() { static std::mutex m; return m; }
inline std::set<std::string>& stages() { static std::set<std::string> s; return s; }
inline std::map<std::string, int>& counts() { static std::map<std::string, int> c; return c; }

// The IQ-producing RX stages, in pipeline order. Used by main() to validate
// --rx-tap and to create the directories.
inline const std::vector<std::string>& known() {
    static const std::vector<std::string> k = {
        "recv",    // raw USRP samples, before any detection
        "detect",  // energy detector output -- bursts, UNSCALED
        "agc",     // after AGC (what viz calls rx_wave)
        "mf",      // after the matched filter
        "sync",    // aligned burst at 1 sample/symbol, preamble still present
        "cfo",     // after carrier-frequency-offset correction
        "phase",   // after carrier-phase correction
        "eq"       // after the equaliser -- preamble stripped; last IQ point
    };
    return k;
}

inline bool on(const std::string& stage) { return stages().count(stage) != 0; }

// Claim a slot for one block: true only while this stage is under its cap.
// The counter is the only shared state, so it alone is locked -- the file write
// happens outside, since every (stage, id) names a distinct file.
inline bool claim(const std::string& stage) {
    std::lock_guard<std::mutex> lk(mtx());
    if (!stages().count(stage)) return false;
    int& n = counts()[stage];
    if (max_blocks > 0 && n >= max_blocks) {
        if (n == max_blocks) {           // say so once, then go quiet
            ++n;
            std::cout << "[TAP] " << stage << ": reached --rx-tap-max ("
                      << max_blocks << "), not writing more\n";
        }
        return false;
    }
    ++n;
    return true;
}

// Write the samples leaving `stage` for block `id`. Cheap no-op when no tap is
// enabled, which is the default -- this sits in the pipeline's hot loop.
inline void write(const std::string& stage, size_t id,
                  const std::vector<std::complex<float>>& x) {
    if (stages().empty()) return;        // fast path: --rx-tap not given
    if (!claim(stage)) return;

    const std::string path = dir + "/" + stage + "/block_" + std::to_string(id) + ".txt";
    std::ofstream f(path);
    if (!f.is_open()) {                  // directory missing -> say which, once per block
        std::cerr << "[TAP] cannot open " << path << "\n";
        return;
    }
    f << "# " << stage << "  block " << id << "  " << x.size()
      << " samples (real imag)\n";
    for (const auto& s : x) f << s.real() << " " << s.imag() << "\n";
    std::cout << "[TAP] " << stage << " block " << id << " -> " << path
              << " (" << x.size() << " samples)\n";
}

} // namespace phytap
