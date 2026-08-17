// OFDM pipeline thread test: build_packet_bits -> ofdm_modulation_thread ->
// [pad + multipath + CFO + AWGN + AGC-normalize] -> ofdm_demodulation_thread ->
// decode_packet_bits. Validates the real pipeline threads + CRC framing through
// a channel, before hardware.
#include "messages.hpp"
#include "ofdm_pipeline.hpp"
#include <random>
#include <thread>
#include <cstdio>
using cf = std::complex<float>;
using Blk = std::pair<size_t, std::vector<cf>>;

static std::vector<cf> multipath(const std::vector<cf>& x, const std::vector<cf>& h){
    std::vector<cf> y(x.size(), cf(0,0));
    for (size_t n=0;n<x.size();++n) for (size_t k=0;k<h.size()&&k<=n;++k) y[n]+=h[k]*x[n-k];
    return y;
}

static void run(const char* scheme, const std::vector<cf>& h, float noise, float cfo_sc){
    std::string sch = scheme;
    int fft=64, cp=16;
    size_t bytes_length=125;
    std::string payload(bytes_length,'0'); for(size_t i=0;i<bytes_length;i++) payload[i]=char('0'+i%10);
    auto tx_bits = build_packet_bits(payload, 2, 5);
    Modulator probe(string_to_mod_type(scheme));
    int bps=probe.get_bits_per_symbol();
    int data_syms=((int)tx_bits.size()+bps-1)/bps;      // num_qam

    std::atomic<bool> stop{false};
    MutexFIFO<std::vector<uint8_t>> bits_fifo;
    MutexFIFO<Blk> frame_fifo, burst_fifo;
    MutexFIFO<std::pair<size_t,std::vector<uint8_t>>> out_fifo;

    // TX thread
    bits_fifo.push(tx_bits);
    std::thread tx(ofdm_modulation_thread, std::ref(bits_fifo), std::ref(frame_fifo),
        std::ref(sch), fft, cp, 0.5f, std::ref(stop));
    Blk frame; for(int i=0;i<500 && !frame_fifo.pop(frame); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    stop.store(true); tx.join(); stop.store(false);
    if (frame.second.empty()){ printf("%-7s no frame\n",scheme); return; }

    // channel: lead-in pad + multipath + CFO + AWGN, then AGC-normalize RMS->1
    std::mt19937 g(5); std::normal_distribution<float> nz(0.f,1.f);
    auto ch = multipath(frame.second, h);
    double fn=(double)cfo_sc/fft;
    for(size_t n=0;n<ch.size();++n) ch[n]*=cf(std::cos(2*M_PI*fn*n),std::sin(2*M_PI*fn*n));
    std::vector<cf> burst;
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double pw=0; for(auto&s:ch) pw+=std::norm(s); float rms=std::sqrt(pw/ch.size());
    for(auto&s:ch) burst.push_back(s + cf(noise*rms*nz(g), noise*rms*nz(g)));
    for(int i=0;i<40;i++) burst.push_back(cf(0.01f*nz(g),0.01f*nz(g)));
    double bp=0; for(auto&s:burst) bp+=std::norm(s); float brms=std::sqrt(bp/burst.size());
    for(auto&s:burst) s/=brms;                          // mimic FeedforwardAGC
    burst_fifo.push({0,burst});

    // RX thread
    std::thread rx(ofdm_demodulation_thread, std::ref(burst_fifo), std::ref(out_fifo),
        std::ref(sch), fft, cp, data_syms, std::ref(stop),
        (MutexFIFO<std::pair<size_t,std::vector<float>>>*)nullptr);   // no soft-LLR output
    std::pair<size_t,std::vector<uint8_t>> res; bool got=false;
    for(int i=0;i<1000;i++){ if(out_fifo.pop(res)){got=true;break;} std::this_thread::sleep_for(std::chrono::milliseconds(2)); }
    stop.store(true); rx.join();
    if(!got){ printf("%-7s no output\n",scheme); return; }

    auto [idx,tot,pl,crc] = decode_packet_bits(res.second);
    printf("%-7s | data_syms=%d bits=%zu | idx=%d tot=%d CRC=%s %s\n",
        scheme, data_syms, res.second.size(), (int)idx,(int)tot,
        crc?"OK":"FAIL", (crc && pl==payload)?"[OK]":"[BAD]");
}

int main(){
    std::vector<cf> h = { cf(1,0), cf(0.4f,0.2f), cf(0.2f,-0.1f) };
    printf("=== OFDM pipeline threads through multipath+CFO+noise ===\n");
    for (auto s : {"QPSK","16-QAM","64-QAM"}) run(s, h, 0.02f, 0.25f);
    return 0;
}
