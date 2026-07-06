#pragma once
// ============================================================
//  ACQ_stop_and_wait.hpp
//  Stop-and-wait ARQ layer on top of PHYSICAL_LAYER.
//
//  SOURCE: transmits chunks one at a time, waits for ACK,
//          retransmits on timeout.
//  SINK:   receives chunks, reassembles message, sends ACKs.
// ============================================================

#include <string>
#include <vector>
#include <map>
#include <iostream>
#include <chrono>
#include <thread>
#include <atomic>
#include <sstream>
#include <iomanip>

#include "physical_layer.hpp"
#include "messages.hpp"
#include "ack_transport.hpp"

// ─────────────────────────────────────────────────────────────
//  SOURCE
// ─────────────────────────────────────────────────────────────
class SOURCE {
public:
    // ack: ACK channel (RF or TCP) the receiver acknowledges chunks on.
    // max_attempts: give up on a chunk after this many un-ACKed transmissions
    // (0 = retry forever). Prevents a dead reverse link from hanging the run.
    SOURCE(PHYSICAL_LAYER& phy, AckLink& ack, int timeout_ms,
           int num_bits_per_packet, int max_attempts = 50)
        : phy_(phy), ack_(ack), timeout_ms_(timeout_ms),
          num_bits_(num_bits_per_packet), max_attempts_(max_attempts)
    {}

    void start(const std::vector<std::string>& chunks) {
        chunks_  = chunks;
        running_ = true;
        done_    = false;
        worker_  = std::thread(&SOURCE::run, this);
    }

    void stop() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

    // True once every chunk has been ACKed (or given up on) — lets main() exit.
    bool done() const { return done_.load(); }

private:
    PHYSICAL_LAYER&      phy_;
    AckLink&             ack_;
    int                  timeout_ms_;
    int                  num_bits_;
    int                  max_attempts_;
    std::vector<std::string> chunks_;
    std::atomic<bool>    running_{false};
    std::atomic<bool>    done_{false};
    std::thread          worker_;

    void run() {
        uint8_t total = static_cast<uint8_t>(chunks_.size());
        size_t  sent  = 0;
        size_t  retx  = 0;
        size_t  failed = 0;

        // Per-chunk bookkeeping for the end-of-run summary.
        std::vector<int>  tries_per(total, 0);
        std::vector<bool> ok_per(total, false);

        for (uint8_t idx = 0; idx < total && running_; ++idx) {
            auto bits = build_packet_bits(chunks_[idx], idx, total);

            bool acked = false;
            int  tries = 0;

            while (!acked && running_) {
                ++tries;
                std::cout << "[SOURCE] TX chunk " << (int)idx+1 << "/" << (int)total
                          << "  attempt=" << tries << "\n";

                phy_.transmit(bits);
                ++sent;

                // Wait for ACK in rx_bits_fifo
                auto deadline = std::chrono::steady_clock::now()
                              + std::chrono::milliseconds(timeout_ms_);

                while (!acked && std::chrono::steady_clock::now() < deadline && running_) {
                    uint8_t ridx;
                    if (ack_.poll_ack(ridx)) {
                        if (ridx == idx) {
                            acked = true;
                            std::cout << "[SOURCE] ACK received for chunk "
                                      << (int)idx+1 << "  (attempt " << tries << ")\n";
                        }
                        // else: stale ACK for an already-sent chunk — ignore.
                    } else {
                        std::this_thread::sleep_for(std::chrono::milliseconds(20));
                    }
                }

                if (!acked) {
                    ++retx;
                    if (max_attempts_ > 0 && tries >= max_attempts_) {
                        std::cout << "[SOURCE] GAVE UP on chunk " << (int)idx+1
                                  << " after " << tries << " attempts (no ACK)\n";
                        ++failed;
                        break;   // move on so the run can finish
                    }
                    std::cout << "[SOURCE] TIMEOUT on chunk " << (int)idx+1
                              << " — retransmitting\n";
                }
            }

            tries_per[idx] = tries;
            ok_per[idx]    = acked;
        }

        std::cout << "[SOURCE] Done. Sent=" << sent
                  << "  Retransmissions=" << retx
                  << "  Unacked chunks=" << failed << "\n";

        // Per-chunk summary: how many transmissions each chunk needed.
        std::cout << "\n[SOURCE] ===== Per-chunk transmission summary =====\n";
        for (uint8_t idx = 0; idx < total; ++idx) {
            std::cout << "[SOURCE]   chunk #" << (int)idx + 1 << "/" << (int)total
                      << " : tried " << tries_per[idx] << " time"
                      << (tries_per[idx] == 1 ? "" : "s") << "  ->  "
                      << (ok_per[idx] ? "ACKed" : "NOT ACKed (gave up)") << "\n";
        }
        std::cout << "[SOURCE] ==========================================\n";
        done_.store(true);
    }
};

// ─────────────────────────────────────────────────────────────
//  SINK
// ─────────────────────────────────────────────────────────────
class SINK {
public:
    // ack: ACK channel (RF or TCP) used to acknowledge each verified chunk.
    SINK(PHYSICAL_LAYER& phy, AckLink& ack, int ack_interval_ms)
        : phy_(phy), ack_(ack), ack_interval_ms_(ack_interval_ms)
    {}

    void start() {
        running_ = true;
        done_    = false;
        worker_  = std::thread(&SINK::run, this);
    }

    void stop() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

    // True once every chunk (per the header's total) has been received.
    bool done() const { return done_.load(); }

    void print_received_message() const {
        std::cout << "\n========== RECEIVED MESSAGE ==========\n";
        if (chunks_.empty()) {
            std::cout << "(nothing received)\n";
        } else {
            // Reassemble in order
            std::string full;
            for (size_t i = 0; i < chunks_.size(); i++) {
                auto it = chunks_.find(static_cast<uint8_t>(i));
                if (it != chunks_.end())
                    full += it->second;
                else
                    full += "[MISSING CHUNK " + std::to_string(i) + "]";
            }
            std::cout << full << "\n";
        }
        std::cout << "======================================\n";
        std::cout << "Received " << chunks_.size() << " chunk(s)\n";
    }

private:
    PHYSICAL_LAYER&  phy_;
    AckLink&         ack_;
    int              ack_interval_ms_;
    std::atomic<bool> running_{false};
    std::atomic<bool> done_{false};
    std::thread      worker_;
    std::map<uint8_t, std::string> chunks_;

    void run() {
        std::cout << "[SINK] Listening...\n";

        while (running_) {
            std::pair<size_t, std::vector<uint8_t>> rx;
            if (!phy_.rx_bits_fifo.pop(rx)) {
                std::this_thread::sleep_for(
                    std::chrono::milliseconds(ack_interval_ms_));
                continue;
            }

            auto [idx, tot, payload, crc_ok] = decode_packet_bits(rx.second);

            // Only accept (and ACK) error-free frames: a failed CRC means bit
            // errors, so drop it and let the source retransmit.
            if (!crc_ok || tot == 0 || idx >= tot) {
                std::this_thread::sleep_for(
                    std::chrono::milliseconds(ack_interval_ms_));
                continue;
            }

            std::cout << "[SINK] Received chunk " << (int)idx+1
                      << "/" << (int)tot
                      << "  payload_len=" << payload.size() << "  [CRC OK]\n";

            // Print printable characters only
            std::string clean;
            for (char c : payload)
                clean += (c >= 32 && c < 127) ? c : '?';
            std::cout << "[SINK] Content: \"" << clean << "\"\n";

            chunks_[idx] = payload;

            // Acknowledge over the ACK channel (RF or TCP). Sent even for
            // duplicates, in case a previous ACK was lost.
            ack_.send_ack(idx, tot);
            std::cout << "[SINK] ACK sent for chunk " << (int)idx+1
                      << "  via " << ack_.name() << "\n";

            // Check if all chunks received
            if (tot > 0 && chunks_.size() == static_cast<size_t>(tot)) {
                std::cout << "[SINK] All " << (int)tot << " chunks received!\n";
                print_received_message();
                // Keep running briefly so late duplicates still get ACKed (helps
                // the source close out its last chunk), then signal completion.
                done_.store(true);
            }
        }
        std::cout << "[SINK] Stopped\n";
    }
};
