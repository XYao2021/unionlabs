// ============================================================
//  main.cpp — QPSK transceiver test entry point
//  Supports --mode source (TX) and --mode sink (RX).
//  All PHY parameters are set via command-line flags.
// ============================================================

#include <boost/program_options.hpp>
#include <boost/format.hpp>
#include <uhd/utils/safe_main.hpp>

#include <iostream>
#include <csignal>
#include <thread>
#include <chrono>
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>

#include "physical_layer.hpp"
#include "ACQ_stop_and_wait.hpp"

namespace po = boost::program_options;

// ─────────────────────────────────────────────────────────────
int UHD_SAFE_MAIN(int argc, char* argv[])
{
    // ── Parameter declarations ──────────────────────────────
    PHYSICAL_CONFIG config;

    std::string mode;
    int         timeout_ms      = 3000;
    int         timer_interval  = 1000;
    int         num_bits        = 1000;
    int         interval_ms     = 3000;
    int         tx_reps         = 20;      // one-way (role tx) repetitions
    double      rx_idle_timeout = 8.0;     // role rx: auto-stop after N s of no bursts
    bool        skip_rate_check = false;   // bypass the rate-chain consistency check
    bool        continuous      = false;
    std::string preamble_type;
    size_t      bytes_length    = 125;

    // ── CLI options ─────────────────────────────────────────
    po::options_description desc("Allowed options");
    desc.add_options()
        ("help", "show this help message")

        // Mode
        ("mode",     po::value<std::string>(&mode)->default_value("source"),
                     "Operation mode: source or sink")
        ("role",     po::value<std::string>(&config.role)->default_value("both"),
                     "tx = transmit only (one B210), rx = receive only (other B210), "
                     "both = original single-box full-duplex/loopback")
        ("tx-reps",  po::value<int>(&tx_reps)->default_value(20),
                     "role tx: how many times to cycle through all chunks (one-way, no ACK)")
        ("rx-idle-timeout", po::value<double>(&rx_idle_timeout)->default_value(8.0),
                     "role rx: auto-stop and print the message after this many seconds "
                     "with no new bursts (TX has finished). 0 = run until Ctrl-C")
        ("skip-rate-check", po::bool_switch(&skip_rate_check),
                     "bypass the startup rate-chain consistency check (run even if rates mismatch)")
        ("timeout",  po::value<int>(&timeout_ms)->default_value(3000),
                     "ACK timeout in ms (source)")
        ("timer_interval", po::value<int>(&timer_interval)->default_value(1000),
                     "ACK timer interval in ms (sink)")

        // Message
        ("num_bits", po::value<int>(&num_bits)->default_value(1000),
                     "Payload bits per packet")
        ("interval", po::value<int>(&interval_ms)->default_value(3000),
                     "TX interval between packets (ms)")
        ("preamble", po::value<std::string>(&preamble_type)->default_value("m-sequence"),
                     "Preamble type: m-sequence or zadoff")
        ("m",        po::value<int>(&config.preamble_length)->default_value(5),
                     "m-sequence order (length = 2^m-1)")
        ("add_preamble", po::value<bool>(&config.add_preamble)->default_value(true),
                     "Prepend preamble to each packet")

        // Filter
        ("U",           po::value<int>(&config.U)->default_value(2),
                        "TX pulse-shaper upsampling factor (wire sps = U/D)")
        ("D",           po::value<int>(&config.D)->default_value(1),
                        "TX pulse-shaper downsampling factor (wire sps = U/D)")
        ("filter_type", po::value<std::string>(&config.filter_type)->default_value("rrc"),
                        "Filter type: rrc / rc / lp")
        ("symbol_rate", po::value<double>(&config.symbol_rate)->default_value(0.8e6),
                        "Symbol rate (Hz)")
        ("num_taps",    po::value<int>(&config.num_taps)->default_value(151),
                        "Filter tap count")
        ("roll_off",    po::value<double>(&config.roll_off)->default_value(0.25),
                        "Roll-off factor")
        ("num_threads", po::value<int>(&config.num_threads)->default_value(1),
                        "FFT threads")

        // TX hardware
        ("tx-args",   po::value<std::string>(&config.tx_args)->default_value(""),
                      "UHD TX device args (e.g. serial=XXXXXXX)")
        ("tx-rate",   po::value<double>(&config.tx_rate)->default_value(1.6e6),
                      "TX sample rate (Hz) = symbol_rate * U/D")
        ("tx-freq",   po::value<double>(&config.tx_freq)->default_value(2.412e9),
                      "TX centre frequency (Hz)")
        ("tx-gain",   po::value<double>(&config.tx_gain)->default_value(20.0),
                      "TX gain (dB)")
        ("tx-bw",     po::value<double>(&config.tx_bw)->default_value(1.0e6),
                      "TX bandwidth (Hz)")
        ("tx-ant",    po::value<std::string>(&config.tx_ant)->default_value("TX/RX"),
                      "TX antenna port")
        ("tx-channel",po::value<int>(&config.tx_channel)->default_value(0),
                      "TX channel index")
        ("tx-subdev", po::value<std::string>(&config.tx_subdev)->default_value("A:A"),
                      "TX subdev spec")

        // RX hardware
        ("rx-args",   po::value<std::string>(&config.rx_args)->default_value(""),
                      "UHD RX device args")
        ("rx-rate",   po::value<double>(&config.rx_rate)->default_value(1.6e6),
                      "RX sample rate (Hz) = tx_rate (integer samples/symbol)")
        ("rx-freq",   po::value<double>(&config.rx_freq)->default_value(2.412e9),
                      "RX centre frequency (Hz)")
        ("rx-gain",   po::value<double>(&config.rx_gain)->default_value(30.0),
                      "RX gain (dB)")
        ("rx-bw",     po::value<double>(&config.rx_bw)->default_value(1.0e6),
                      "RX bandwidth (Hz)")
        ("rx-ant",    po::value<std::string>(&config.rx_ant)->default_value("RX2"),
                      "RX antenna port")
        ("rx-channel",po::value<int>(&config.rx_channel)->default_value(0),
                      "RX channel index")
        ("rx-subdev", po::value<std::string>(&config.rx_subdev)->default_value("A:A"),
                      "RX subdev spec")

        // Common RF
        ("ref",      po::value<std::string>(&config.ref)->default_value("internal"),
                     "Clock reference: internal / external / mimo")
        ("settling", po::value<double>(&config.settling_time)->default_value(0.2),
                     "Settling time (s)")
        ("uhd_timeout", po::value<double>(&config.uhd_timeout)->default_value(1000.0),
                     "UHD TX timeout (ms)")

        // Energy detection
        ("alpha",
         po::value<float>(&config.alpha)->default_value(0.95f),
         "Energy-detector IIR smoothing: filtered = (1-alpha)*inst + alpha*prev, "
         "so LARGER alpha = MORE smoothing. The old 0.02 barely smoothed, so the "
         "detector fired on every noise spike (thousands of false bursts) and cut "
         "real bursts apart on the RRC envelope. 0.95 (~20-sample time constant) "
         "gives one clean capture per burst.")
        ("energy_threshold",
         po::value<float>(&config.energy_threshold)->default_value(1e-7f),
         "Fixed energy threshold (used if adaptive=false)")
        ("energy_packet_size",
         po::value<size_t>(&config.energy_packet_size)->default_value(3300),
         "Samples to collect after energy detection")
        ("IIR_window_size",
         po::value<size_t>(&config.IIR_window_size)->default_value(20),
         "IIR window size")
        ("IIR_threshold_adaptive",
         po::value<bool>(&config.IIR_threshold_adaptive)->default_value(true),
         "Use adaptive energy threshold")
        ("IIR_threshold_multiplier",
         po::value<float>(&config.IIR_threshold_multiplier)->default_value(5.0f),
         "Adaptive threshold = noise_floor × this")

        // Synchronisation
        ("sps_sync",
         po::value<int>(&config.sps_sync)->default_value(5),
         "Samples per symbol at match-filter output")
        ("sync_threshold",
         po::value<float>(&config.sync_threshold)->default_value(15.0f),
         "ACQ correlation threshold (preamble peak ~= preamble length after AGC)")
        ("recv_msg_len",
         po::value<int>(&config.message_length)->default_value(508),
         "Data symbols to extract (QPSK: 1016bits/2bps=508)")

        // Receiver buffering
        ("samps_per_buff",
         po::value<int>(&config.samps_per_buff)->default_value(10000),
         "Samples per UHD receive buffer")
        ("num_recv_request",
         po::value<int>(&config.num_recv_request)->default_value(0),
         "Total samples to receive (0=continuous)")

        // AGC
        ("AGC_type",
         po::value<std::string>(&config.AGC_type)->default_value("Feed"),
         "AGC type: Feed or Closed")

        // Modulation
        ("scheme",
         po::value<std::string>(&config.scheme)->default_value("QPSK"),
         "Modulation: QPSK / DQPSK / DBPSK / 16-QAM / ...")
        ("sps",
         po::value<int>(&config.sps)->default_value(2),
         "Samples per symbol (informational)")

        // Timing recovery
        ("timing_loop_bw",
         po::value<float>(&config.timing_loop_bw)->default_value(0.015f),
         "Gardner TED loop bandwidth BnT")
        ("timing_damping",
         po::value<float>(&config.timing_damping)->default_value(0.707f),
         "Gardner TED damping factor")

        // Phase correction
        ("phase_loop_bw",
         po::value<float>(&config.phase_loop_bw)->default_value(0.02f),
         "Phase PLL loop bandwidth")
        ("phase_damping",
         po::value<float>(&config.phase_damping)->default_value(0.707f),
         "Phase PLL damping factor")

        // Equaliser
        ("eq_taps",
         po::value<int>(&config.eq_taps)->default_value(11),
         "Number of equaliser taps")
        ("eq_mu",
         po::value<float>(&config.eq_mu)->default_value(0.01f),
         "LMS step size")
        ("eq_type",
         po::value<std::string>(&config.eq_type)->default_value("None"),
         "Equaliser type: LMS / RLS / DFE / None. Default None: on a clean cabled "
         "link no equaliser is needed, and the LMS loop currently DIVERGES on the "
         "real signal (decision-directed error grows and destroys the symbols) — "
         "verified on hardware, where None decodes the message and LMS produces "
         "garbage. Leave None until the LMS/RLS/DFE update is debugged.");

    po::variables_map vm;
    po::store(po::parse_command_line(argc, argv, desc), vm);
    po::notify(vm);

    if (vm.count("help")) {
        std::cout << desc << "\n";
        return EXIT_SUCCESS;
    }

    // ── Apply preamble type ─────────────────────────────────
    config.preamble_type = preamble_type;

    // ── Auto-size detect/sync lengths for the chosen modulation ──
    // The number of DATA symbols in a packet depends on bits/symbol, so both the
    // sync-extraction length (recv_msg_len) and the energy detector's minimum
    // burst length (energy_packet_size) must scale with --scheme. Otherwise the
    // QPSK-tuned defaults (508 data symbols, 3300-sample burst) reject or
    // mis-extract every other modulation. Explicit --recv_msg_len /
    // --energy_packet_size still win (only auto-set when left at their defaults).
    {
        Modulator probe(string_to_mod_type(config.scheme));
        int bps          = probe.get_bits_per_symbol();
        int preamble_len = (1 << config.preamble_length) - 1;        // m-sequence length
        const int guard  = 10;                                       // matches modulate()
        int packet_bits  = 16 + static_cast<int>(bytes_length) * 8 + 16;  // header + chunk + CRC-16
        int data_syms    = (packet_bits + bps - 1) / bps;            // ceil
        int total_syms   = guard + preamble_len + data_syms;

        if (vm["recv_msg_len"].defaulted())
            config.message_length = data_syms;              // ACQ extracts this many data symbols
        if (vm["energy_packet_size"].defaulted()) {
            // Detector works in RF samples: at rx_rate there are
            // (rx_rate/symbol_rate) samples/symbol, so a packet is
            // that many * total_syms samples. Use ~0.75x of that as the minimum
            // burst-length gate (real bursts include extra guard samples and
            // easily exceed it; short noise glitches don't).
            double det_sps = config.rx_rate / config.symbol_rate;
            config.energy_packet_size =
                static_cast<size_t>(std::lround(0.75 * det_sps * total_syms));
        }

        std::cout << "[MAIN] Auto-size for " << config.scheme << ": " << bps
                  << " bits/sym -> data_syms=" << data_syms << " (recv_msg_len), packet="
                  << total_syms << " sym, det_sps=" << (config.rx_rate/config.symbol_rate)
                  << " -> energy_packet_size=" << config.energy_packet_size
                  << " samples  (override via --recv_msg_len / --energy_packet_size)\n";
    }

    // ── Report the RX oversampling used for ACQ symbol timing ──
    // The RX front-end runs entirely at an INTEGER samples/symbol `os`
    // (= rx_rate/symbol_rate). The matched filter is single-rate at `os`, and
    // ACQ correlates at samples_per_symbol = os to pick the sampling instant
    // (no Gardner loop). --sps_sync is retained for compatibility but unused by
    // the RX pipeline (timing is done inside ACQ).
    {
        double os_f = config.rx_rate / config.symbol_rate;
        int    os_i = std::max(1, (int)std::lround(os_f));
        std::cout << "[MAIN] RX oversampling os = " << os_i
                  << " samples/symbol (= rx_rate/symbol_rate = " << os_f
                  << "); ACQ does joint frame+symbol timing at this rate.";
        if (std::abs(os_f - os_i) > 0.01)
            std::cout << "  [WARNING: non-integer — set rx_rate = k*symbol_rate]";
        std::cout << "\n";
    }

    // ── Rate-chain consistency check ─────────────────────────
    // Pipeline (this order): receive -> energy -> AGC -> matched filter (single
    //   rate at os) -> time sync (ACQ, joint frame+symbol timing) -> CFO ->
    //   phase -> strip preamble -> demod.
    // Requirements:
    //   1. tx_rate == symbol_rate * U/D          (pulse-shaper output rate)
    //   2. rx_rate == tx_rate                    (RX samples exactly what TX sent)
    //   3. os = rx_rate/symbol_rate is an integer >= 1  (clean integer sps)
    {
        const double rel = 1e-3;
        const double tx_rate_expected = config.symbol_rate
                                        * (double)config.U / (double)config.D;
        const double os = config.rx_rate / config.symbol_rate;       // integer samples/symbol

        std::cout << "[CONSISTENCY] rate chain (samples/symbol through the RX):\n"
                  << "  symbol_rate       = " << config.symbol_rate << " Hz\n"
                  << "  TX RRC U/D         = " << config.U << "/" << config.D
                  << "  => tx_rate should be symbol_rate*U/D = " << tx_rate_expected << " Hz\n"
                  << "  tx_rate (set)      = " << config.tx_rate << " Hz\n"
                  << "  rx_rate (set)      = " << config.rx_rate << " Hz\n"
                  << "  RX oversampling os = rx_rate/symbol_rate = " << os
                  << "   (matched filter single-rate; ACQ times at this sps)\n";

        bool fatal = false;
        if (std::abs(config.tx_rate - tx_rate_expected) > rel * tx_rate_expected) {
            std::cerr << "  [FAIL] tx_rate (" << config.tx_rate << ") != symbol_rate*U/D ("
                      << tx_rate_expected << "). The pulse shaper outputs at symbol_rate*U/D; "
                      << "set --tx-rate " << tx_rate_expected
                      << " (or change --symbol_rate / --U / --D).\n";
            fatal = true;
        }
        if (std::abs(config.rx_rate - config.tx_rate) > rel * config.tx_rate) {
            std::cerr << "  [FAIL] rx_rate (" << config.rx_rate << ") != tx_rate ("
                      << config.tx_rate << "). The RX must sample exactly what the TX sent; "
                      << "set --rx-rate " << config.tx_rate << ".";
            if (config.rx_rate < config.tx_rate)
                std::cerr << "  (rx_rate < tx_rate also aliases the signal.)";
            std::cerr << "\n";
            fatal = true;
        }
        if (std::abs(os - std::lround(os)) > 0.01) {
            std::cerr << "  [FAIL] os = rx_rate/symbol_rate = " << os
                      << " is not an integer. The RX front-end needs an integer number of "
                      << "samples/symbol; set symbol_rate so rx_rate/symbol_rate is a whole "
                      << "number (e.g. rx_rate = 2*symbol_rate).\n";
            fatal = true;
        }

        if (!fatal) {
            std::cout << "  [OK] integer sps front-end: matched filter + ACQ time cleanly.\n\n";
        } else if (skip_rate_check) {
            std::cerr << "  [CONSISTENCY] rate mismatch above, but --skip-rate-check is set: "
                         "continuing anyway (expect garbled output).\n\n";
        } else {
            std::cerr << "[CONSISTENCY] Fatal rate mismatch (see above). Fix the rates, or pass "
                         "--skip-rate-check to run regardless.\n";
            return EXIT_FAILURE;
        }
    }

    // ── Role / mode routing ─────────────────────────────────
    // role = "tx": transmit only on the TX B210 (--tx-args serial=...).
    // role = "rx": receive only on the RX B210 (--rx-args serial=...).
    // role = "both": original single-box behaviour, where --mode source/sink
    //                selects the loopback device/frequency swap.
    if (config.role == "tx") {
        // transmit-only: use --tx-* exactly as given, RX pipeline not launched.
        mode = "source";
        std::cout << "[MAIN] Role tx  -> TX device '" << config.tx_args
                  << "' subdev " << config.tx_subdev << " ant " << config.tx_ant << "\n";
    } else if (config.role == "rx") {
        // receive-only: use --rx-* exactly as given, TX pipeline not launched.
        mode = "sink";
        std::cout << "[MAIN] Role rx  -> RX device '" << config.rx_args
                  << "' subdev " << config.rx_subdev << " ant " << config.rx_ant << "\n";
    } else if (config.role == "source_arq" || config.role == "sink_arq") {
        // ── Two-box stop-and-wait ARQ (full-duplex on one B210) ──
        // Data on one RF front-end, ACK on the other. The user supplies BOTH
        // --tx-* and --rx-* explicitly (same --tx-args == --rx-args = this box's
        // serial, different --tx-subdev/--rx-subdev, antennas and frequencies).
        //   source_arq: TX = data out, RX = ACK in.   Runs SOURCE.
        //   sink_arq  : RX = data in,  TX = ACK out.   Runs SINK.
        // No arg munging — the config is used as given.
        if (config.tx_args.empty() || config.tx_args != config.rx_args) {
            std::cerr << "[ERROR] " << config.role << " needs --tx-args and --rx-args "
                         "set to the SAME serial (this box), with different "
                         "--tx-subdev/--rx-subdev for RF A vs RF B.\n";
            return EXIT_FAILURE;
        }
        std::cout << "[MAIN] Role " << config.role << "  device '" << config.tx_args
                  << "'  data/ACK split: TX subdev " << config.tx_subdev << " ("
                  << config.tx_ant << ", " << config.tx_freq/1e9 << " GHz)  |  RX subdev "
                  << config.rx_subdev << " (" << config.rx_ant << ", "
                  << config.rx_freq/1e9 << " GHz)\n";
    } else if (config.role == "both") {
        // ── Legacy single-box routing (unchanged) ───────────
        // source: TX and RX on the same physical device (different ports)
        // sink  : swap TX/RX device serials and frequencies
        std::string tx_serial  = config.tx_args;
        std::string rx_serial  = config.rx_args;
        double      tx_freq_   = config.tx_freq;
        double      rx_freq_   = config.rx_freq;

        if (mode == "source") {
            if (config.rx_args.empty())
                config.rx_args = tx_serial;
        } else if (mode == "sink") {
            config.tx_args = rx_serial;
            config.rx_args = tx_serial;
            config.tx_freq = rx_freq_;
            config.rx_freq = tx_freq_;
        } else {
            std::cerr << "[ERROR] Unknown mode: " << mode
                      << "  (use source or sink)\n";
            return EXIT_FAILURE;
        }
    } else {
        std::cerr << "[ERROR] Unknown role: " << config.role
                  << "  (use tx | rx | both | source_arq | sink_arq)\n";
        return EXIT_FAILURE;
    }

    // ── Register SIGINT handler ─────────────────────────────
    std::signal(SIGINT, global_sig_int_handler);

    // ── Build message ───────────────────────────────────────
    std::string original_message =
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

    std::cout << "[MAIN] Message length: "
              << original_message.size() << " bytes\n";

    auto chunks = split_message_into_chunks(original_message, bytes_length);
    // Pad the final (short) chunk up to bytes_length so EVERY packet is the same
    // size. The RX detect/sync path is sized for a full chunk (fixed data-symbol
    // count + energy min-length gate), so a short final chunk would otherwise be
    // rejected and its bytes lost. Padding with spaces is harmless for text.
    for (auto& c : chunks)
        if (c.size() < bytes_length) c.resize(bytes_length, ' ');
    std::cout << "[MAIN] Split into " << chunks.size()
              << " chunk(s) of " << bytes_length << " bytes (final chunk padded)\n";

    // ── Start physical layer ────────────────────────────────
    std::cout << "[MAIN] Mode: " << mode << "\n";
    std::cout << "[MAIN] Scheme: " << config.scheme << "\n";
    std::cout << "[MAIN] TX " << config.tx_freq/1e9 << " GHz  gain="
              << config.tx_gain << " dB\n";
    std::cout << "[MAIN] RX " << config.rx_freq/1e9 << " GHz  gain="
              << config.rx_gain << " dB\n";

    PHYSICAL_LAYER transceiver(config);
    transceiver.start();

    // ── Run according to role ───────────────────────────────
    if (config.role == "source_arq") {
        // Stop-and-wait ARQ transmitter: send each chunk, wait for its ACK on the
        // reverse RF channel, retransmit on timeout, advance when ACKed. Exits
        // when every chunk is ACKed (or gives up per SOURCE max-attempts).
        std::cout << "[SOURCE-ARQ] " << chunks.size() << " chunks, timeout "
                  << timeout_ms << " ms. Ctrl-C to abort.\n";
        SOURCE source(transceiver, timeout_ms, num_bits);
        source.start(chunks);
        while (!source.done() && !global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        source.stop();
        std::cout << "[SOURCE-ARQ] Finished.\n";

    } else if (config.role == "sink_arq") {
        // Stop-and-wait ARQ receiver: CRC-verify each data chunk, transmit an ACK
        // (full-size, so it matches the data length) on the reverse RF channel,
        // reassemble. Exits once all chunks are received.
        std::cout << "[SINK-ARQ] Waiting for chunks; ACKing verified ones. "
                     "Ctrl-C to stop.\n";
        SINK sink(transceiver, timer_interval, bytes_length);
        sink.start();
        while (!sink.done() && !global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        // Grace period so a lost final ACK is re-sent when the source retransmits
        // its last chunk (the sink re-ACKs duplicates).
        if (sink.done())
            std::this_thread::sleep_for(std::chrono::milliseconds(2000));
        sink.stop();
        sink.print_received_message();

    } else if (config.role == "tx") {
        // ONE-WAY TRANSMIT (no ARQ). Push every chunk into the TX pipeline and
        // repeat the whole message tx_reps times so the RX — which may be started
        // at any moment — has several chances to acquire each burst.
        uint8_t total = static_cast<uint8_t>(chunks.size());
        std::cout << "[TX] One-way transmit: " << (int)total << " chunk(s), scheme "
                  << config.scheme << ", " << tx_reps << " reps. Ctrl-C to stop.\n";
        for (int r = 0; r < tx_reps && !global_stop_signal.load(); ++r) {
            for (uint8_t idx = 0; idx < total && !global_stop_signal.load(); ++idx) {
                auto bits = build_packet_bits(chunks[idx], idx, total);
                transceiver.transmit(bits);
                std::cout << "[TX] queued chunk " << (int)idx + 1 << "/" << (int)total
                          << "  (rep " << r + 1 << "/" << tx_reps << ")\n";
                std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
            }
        }
        // let the final burst drain out of the TX pipeline before shutting down
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        std::cout << "[TX] Done.\n";

    } else if (config.role == "rx") {
        // ONE-WAY RECEIVE (no ARQ). Pull decoded packets from the RX pipeline,
        // place each chunk by its header index, and print the running message.
        // Auto-terminate: the TX process exits once it has sent all repetitions,
        // after which no more bursts arrive. When no packet has been received for
        // rx_idle_timeout seconds (only armed AFTER the first burst, so the RX can
        // be started first and wait for the TX), stop, assemble, and print. Ctrl-C
        // still works. --rx-idle-timeout 0 restores the old run-until-Ctrl-C.
        // CRC-VERIFIED COLLECTION. Each packet carries a CRC-16, so a chunk is
        // accepted only when its CRC checks out — i.e. it arrived with NO bit
        // errors. The TX repeats every chunk (--tx-reps), so a corrupted copy is
        // simply dropped and a later clean copy is taken instead. This guarantees
        // an error-free reassembled message over the current one-way link, with
        // no reverse ACK channel. The RX stops as soon as every chunk has a
        // CRC-verified copy (or on the idle timeout / Ctrl-C).
        std::cout << "[RX] One-way receive (CRC-verified), scheme " << config.scheme
                  << ". Auto-stops when all chunks verified, or " << rx_idle_timeout
                  << " s after the last burst (Ctrl-C also stops).\n";
        std::vector<std::string> parts;
        std::vector<bool>        got;
        int total = 0;
        long rx_bursts = 0, crc_pass = 0, crc_fail = 0;
        bool got_any = false;
        auto last_rx = std::chrono::steady_clock::now();
        while (!global_stop_signal.load()) {
            std::pair<size_t, std::vector<uint8_t>> rx;
            if (transceiver.rx_bits_fifo.pop(rx)) {
                // Any burst (valid or not) counts as TX activity: keep the link
                // alive while the transmitter is still sending.
                got_any = true;
                rx_bursts++;
                last_rx = std::chrono::steady_clock::now();
                auto [idx, tot, payload, crc_ok] = decode_packet_bits(rx.second);
                // Accept ONLY error-free frames: CRC must pass, and the header
                // must be self-consistent (belt-and-suspenders against the ~1/65536
                // CRC false-accept). A failed CRC means bit errors → drop it and
                // wait for a clean retransmission.
                if (!crc_ok || tot == 0 || tot > 64 || idx >= tot) {
                    crc_fail++;
                    continue;
                }
                crc_pass++;
                total = tot;
                if ((int)parts.size() < tot) { parts.resize(tot); got.resize(tot, false); }
                bool first = !got[idx];
                parts[idx] = payload; got[idx] = true;
                int have = 0; for (bool g : got) have += g ? 1 : 0;
                std::cout << "[RX] chunk " << (int)idx + 1 << "/" << (int)tot
                          << (first ? "  [CRC OK, new]" : "  [CRC OK, dup]")
                          << "  (" << have << "/" << (int)tot << " verified): \""
                          << payload << "\"\n";
                if (have == total) {
                    std::cout << "[RX] all " << total
                              << " chunks CRC-verified — message complete, stopping.\n";
                    break;
                }
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                if (rx_idle_timeout > 0.0 && got_any) {
                    double idle = std::chrono::duration<double>(
                        std::chrono::steady_clock::now() - last_rx).count();
                    if (idle >= rx_idle_timeout) {
                        std::cout << "[RX] no new bursts for " << idle
                                  << " s (>= " << rx_idle_timeout
                                  << " s) — TX finished, stopping.\n";
                        break;
                    }
                }
            }
        }
        std::cout << "[RX] bursts=" << rx_bursts << "  CRC-pass=" << crc_pass
                  << "  CRC-fail(dropped)=" << crc_fail << "\n";
        {
            int have = 0; for (bool g : got) have += g ? 1 : 0;
            if (total > 0 && have < total)
                std::cout << "[RX] WARNING: only " << have << "/" << total
                          << " chunks verified — message INCOMPLETE (raise --tx-reps"
                             " or link margin).\n";
        }
        std::string full; for (auto& p : parts) full += p;
        std::cout << "\n================ DECODED MESSAGE ================\n"
                  << full << "\n"
                  << "=================================================\n";

    } else if (mode == "source") {   // role both (legacy ARQ)
        SOURCE source(transceiver, timeout_ms, num_bits);
        source.start(chunks);

        std::cout << "[SOURCE] Running — Ctrl-C to stop\n";
        while (!global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(500));

        source.stop();

    } else {   // role both, mode sink (legacy ARQ)
        SINK sink(transceiver, timer_interval);
        sink.start();

        std::cout << "[SINK] Running — Ctrl-C to stop\n";
        while (!global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(500));

        sink.stop();
        sink.print_received_message();
    }

    transceiver.stop();
    std::cout << "[MAIN] Finished\n";
    return EXIT_SUCCESS;
}
