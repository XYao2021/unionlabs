// Framing + modulate/demodulate round-trip for each scheme (no RF).
// Checks: does build_packet_bits -> modulate -> demodulate -> decode_packet_bits
// recover the exact payload and pass CRC? Isolates framing/bit-packing bugs from
// the RF front-end. Also prints data-symbol counts and derived sizes.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include <cstdio>
#include <string>
using cf = std::complex<float>;

int main() {
    const size_t bytes_length = 125;
    std::string payload(bytes_length, 'A');
    for (size_t i = 0; i < bytes_length; i++) payload[i] = char('0' + (i % 10));
    const int m = 5;
    auto pre = generate_msequence_preamble(m);
    int P = (int)pre.size();

    const char* schemes[] = {"BPSK","QPSK","8-PSK","16-QAM","32-QAM","64-QAM","128-QAM","256-QAM"};
    for (auto s : schemes) {
        ModulationType mt;
        try { mt = string_to_mod_type(s); } catch (...) { printf("%-8s : unknown\n", s); continue; }
        Modulator mod(mt);
        int bps = mod.get_bits_per_symbol();

        auto tx_bits = build_packet_bits(payload, 0, 5);          // header+payload+CRC
        int packet_bits = (int)tx_bits.size();
        int data_syms   = (packet_bits + bps - 1) / bps;         // main.cpp auto-size

        bool add = true; auto pre_copy = pre;
        auto syms = mod.modulate(tx_bits, pre_copy, add);        // [guard|preamble|data]
        int total_syms = (int)syms.size();
        int data_only  = total_syms - 10 - P;                    // guard(10)+preamble(P)

        // RX side: strip guard+preamble, take exactly data_syms symbols (as the
        // real pipeline's ACQ extraction does), demod, decode.
        std::vector<cf> data(syms.begin() + 10 + P, syms.end());
        // pipeline extracts exactly recv_msg_len = data_syms symbols:
        if ((int)data.size() > data_syms) data.resize(data_syms);
        auto rx_bits = mod.demodulate(data);
        auto [idx, tot, rpayload, crc_ok] = decode_packet_bits(rx_bits);

        bool len_ok = (rpayload.size() == bytes_length);
        bool pay_ok = (rpayload == payload);
        printf("%-8s bps=%d | packet_bits=%d data_syms=%d | modulated data-only=%d "
               "| rx_bits=%zu payload_len=%zu CRC=%s %s%s\n",
               s, bps, packet_bits, data_syms, data_only,
               rx_bits.size(), rpayload.size(),
               crc_ok ? "OK" : "FAIL",
               len_ok ? "" : "[LEN MISMATCH] ",
               (pay_ok && crc_ok) ? "[OK]" : "[BAD]");
    }
    return 0;
}
