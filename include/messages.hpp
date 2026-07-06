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

// Build a full packet bit-stream: header(16) + payload bits
inline std::vector<uint8_t>
build_packet_bits(const std::string& payload_str,
                  uint8_t chunk_index, uint8_t total_chunks)
{
    auto header  = encode_header(chunk_index, total_chunks);
    auto payload = string_to_bits(payload_str);
    header.insert(header.end(), payload.begin(), payload.end());
    return header;
}

// Decode a received bit-stream into header + payload string
// Returns {chunk_index, total_chunks, payload_string}
inline std::tuple<uint8_t, uint8_t, std::string>
decode_packet_bits(const std::vector<uint8_t>& bits)
{
    if (bits.size() < 16)
        return {0, 0, ""};
    auto [idx, tot] = decode_header(bits);
    std::vector<uint8_t> payload_bits(bits.begin() + 16, bits.end());
    std::string payload = bits_to_string(payload_bits);
    return {idx, tot, payload};
}
