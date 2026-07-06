// DEMO 1 — modulation round-trip (no hardware).
// Modulates random bits and demodulates them back for every scheme; reports bit
// errors. Absolute + differential-PSK + APSK + pi/4-QPSK must be error-free;
// differential-QAM is intentionally rejected at the thread level (constellation
// only). Proves the newly-wired schemes (16-APSK, 32-APSK, pi/4-QPSK) and the
// rebuilt 128-QAM work.
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include <random>
#include <cstdio>
static std::vector<uint8_t> rand_bits(int n, unsigned s){ std::mt19937 g(s);
    std::uniform_int_distribution<int> d(0,1); std::vector<uint8_t> b(n); for(auto&x:b)x=d(g); return b; }
static int bit_diff(const std::vector<uint8_t>&a,const std::vector<uint8_t>&b){
    int n=std::min(a.size(),b.size()),e=0; for(int i=0;i<n;i++)e+=(a[i]!=b[i]); return e; }
int main(){
    int fails=0;
    printf("--- absolute schemes: exact zero-noise loopback ---\n");
    for(auto s:{"BPSK","QPSK","8-PSK","16-QAM","32-QAM","64-QAM","128-QAM","256-QAM","16APSK","32APSK"}){
        Modulator m(string_to_mod_type(s)); int bps=m.get_bits_per_symbol(), cs=m.get_constellation_size();
        bool ok=(cs==(1<<bps)); auto bits=rand_bits(64*bps,1234);
        std::vector<std::complex<float>> pre={{1.f,0.f}}; bool add=false;
        int e=bit_diff(bits,m.demodulate(m.modulate(bits,pre,add)));
        printf("  %-9s bps=%d C=%3d %-9s errors=%d/%zu %s\n",s,bps,cs,ok?"":"(BADSIZE)",e,bits.size(),(ok&&e==0)?"PASS":"FAIL");
        if(!ok||e!=0)fails++;
    }
    printf("--- differential PSK: exact loopback (reference symbol prepended) ---\n");
    for(auto s:{"DBPSK","DQPSK","8-DPSK"}){
        Modulator m(string_to_mod_type(s)); int bps=m.get_bits_per_symbol(); auto bits=rand_bits(64*bps,777);
        std::complex<float> ref=m.get_constellation()[0]; std::vector<std::complex<float>> pre={ref}; bool add=false;
        auto enc=m.modulate(bits,pre,add); std::vector<std::complex<float>> rx={ref}; rx.insert(rx.end(),enc.begin(),enc.end());
        int e=bit_diff(bits,m.demodulate(rx));
        printf("  %-9s bps=%d errors=%d/%zu %s\n",s,bps,e,bits.size(),e==0?"PASS":"FAIL"); if(e!=0)fails++;
    }
    printf("--- pi/4-QPSK (reference symbol prepended) ---\n");
    { PI4QPSKModulator pi4; auto bits=rand_bits(128,99); auto enc=pi4.encode(bits);
      std::vector<std::complex<float>> rx={{1.f,0.f}}; rx.insert(rx.end(),enc.begin(),enc.end());
      pi4.reset(); int e=bit_diff(bits,pi4.decode(rx));
      printf("  %-9s     errors=%d/%zu %s\n","PI4-QPSK",e,bits.size(),e==0?"PASS":"FAIL"); if(e!=0)fails++; }
    printf("--- differential QAM: constellation-only (thread-level rejected) ---\n");
    for(auto s:{"DQAM16","DQAM32","DQAM64","DQAM128","DQAM256"}){
        Modulator m(string_to_mod_type(s)); int bps=m.get_bits_per_symbol(), cs=m.get_constellation_size();
        printf("  %-9s bps=%d C=%3d constellation %s (round-trip intentionally unsupported)\n",s,bps,cs,cs==(1<<bps)?"OK":"BAD");
        if(cs!=(1<<bps))fails++; }
    printf("\n==== %s ====\n", fails==0?"ALL PASS":"SOME FAIL");
    return fails;
}
