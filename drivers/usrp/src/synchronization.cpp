// synchronization.cpp — where in a captured burst does the packet actually start?
//
// TimeSync_thread correlates the known preamble against every sample offset of a
// detected burst (a brute-force search, not a tracking loop) and returns the
// aligned burst: preamble + exactly the expected number of data symbols.
//
// Because the search runs once per burst, there is no timing loop to follow drift
// WITHIN a burst. A sample rate that is even slightly wrong, or samples going
// missing part-way through, therefore shows up as a clean prefix followed by
// garbage rather than as a gradual degradation -- the preamble is short enough to
// correlate regardless, while the payload accumulates the error. Symptoms that
// look like a weak link but decode perfectly up to a fixed offset belong to this
// class, and gain will not touch them.

#include <iostream>
#include <complex>
#include <vector>
#include <cmath>
#include <mutex>
#include <algorithm>
#include <thread>

#include "FIFO.hpp"
#include "synchronization.hpp"
#include "transceiver.hpp"


void TimeSync_thread(MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& detected_fifo,
                     MutexFIFO<std::pair<size_t, std::vector<std::complex<float>>>>& synced_fifo,
                     std::vector<std::complex<float>>& preamble_sequence,
                     size_t U, size_t D, int sps, std::atomic<bool>& stop_sign,
                     int Data_length, float threshold)
{
    size_t processed_blocks = 0;
    std::pair<size_t, std::vector<std::complex<float>>> detected_message;

    ACQSynchronizer ACQ(preamble_sequence, sps, threshold, Data_length, true);

    DrainGate gate;
    while (gate.keep_going(stop_sign, detected_fifo)){
        
        if (!detected_fifo.pop(detected_message)){
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;;
        }

        // Perform ACQ.
        // Use the brute-force (every-sample) search, NOT PerformACQOptimized:
        // the ACQ now doubles as symbol-timing recovery, so it must test every
        // sub-symbol sampling phase. PerformACQOptimized's coarse search strides
        // by samples_per_symbol (=os), so it only tests ONE of the os phases and
        // can lock onto a half-symbol-off (zero-crossing) sampling instant →
        // massive ISI (looks like a huge spurious CFO) and BER ~0.5. The burst is
        // only tens of samples longer than the packet, so brute force is cheap.
        auto result = ACQ.SamplesACQPerformance(detected_message.second);
        
        if (result.PacketDetected) {
            std::cout << "\n✓ Packet detected successfully!" << std::endl;
            std::cout << "  Aligned burst ready (preamble + data)" << std::endl;
            std::cout << "  Number of symbols: " << result.AlignedStats.size() << std::endl;

            // Push the ALIGNED burst [preamble | data]. The preamble is kept so
            // the downstream data-aided CFO, phase-offset and channel-equalizer
            // stages can key off it. The preamble is stripped just before
            // demodulation (in channel_eq_thread / the no-EQ passthrough).
            synced_fifo.push({detected_message.first, result.AlignedStats});
            processed_blocks++;
            // save_block_to_txt(result.DecisionStats, detected_message.first, "sync");
        } else {
            std::cout << "\n✗ No packet detected in this block" << std::endl;
        }
    }
}