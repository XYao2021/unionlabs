#pragma once
// ============================================================
//  ack_transport.hpp
//  Pluggable ACK channel for the stop-and-wait ARQ (SOURCE/SINK).
//  The DATA always travels over RF (RF A); only the small ACK can
//  use a different transport:
//    - RfAckLink : ACK over a second RF path (RF B) — needs the
//                  reverse cable/antenna and full-duplex on the box.
//    - TcpAckLink: ACK over a TCP/IP socket (e.g. localhost when both
//                  radios are on one host) — no reverse RF needed.
//  SINK calls send_ack(); SOURCE calls poll_ack() (non-blocking).
// ============================================================
#include <cstdint>
#include <string>
#include <vector>

#include "physical_layer.hpp"
#include "messages.hpp"
#include "net.hpp"
#include "fec.hpp"

// ── Abstract ACK link ────────────────────────────────────────
class AckLink {
public:
    virtual ~AckLink() {}
    // SINK side: announce that chunk `idx` (of `tot`) was received error-free.
    virtual void send_ack(uint8_t idx, uint8_t tot) = 0;
    // SOURCE side: non-blocking. Returns true and sets `idx` if an ACK is ready.
    virtual bool poll_ack(uint8_t& idx) = 0;
    virtual const char* name() const = 0;
};

// ── ACK over RF (RF B pipeline) ──────────────────────────────
// The ACK is sent as a full-size data frame (padded) so it matches the length
// the source's RX pipeline extracts; poll_ack pops CRC-verified frames from the
// RF rx FIFO. Data direction stays on the caller's other RF path.
class RfAckLink : public AckLink {
public:
    // fec: if the data is FEC-coded, the ACK frame must be too, so it is the same
    // length as a data frame (the source's ACK-RX pipeline extracts a fixed
    // recv_msg_len sized for the coded data chunk).
    RfAckLink(PHYSICAL_LAYER& phy, size_t ack_payload_bytes, bool fec = false)
        : phy_(phy), ack_payload_bytes_(ack_payload_bytes), fec_(fec) {}

    void send_ack(uint8_t idx, uint8_t tot) override {
        auto bits = build_packet_bits(std::string(ack_payload_bytes_, ' '), idx, tot);
        if (fec_) bits = fec_encode_block(bits);
        phy_.transmit(bits);
    }

    bool poll_ack(uint8_t& idx) override {
        std::pair<size_t, std::vector<uint8_t>> rx;
        if (!phy_.rx_bits_fifo.pop(rx)) return false;
        std::vector<uint8_t> raw = rx.second;
        if (fec_) {
            int info_len = 16 + (int)ack_payload_bytes_ * 8 + 16;
            int coded    = fec_encoded_len(info_len);
            if ((int)raw.size() >= coded) raw.resize(coded);
            raw = fec_decode_block(raw, info_len);
        }
        auto [ridx, rtot, rpayload, rok] = decode_packet_bits(raw);
        if (!rok) return false;              // corrupted → not a valid ACK
        idx = ridx;
        return true;
    }

    const char* name() const override { return "RF (RF B)"; }

private:
    PHYSICAL_LAYER& phy_;
    size_t          ack_payload_bytes_;
    bool            fec_;
};

// ── ACK over TCP/IP ──────────────────────────────────────────
// A 2-byte message [idx, tot] per ACK. send is blocking (tiny), poll is
// non-blocking and buffers partial reads so it never stalls the ARQ loop.
class TcpAckLink : public AckLink {
public:
    explicit TcpAckLink(int fd) : fd_(fd) {}
    ~TcpAckLink() override { if (fd_ >= 0) ::close(fd_); }

    void send_ack(uint8_t idx, uint8_t tot) override {
        uint8_t msg[2] = { idx, tot };
        try { net::send_all(fd_, msg, sizeof(msg)); }
        catch (const std::exception& e) {
            std::cerr << "[ACK/TCP] send failed: " << e.what() << "\n";
        }
    }

    bool poll_ack(uint8_t& idx) override {
        uint8_t tmp[64];
        int r = net::recv_avail(fd_, tmp, sizeof(tmp));
        for (int i = 0; i < r; i++) buf_.push_back(tmp[i]);
        if (buf_.size() >= 2) {
            idx = buf_[0];                   // buf_[1] = tot (unused here)
            buf_.erase(buf_.begin(), buf_.begin() + 2);
            return true;
        }
        return false;
    }

    const char* name() const override { return "TCP/IP"; }

private:
    int                  fd_;
    std::vector<uint8_t> buf_;
};
