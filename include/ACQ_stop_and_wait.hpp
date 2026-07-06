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

// ─────────────────────────────────────────────────────────────
//  SOURCE
// ─────────────────────────────────────────────────────────────
class SOURCE {
public:
    SOURCE(PHYSICAL_LAYER& phy, int timeout_ms, int num_bits_per_packet)
        : phy_(phy), timeout_ms_(timeout_ms),
          num_bits_(num_bits_per_packet)
    {}

    void start(const std::vector<std::string>& chunks) {
        chunks_  = chunks;
        running_ = true;
        worker_  = std::thread(&SOURCE::run, this);
    }

    void stop() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

private:
    PHYSICAL_LAYER&      phy_;
    int                  timeout_ms_;
    int                  num_bits_;
    std::vector<std::string> chunks_;
    std::atomic<bool>    running_{false};
    std::thread          worker_;

    void run() {
        uint8_t total = static_cast<uint8_t>(chunks_.size());
        size_t  sent  = 0;
        size_t  retx  = 0;

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
                    std::pair<size_t, std::vector<uint8_t>> rx;
                    if (phy_.rx_bits_fifo.pop(rx)) {
                        // Any received frame with matching index is treated as ACK
                        auto [ridx, rtot, rpayload] = decode_packet_bits(rx.second);
                        if (ridx == idx) {
                            acked = true;
                            std::cout << "[SOURCE] ACK received for chunk "
                                      << (int)idx+1 << "\n";
                        }
                    } else {
                        std::this_thread::sleep_for(std::chrono::milliseconds(50));
                    }
                }

                if (!acked) {
                    std::cout << "[SOURCE] TIMEOUT on chunk " << (int)idx+1
                              << " — retransmitting\n";
                    ++retx;
                }
            }
        }

        std::cout << "[SOURCE] Done. Sent=" << sent
                  << "  Retransmissions=" << retx << "\n";
    }
};

// ─────────────────────────────────────────────────────────────
//  SINK
// ─────────────────────────────────────────────────────────────
class SINK {
public:
    SINK(PHYSICAL_LAYER& phy, int ack_interval_ms)
        : phy_(phy), ack_interval_ms_(ack_interval_ms)
    {}

    void start() {
        running_ = true;
        worker_  = std::thread(&SINK::run, this);
    }

    void stop() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

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
    int              ack_interval_ms_;
    std::atomic<bool> running_{false};
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

            auto [idx, tot, payload] = decode_packet_bits(rx.second);

            std::cout << "[SINK] Received chunk " << (int)idx+1
                      << "/" << (int)tot
                      << "  payload_len=" << payload.size() << "\n";

            // Print printable characters only
            std::string clean;
            for (char c : payload)
                clean += (c >= 32 && c < 127) ? c : '?';
            std::cout << "[SINK] Content: \"" << clean << "\"\n";

            chunks_[idx] = payload;

            // Send ACK: re-encode the same header with empty payload
            auto ack_bits = build_packet_bits("", idx, tot);
            phy_.transmit(ack_bits);
            std::cout << "[SINK] ACK sent for chunk " << (int)idx+1 << "\n";

            // Check if all chunks received
            if (tot > 0 && chunks_.size() == static_cast<size_t>(tot)) {
                std::cout << "[SINK] All " << (int)tot << " chunks received!\n";
                print_received_message();
            }
        }
        std::cout << "[SINK] Stopped\n";
    }
};
