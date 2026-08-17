// FEC round-trip: build_packet_bits -> fec_encode -> inject BER -> fec_decode ->
// decode_packet_bits. Shows FEC turns a noisy bit stream into an error-free
// (CRC-OK) frame where uncoded fails. Averaged over many trials per BER.
#include "messages.hpp"
#include "fec.hpp"
#include <random>
#include <cstdio>

int main(){
    const size_t bytes_length=125;
    std::string payload(bytes_length,'0'); for(size_t i=0;i<bytes_length;i++) payload[i]=char('0'+i%10);
    auto pkt = build_packet_bits(payload, 2, 5);
    auto coded = fec_encode_block(pkt);
    printf("packet=%zu bits -> FEC coded=%zu bits (rate 1/2)\n", pkt.size(), coded.size());

    std::mt19937 g(1);
    printf("\n%-8s | uncoded CRC-OK rate | FEC CRC-OK rate  (200 trials each)\n","BER");
    for (double ber : {0.005, 0.01, 0.02, 0.03, 0.05, 0.08}) {
        int trials=200, unc_ok=0, fec_ok=0;
        std::bernoulli_distribution flip(ber);
        for (int t=0;t<trials;t++){
            // uncoded: flip bits in pkt, CRC check
            auto u = pkt; for(auto&b:u) if(flip(g)) b^=1;
            auto [ui,ut,up,uc]=decode_packet_bits(u);
            if (uc && up==payload) unc_ok++;
            // FEC: flip bits in coded, decode, CRC check
            auto c = coded; for(auto&b:c) if(flip(g)) b^=1;
            auto d = fec_decode_block(c);
            auto [di,dt,dp,dc]=decode_packet_bits(d);
            if (dc && dp==payload) fec_ok++;
        }
        printf("%-8.3f | %6.1f%%              | %6.1f%%\n",
            ber, 100.0*unc_ok/trials, 100.0*fec_ok/trials);
    }
    return 0;
}
