#pragma once
// ============================================================
//  messages.hpp
//  Preamble generation (m-sequence, Zadoff-Chu) and message
//  framing utilities used throughout the project.
// ============================================================
#include <complex>
#include <vector>
#include <string>
#include <cmath>
#include <stdexcept>
#include <iostream>
#include <sstream>
#include <cstdint>

// ─────────────────────────────────────────────────────────────
//  M-sequence preamble (BPSK-modulated, ±1)
//  m : shift-register length; sequence length = 2^m - 1
//  Returns complex float symbols with imag = 0.
// ─────────────────────────────────────────────────────────────
inline std::vector<std::complex<float>> generate_msequence_preamble(int m)
{
    if (m < 2 || m > 20)
        throw std::invalid_argument("[msequence] m must be 2–20");

    // Primitive polynomials (feedback taps) for common m values
    // Indexed by m; value is the feedback polynomial as a bitmask (excluding x^m)
    static const int poly[] = {
        0, 0,
        0x3,   // m=2: x^2+x+1
        0x6,   // m=3: x^3+x^2+1
        0xC,   // m=4: x^4+x^3+1
        0x14,  // m=5: x^5+x^3+1      → length 31
        0x30,  // m=6: x^6+x^5+1      → length 63
        0x60,  // m=7: x^7+x^6+1      → length 127
        0xB8,  // m=8
        0x110, // m=9
        0x240, // m=10
    };

    int feedback = (m <= 10) ? poly[m] : (1 << (m - 1)) | 1;
    int state    = 1;
    int len      = (1 << m) - 1;

    std::vector<std::complex<float>> seq;
    seq.reserve(len);
    for (int i = 0; i < len; i++) {
        int bit = state & 1;
        seq.push_back(std::complex<float>(bit ? 1.0f : -1.0f, 0.0f));

        // Fibonacci LFSR
        int feedback_bit = __builtin_popcount(state & feedback) & 1;
        state = ((state >> 1) | (feedback_bit << (m - 1)));
    }
    std::cout << "[Preamble] M-sequence length " << m
              << " → " << len << " symbols\n";
    return seq;
}

// ─────────────────────────────────────────────────────────────
//  Zadoff-Chu preamble
//  root : ZC root (must be coprime with seq_len)
//  seq_len : sequence length (odd prime recommended)
// ─────────────────────────────────────────────────────────────
inline std::vector<std::complex<float>>
generate_zadoff_chu_preamble(int root, int seq_len)
{
    std::vector<std::complex<float>> seq(seq_len);
    for (int n = 0; n < seq_len; n++) {
        float phase = -static_cast<float>(M_PI) * root * n * (n + 1)
                      / seq_len;
        seq[n] = std::polar(1.0f, phase);
    }
    std::cout << "[Preamble] Zadoff-Chu root=" << root
              << " length=" << seq_len << "\n";
    return seq;
}

// ─────────────────────────────────────────────────────────────
//  Dispatcher: generate preamble by type string and parameter
// ─────────────────────────────────────────────────────────────
inline std::vector<std::complex<float>>
generate_preamble(const std::string& type, int param)
{
    if (type == "m-sequence" || type == "msequence" || type == "m_sequence")
        return generate_msequence_preamble(param);
    else if (type == "zadoff" || type == "zadoff-chu" || type == "zc")
        return generate_zadoff_chu_preamble(param, 63);  // default ZC length
    else if (type == "None" || type == "none" || type.empty())
        return generate_msequence_preamble(5);           // fallback
    else
        throw std::invalid_argument("[generate_preamble] Unknown type: " + type);
}

// ─────────────────────────────────────────────────────────────
//  Message framing utilities
// ─────────────────────────────────────────────────────────────

// Convert a string to a bit vector (MSB first per byte)
inline std::vector<uint8_t> string_to_bits(const std::string& s)
{
    std::vector<uint8_t> bits;
    bits.reserve(s.size() * 8);
    for (unsigned char c : s)
        for (int b = 7; b >= 0; b--)
            bits.push_back((c >> b) & 1);
    return bits;
}

// Convert a bit vector back to a string (MSB first per byte)
inline std::string bits_to_string(const std::vector<uint8_t>& bits)
{
    std::string s;
    size_t n = (bits.size() / 8) * 8;
    for (size_t i = 0; i < n; i += 8) {
        unsigned char c = 0;
        for (int b = 0; b < 8; b++)
            c = (c << 1) | (bits[i + b] & 1);
        s += static_cast<char>(c);
    }
    return s;
}

// Split a long string into chunks of at most bytes_per_chunk bytes
inline std::vector<std::string>
split_message_into_chunks(const std::string& msg, size_t bytes_per_chunk)
{
    std::vector<std::string> chunks;
    for (size_t i = 0; i < msg.size(); i += bytes_per_chunk)
        chunks.push_back(msg.substr(i, bytes_per_chunk));
    return chunks;
}

// ─────────────────────────────────────────────────────────────
//  CRC-16-CCITT (poly 0x1021, init 0xFFFF) over a byte sequence.
//  Used to detect bit errors in a received packet so the RX can accept
//  only error-free chunks (CRC-verified collection). ~1/65536 chance a
//  corrupted frame slips through.
// ─────────────────────────────────────────────────────────────
inline uint16_t crc16_ccitt(const std::vector<uint8_t>& bytes)
{
    uint16_t crc = 0xFFFF;
    for (uint8_t b : bytes) {
        crc ^= static_cast<uint16_t>(b) << 8;
        for (int i = 0; i < 8; i++)
            crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                                 : static_cast<uint16_t>(crc << 1);
    }
    return crc;
}

// ─────────────────────────────────────────────────────────────
//  Simple 16-bit header: encodes chunk index and total chunks
//  Layout: [8-bit chunk_index][8-bit total_chunks]
// ─────────────────────────────────────────────────────────────
inline std::vector<uint8_t>
encode_header(uint8_t chunk_index, uint8_t total_chunks)
{
    std::vector<uint8_t> header;
    for (int b = 7; b >= 0; b--) header.push_back((chunk_index  >> b) & 1);
    for (int b = 7; b >= 0; b--) header.push_back((total_chunks >> b) & 1);
    return header;  // 16 bits
}

inline std::pair<uint8_t, uint8_t>
decode_header(const std::vector<uint8_t>& bits)
{
    if (bits.size() < 16) return {0, 0};
    uint8_t idx = 0, tot = 0;
    for (int b = 0; b < 8; b++) idx  = (idx  << 1) | bits[b];
    for (int b = 0; b < 8; b++) tot  = (tot  << 1) | bits[8 + b];
    return {idx, tot};
}

// Build a full packet bit-stream: header(16) + payload bits + CRC-16.
// Frame layout (bits, MSB first per field):
//   [ idx(8) | tot(8) | payload(8*P) | crc16(16) ]
// The CRC is computed over the frame BYTES [idx, tot, payload...] so the RX
// can verify the whole frame. Signature is unchanged, so callers are unaffected.
inline std::vector<uint8_t>
build_packet_bits(const std::string& payload_str,
                  uint8_t chunk_index, uint8_t total_chunks)
{
    // Frame bytes for the CRC: idx, tot, then the payload characters.
    std::vector<uint8_t> frame_bytes;
    frame_bytes.reserve(2 + payload_str.size());
    frame_bytes.push_back(chunk_index);
    frame_bytes.push_back(total_chunks);
    for (unsigned char c : payload_str) frame_bytes.push_back(c);
    uint16_t crc = crc16_ccitt(frame_bytes);

    std::vector<uint8_t> bits;
    bits.reserve(16 + payload_str.size() * 8 + 16);
    auto header  = encode_header(chunk_index, total_chunks);   // 16 bits
    bits.insert(bits.end(), header.begin(), header.end());
    auto payload = string_to_bits(payload_str);                // 8*P bits
    bits.insert(bits.end(), payload.begin(), payload.end());
    for (int b = 15; b >= 0; b--) bits.push_back((crc >> b) & 1);  // 16 CRC bits
    return bits;
}

// Decode a received bit-stream into header + payload + CRC-valid flag.
// Returns {chunk_index, total_chunks, payload_string, crc_ok}.
// The payload length is derived from the bit count: header(16)+crc(16)=32 bits
// of overhead, so payload_bytes = (bits.size()-32)/8. Any trailing symbol
// padding (< 8 bits for bits/symbol <= 8) floors away correctly.
inline std::tuple<uint8_t, uint8_t, std::string, bool>
decode_packet_bits(const std::vector<uint8_t>& bits)
{
    if (bits.size() < 32)                       // need header + CRC at minimum
        return {0, 0, "", false};

    int payload_bytes = (static_cast<int>(bits.size()) - 32) / 8;
    if (payload_bytes < 0) return {0, 0, "", false};

    auto [idx, tot] = decode_header(bits);

    // Payload bytes (MSB first), and rebuild the frame-byte sequence for the CRC.
    std::vector<uint8_t> frame_bytes;
    frame_bytes.push_back(idx);
    frame_bytes.push_back(tot);
    std::string payload;
    payload.reserve(payload_bytes);
    for (int i = 0; i < payload_bytes; i++) {
        unsigned char c = 0;
        for (int b = 0; b < 8; b++) c = (c << 1) | (bits[16 + i * 8 + b] & 1);
        frame_bytes.push_back(c);
        payload += static_cast<char>(c);
    }

    // Received CRC sits right after the payload (before any symbol padding).
    int crc_off = 16 + payload_bytes * 8;
    uint16_t rx_crc = 0;
    for (int b = 0; b < 16; b++) rx_crc = (rx_crc << 1) | (bits[crc_off + b] & 1);

    bool crc_ok = (crc16_ccitt(frame_bytes) == rx_crc);
    return {idx, tot, payload, crc_ok};
}
