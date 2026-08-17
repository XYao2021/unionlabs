// fec_latency.cpp — per-packet decode-time distribution at a fixed Eb/N0.
// Decodes the SAME 1032-bit packet many times (fresh AWGN each rep) with each
// soft decoder and records the individual decode-call times. Reveals the shape
// of the latency: Viterbi is a fixed spike (constant trellis); LDPC and turbo
// are spread out (iteration count varies with the noise realization).
// Writes a long-format CSV: decoder,us  -> plot with tools/plot_fec_latency.py
#include "messages.hpp"
#include "fec.hpp"
#include <random>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <fstream>
using clk = std::chrono::steady_clock;
static double us(clk::duration d){ return std::chrono::duration<double,std::micro>(d).count(); }

int main(int argc, char** argv){
    double ebn0_db = (argc>1)? std::atof(argv[1]) : 3.0;
    int    REPS    = (argc>2)? std::atoi(argv[2]) : 1500;
    std::string csv= (argc>3)? argv[3] : "fec_latency.csv";
    size_t bytes=125;

    std::string payload(bytes,'0'); for(size_t i=0;i<bytes;i++) payload[i]=char('0'+i%10);
    auto pkt=build_packet_bits(payload,2,5); int info_len=(int)pkt.size();
    double R=0.5, ebn0=std::pow(10.0,ebn0_db/10.0), sigma=std::sqrt(1.0/(2.0*R*ebn0));
    std::mt19937 g(7); std::normal_distribution<float> noise(0.f,(float)sigma);

    std::ofstream cf(csv); cf<<"decoder,us\n";
    printf("fec_latency @ %.1f dB Eb/N0, %d reps, %d-bit packet\n", ebn0_db, REPS, info_len);

    struct Dec { const char* name; const char* type; };
    for (Dec dec : { Dec{"conv (Viterbi)","conv"}, Dec{"LDPC (min-sum)","ldpc"}, Dec{"turbo (BCJR)","turbo"} }) {
        fec_set_type(dec.type, 256);
        auto coded = fec_encode_block(pkt);
        std::vector<float> llr(coded.size());
        double sum=0, mn=1e30, mx=0;
        for(int r=0;r<REPS;++r){
            for(size_t i=0;i<coded.size();++i){ float s=coded[i]?-1.f:1.f, y=s+noise(g);
                llr[i]=2.f*y/(float)(sigma*sigma); }
            auto a=clk::now(); auto d=fec_soft_decode_block(llr,info_len); auto t=us(clk::now()-a);
            (void)d;
            cf<<dec.name<<","<<t<<"\n";
            sum+=t; mn=std::min(mn,t); mx=std::max(mx,t);
        }
        printf("  %-16s mean=%7.1f us  min=%7.1f  max=%7.1f  spread=%.1fx\n",
               dec.name, sum/REPS, mn, mx, mx/std::max(1e-9,mn));
    }
    cf.close();
    printf("[csv] wrote %s\n", csv.c_str());
    return 0;
}
