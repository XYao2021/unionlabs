// ============================================================
//  tx_app — TRANSMIT terminal (no radio).
//  Modulates a text message with a chosen scheme and streams the
//  complex baseband symbols to the RX terminal over a localhost
//  TCP socket. Framing per chunk: [guard(10) | preamble | data],
//  where data = modulate( header(16 bits) + payload bits ).
//
//  Usage:
//    ./tx_app --scheme QPSK [--m 5] [--host 127.0.0.1] [--port 5555]
//             [--payload-bytes 125] [--message "..."] [--msg-file FILE]
// ============================================================
#include "modulator.hpp"
#include "modulator_extended.hpp"
#include "messages.hpp"
#include "net.hpp"
#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <cstring>

static const char* DEFAULT_MESSAGE =
    "It is a period of civil war.\n"
    "Rebel spaceships, striking\n"
    "from a hidden base, have won\n"
    "their first victory against\n"
    "the evil Galactic Empire.\n"
    "\n"
    "During the battle, Rebel\n"
    "spies managed to steal secret\n"
    "plans to the Empire's\n"
    "ultimate weapon, the DEATH\n"
    "STAR, an armored space\n"
    "station with enough power to\n"
    "destroy an entire planet.\n"
    "\n"
    "Pursued by the Empire's\n"
    "sinister agents, Princess\n"
    "Leia races home aboard her\n"
    "starship, custodian of the\n"
    "stolen plans that can save\n"
    "her people and restore\n"
    "freedom to the galaxy....";

static std::string arg_str(int argc,char**argv,const std::string&k,const std::string&d){
    for(int i=1;i<argc-1;i++) if(k==argv[i]) return argv[i+1]; return d;
}
static int arg_int(int argc,char**argv,const std::string&k,int d){
    std::string v=arg_str(argc,argv,k,""); return v.empty()?d:std::stoi(v);
}

int main(int argc, char** argv){
    std::string scheme = arg_str(argc,argv,"--scheme","QPSK");
    int    m           = arg_int(argc,argv,"--m",5);
    std::string host   = arg_str(argc,argv,"--host","127.0.0.1");
    int    port        = arg_int(argc,argv,"--port",5555);
    int    pbytes      = arg_int(argc,argv,"--payload-bytes",125);
    std::string msg    = arg_str(argc,argv,"--message","");
    std::string mfile  = arg_str(argc,argv,"--msg-file","");

    if (msg.empty() && !mfile.empty()) {
        std::ifstream f(mfile); std::stringstream ss; ss<<f.rdbuf(); msg=ss.str();
    }
    if (msg.empty()) msg = DEFAULT_MESSAGE;

    // Resolve scheme; reject the ones this two-terminal app doesn't carry.
    ModulationType mt;
    try { mt = string_to_mod_type(scheme); }
    catch(const std::exception& e){ std::cerr<<"[TX] "<<e.what()<<"\n"; return 1; }
    if (mt==ModulationType::PI4QPSK){
        std::cerr<<"[TX] PI4-QPSK isn't wired into this two-terminal demo; use the "
                   "loopback demo (tests/mod_loopback_test) for it.\n"; return 1; }
    if (mt==ModulationType::DQAM16||mt==ModulationType::DQAM32||mt==ModulationType::DQAM64||
        mt==ModulationType::DQAM128||mt==ModulationType::DQAM256){
        std::cerr<<"[TX] Differential QAM is not supported (multiply-differential can't "
                   "carry QAM amplitude). Use absolute QAM or a differential PSK scheme.\n";
        return 1; }

    Modulator mod(mt);
    auto preamble = generate_msequence_preamble(m);
    int  P = static_cast<int>(preamble.size());
    int  bps = mod.get_bits_per_symbol();

    auto chunks = split_message_into_chunks(msg, pbytes);
    std::cout << "\n================ TX ================\n";
    std::cout << "[TX] Scheme        : " << mod.get_modulation_name()
              << "  (" << bps << " bits/symbol, C=" << mod.get_constellation_size() << ")\n";
    std::cout << "[TX] Preamble      : m-sequence m=" << m << " (" << P << " symbols) + 10 guard\n";
    std::cout << "[TX] Message       : " << msg.size() << " bytes\n";
    std::cout << "[TX] Chunking      : " << chunks.size() << " chunk(s) of up to "
              << pbytes << " bytes\n";
    std::cout << "[TX] Connecting to : " << host << ":" << port << " ...\n";

    int fd;
    try { fd = net::connect_to(host, port); }
    catch(const std::exception& e){ std::cerr<<"[TX] "<<e.what()<<"\n"; return 1; }
    std::cout << "[TX] Connected. Transmitting...\n";

    for (size_t i=0;i<chunks.size();++i){
        // header(16) + payload bits, then modulate → [guard|preamble|data]
        auto bits = build_packet_bits(chunks[i], (uint8_t)i, (uint8_t)chunks.size());
        bool add = true;
        auto pkt = mod.modulate(bits, preamble, add);
        int num_data = (int)pkt.size() - 10 - P;

        net::send_i32(fd, 1);                    // data-packet tag
        net::send_i32(fd, (int32_t)i);           // chunk index
        net::send_i32(fd, (int32_t)chunks.size());
        net::send_str(fd, scheme);               // for RX sanity check
        net::send_i32(fd, m);
        net::send_str(fd, chunks[i]);            // demo ground-truth payload (for BER)
        net::send_symbols(fd, pkt);              // clean modulated packet

        std::cout << "[TX] chunk " << i << ": " << chunks[i].size() << " bytes -> "
                  << bits.size() << " bits -> " << num_data << " data symbols, packet "
                  << pkt.size() << " symbols sent\n";
    }
    net::send_i32(fd, 0);                         // end-of-stream tag
    std::cout << "[TX] Done. Sent " << chunks.size() << " packet(s).\n";
    ::close(fd);
    return 0;
}
