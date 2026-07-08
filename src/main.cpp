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
#include <memory>
#include <cmath>
#include <algorithm>
#include <random>
#include <fstream>

#include "physical_layer.hpp"
#include "ACQ_stop_and_wait.hpp"
#include "fec.hpp"
#include "viz.hpp"
#include <filesystem>

namespace po = boost::program_options;

// ─────────────────────────────────────────────────────────────
int UHD_SAFE_MAIN(int argc, char* argv[])
{
    // ── Parameter declarations ──────────────────────────────
    PHYSICAL_CONFIG config;

    std::string mode;
    int         timeout_ms      = 3000;
    int         timer_interval  = 20;      // sink poll interval (ms); sets ACK latency
    int         max_attempts    = 50;      // source_arq: give up on a chunk after N (0=never)
    int         num_bits        = 1000;
    int         interval_ms     = 3000;
    int         tx_reps         = 20;      // one-way (role tx) repetitions
    double      rx_idle_timeout = 8.0;     // role rx: auto-stop after N s of no bursts
    bool        stop_on_complete = true;   // role rx: stop as soon as every chunk verified
    bool        skip_rate_check = false;   // bypass the rate-chain consistency check
    std::string tx_mode         = "burst"; // burst (finite, gaps) | continuous (until Ctrl-C)
    std::string message_type    = "bytes"; // bytes | random | sine | cosine
    std::string message_str;               // override text for --message-type bytes
    std::string payload_file;              // TX: send raw bytes from this file (binary payload)
    std::string out_file;                  // RX: write decoded payload bytes to this file
    double      tone_freq       = 100e3;   // sine/cosine baseband freq (Hz)
    float       tone_amp        = 0.5f;    // sine/cosine amplitude (< 1.0)
    double      sense_window_ms   = 10.0;  // role sense: energy-integration window (ms)
    double      sense_threshold_db = -30.0;// role sense: busy if avg power_db exceeds this
    int         sense_count       = 1;     // role sense: number of windows to report
    bool        viz_on          = true;    // dump signals + save plots (default on)
    std::string viz_dir         = "viz";
    std::string preamble_type;
    size_t      bytes_length    = 125;

    // ── CLI options ─────────────────────────────────────────
    // Wide line_length (200) so --help doesn't hard-break long words mid-token
    // (keeps each description clean for the auto-generated Python API / OPTIONS.md).
    po::options_description desc("Allowed options", 200);
    desc.add_options()
        ("help", "show this help message")

        // Mode
        ("mode",     po::value<std::string>(&mode)->default_value("source"),
                     "Operation mode: source or sink")
        ("role",     po::value<std::string>(&config.role)->default_value("both"),
                     "tx = transmit only (one B210), rx = receive only (other B210), "
                     "source_arq / sink_arq = the two ends of stop-and-wait ARQ, "
                     "sense = channel-occupancy sensing (RX energy over a window, no decode), "
                     "both = original single-box full-duplex/loopback")
        ("tx-reps",  po::value<int>(&tx_reps)->default_value(20),
                     "role tx: how many times to cycle through all chunks (one-way, no ACK)")
        ("rx-idle-timeout", po::value<double>(&rx_idle_timeout)->default_value(8.0),
                     "role rx: auto-stop and print the message after this many seconds "
                     "with no new bursts (TX has finished). 0 = run until Ctrl-C")
        ("stop-on-complete", po::value<bool>(&stop_on_complete)->default_value(true),
                     "role rx: stop as soon as every chunk of a finite message "
                     "(bytes / fixed-length random) is CRC-verified (default true). "
                     "false = keep receiving (collect duplicates / measure the link) "
                     "until the idle timeout or Ctrl-C. Ignored for continuous TX.")
        ("ack-transport", po::value<std::string>(&config.ack_transport)->default_value("tcp"),
                     "ARQ ACK channel: tcp (default, ACK over a socket — no reverse RF "
                     "needed) or rf (ACK over the second RF path, RF B)")
        ("ack-host", po::value<std::string>(&config.ack_host)->default_value("127.0.0.1"),
                     "TCP ACK: host/IP of the sink that the source connects to "
                     "(default localhost)")
        ("ack-port", po::value<int>(&config.ack_port)->default_value(5599),
                     "TCP ACK: socket port")
        ("skip-rate-check", po::bool_switch(&skip_rate_check),
                     "bypass the startup rate-chain consistency check (run even if rates mismatch)")
        ("viz", po::value<bool>(&viz_on)->default_value(true),
                     "capture TX/RX signals and auto-save the plot to "
                     "<viz-dir>/<scheme>/figure.png (default true; --viz false disables)")
        ("viz-dir", po::value<std::string>(&viz_dir)->default_value("viz"),
                     "base directory for --viz output (a per-modulation subfolder is made)")
        ("timeout",  po::value<int>(&timeout_ms)->default_value(3000),
                     "ACK timeout in ms (source)")
        ("timer_interval", po::value<int>(&timer_interval)->default_value(20),
                     "sink FIFO poll interval in ms — this SETS the ACK round-trip "
                     "latency, so keep it small (was 1000, which made ARQ ~5x slower)")
        ("max-attempts", po::value<int>(&max_attempts)->default_value(50),
                     "source_arq: give up on a chunk after this many un-ACKed sends. "
                     "0 = never give up (keeps TX/RX in lockstep on a marginal link, "
                     "since a given-up chunk desyncs a paired sender/receiver loop).")

        // Message
        ("num_bits", po::value<int>(&num_bits)->default_value(1000),
                     "Payload bits per packet")
        ("interval", po::value<int>(&interval_ms)->default_value(3000),
                     "TX interval between packets (ms)")
        ("tx-mode", po::value<std::string>(&tx_mode)->default_value("burst"),
                     "role tx transmission mode: burst (discrete packets/tone bursts "
                     "with --interval gaps, repeated --tx-reps times then stop) or "
                     "continuous (transmit until Ctrl-C — a continuous data loop or "
                     "an unbroken carrier for sine/cosine)")
        ("message-type", po::value<std::string>(&message_type)->default_value("bytes"),
                     "payload: bytes (given text, default Star Wars crawl; set with "
                     "--message) | random (num_bits random bits) | sine | cosine "
                     "(raw baseband test tone, role tx only)")
        ("message", po::value<std::string>(&message_str),
                     "text payload for --message-type bytes (overrides the default "
                     "Star Wars crawl)")
        ("bytes-length", po::value<size_t>(&bytes_length)->default_value(125),
                     "payload bytes per chunk (default 125). Larger chunks amortise the "
                     "per-burst detect/sync/ACK overhead — higher throughput. MUST match "
                     "on TX and RX. Total chunks <= 255.")
        ("payload-file", po::value<std::string>(&payload_file),
                     "TX: send the raw bytes of this file as the payload (binary, e.g. a "
                     "serialized gradient). Overrides --message / --message-type.")
        ("out-file", po::value<std::string>(&out_file),
                     "RX (rx / sink_arq): write the decoded payload as raw bytes to this "
                     "file (pairs with --payload-file for a binary byte-pipe).")
        ("sense-window", po::value<double>(&sense_window_ms)->default_value(10.0),
                     "role sense: energy-integration window in ms (default 10)")
        ("sense-threshold-db", po::value<double>(&sense_threshold_db)->default_value(-30.0),
                     "role sense: channel is 'busy' when the window's avg power (dB) "
                     "exceeds this. Calibrate to your gain/noise floor (channel_sense.py "
                     "can auto-calibrate).")
        ("sense-count", po::value<int>(&sense_count)->default_value(1),
                     "role sense: number of consecutive windows to measure/report "
                     "(0 = stream forever until Ctrl-C — for a persistent sensing feed)")
        ("tone-freq", po::value<double>(&tone_freq)->default_value(100e3),
                     "sine/cosine baseband frequency in Hz (default 100 kHz)")
        ("tone-amp", po::value<float>(&tone_amp)->default_value(0.5f),
                     "sine/cosine amplitude, keep < 1.0 to avoid DAC clipping")
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
         "Fixed energy threshold (used only if the adaptive detector is off). "
         "Alias: --det-threshold")
        ("det-threshold", po::value<float>(),
         "alias for --energy_threshold (fixed detector threshold)")
        ("energy_packet_size",
         po::value<size_t>(&config.energy_packet_size)->default_value(3300),
         "Samples to collect after energy detection")
        ("IIR_window_size",
         po::value<size_t>(&config.IIR_window_size)->default_value(20),
         "IIR window size")
        ("IIR_threshold_adaptive",
         po::value<bool>(&config.IIR_threshold_adaptive)->default_value(true),
         "Use the auto (adaptive) detection threshold = noise_floor × multiplier. "
         "Alias: --det-adaptive")
        ("det-adaptive", po::value<bool>(),
         "alias for --IIR_threshold_adaptive (use the auto detector threshold)")
        ("IIR_threshold_multiplier",
         po::value<float>(&config.IIR_threshold_multiplier)->default_value(5.0f),
         "Auto detector threshold = measured noise_floor × this. RAISE it "
         "over-the-air (e.g. 10-30) so the detector fires only on real bursts, "
         "not ambient RF; too high and it misses weak bursts. Alias: --det-mult")
        ("det-mult", po::value<float>(),
         "alias for --IIR_threshold_multiplier (auto-threshold noise multiplier)")
        ("det-continuous",
         po::value<bool>(&config.det_continuous_track)->default_value(true),
         "Continuously re-track the noise floor during idle (default true), vs a "
         "one-shot startup calibration. Robust to drifting ambient noise.")

        // Synchronisation
        ("sps_sync",
         po::value<int>(&config.sps_sync)->default_value(5),
         "Samples per symbol at match-filter output")
        ("sync_threshold",
         po::value<float>(&config.sync_threshold)->default_value(15.0f),
         "ACQ correlation threshold — raise it over-the-air so ambient-noise bursts "
         "are rejected (a real preamble peaks near the preamble length ~31 after AGC; "
         "noise correlates far lower). Watch the '[ACQ]   Peak correlation' lines and "
         "set it below the true peak but above the noise. Alias: --sync-threshold")
        ("sync-threshold",
         po::value<float>(),
         "alias for --sync_threshold (hyphenated spelling)")
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
        ("dc-block",
         po::value<bool>(&config.dc_block)->default_value(false),
         "Experimental per-burst DC-block high-pass on the RX (default false). "
         "A gentle cutoff barely dents the cable leakage; an aggressive one "
         "distorts the preamble and breaks sync — prefer --tx-dc-i/--tx-dc-q.")
        ("tx-dc-i",
         po::value<float>(&config.tx_dc_i)->default_value(0.0f),
         "Manual TX LO-leakage null, I component (normalized [-1,1]). Tune with "
         "--tx-dc-q to minimize the RX DC spike on a direct cable (dense QAM).")
        ("tx-dc-q",
         po::value<float>(&config.tx_dc_q)->default_value(0.0f),
         "Manual TX LO-leakage null, Q component (normalized [-1,1]).")

        // Modulation
        ("scheme",
         po::value<std::string>(&config.scheme)->default_value("QPSK"),
         "Modulation: QPSK / DQPSK / DBPSK / 16-QAM / ...")
        ("fec",
         po::value<bool>(&config.fec)->default_value(false),
         "Forward Error Correction (rate-1/2 K=7 convolutional + Viterbi). "
         "Corrects bit errors so a noisy link decodes error-free; must match on "
         "both ends. Halves the payload rate (2x the symbols).")
        ("waveform",
         po::value<std::string>(&config.waveform)->default_value("sc"),
         "Waveform: sc (single-carrier) or ofdm. OFDM handles multipath/CFO "
         "natively (per-subcarrier equalization) — best for dense QAM.")
        ("ofdm-fft",
         po::value<int>(&config.ofdm_fft)->default_value(64),
         "OFDM FFT size (number of subcarriers)")
        ("ofdm-cp",
         po::value<int>(&config.ofdm_cp)->default_value(16),
         "OFDM cyclic-prefix length (>= channel delay spread)")
        ("ofdm-tx-peak",
         po::value<float>(&config.ofdm_tx_peak)->default_value(0.5f),
         "OFDM TX peak scaling (high PAPR — keep the DAC out of clipping)")
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
         po::value<float>(&config.eq_mu)->default_value(0.3f),
         "Equalizer NLMS step (used for DD tracking / real-preamble training)")
        ("eq_dd",
         po::value<bool>(&config.eq_decision_directed)->default_value(false),
         "Equalizer decision-directed tracking after training (default off: the "
         "LS-trained eq is exact frozen; DD can diverge on noisy dense QAM)")
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

    // Visualization capture: per-modulation subfolder, ideal constellation +
    // metadata (so the plotter can compute EVM and title the figure).
    if (viz_on) {
        viz::enabled = true;
        viz::dir     = viz_dir + "/" + config.scheme;      // e.g. viz/16-QAM
        std::error_code ec; std::filesystem::create_directories(viz::dir, ec);
        try {
            Modulator vmod(string_to_mod_type(config.scheme));
            viz::save_iq("ideal", vmod.get_constellation());   // ideal points for EVM
        } catch (...) {}
        std::ofstream meta(viz::dir + "/meta.txt");
        if (meta.is_open()) {
            meta << "scheme " << config.scheme << "\n"
                 << "waveform " << config.waveform << "\n"
                 << "fs " << config.rx_rate << "\n"
                 << "fec " << (config.fec ? 1 : 0) << "\n";
        }
        std::cout << "[MAIN] --viz on: capturing to '" << viz::dir
                  << "/' (figure auto-saved on exit)\n";
    }

    // Hyphenated aliases override their underscore originals when given (matches
    // the hyphen convention of --rx-gain / --tx-args etc.).
    if (vm.count("sync-threshold"))
        config.sync_threshold = vm["sync-threshold"].as<float>();
    if (vm.count("det-mult"))
        config.IIR_threshold_multiplier = vm["det-mult"].as<float>();
    if (vm.count("det-threshold"))
        config.energy_threshold = vm["det-threshold"].as<float>();
    if (vm.count("det-adaptive"))
        config.IIR_threshold_adaptive = vm["det-adaptive"].as<bool>();

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
        // Derive the preamble length from the ACTUAL generated preamble so it is
        // correct for either an m-sequence (2^m-1) or a Zadoff-Chu sequence.
        int preamble_len = (int)generate_preamble(config.preamble_type,
                                                  config.preamble_length).size();
        const int guard  = 10;                                       // matches modulate()
        int packet_bits  = 16 + static_cast<int>(bytes_length) * 8 + 16;  // header + chunk + CRC-16
        // With FEC, the modulator carries the ENCODED bits (rate 1/2 → ~2x).
        int coded_bits   = config.fec ? fec_encoded_len(packet_bits) : packet_bits;
        int data_syms    = (coded_bits + bps - 1) / bps;            // ceil (QAM symbols)
        int total_syms   = guard + preamble_len + data_syms;

        if (vm["recv_msg_len"].defaulted())
            config.message_length = data_syms;              // data QAM symbols per chunk

        if (config.waveform == "ofdm") {
            // OFDM frame = [SC | chest | data] OFDM symbols on the wire, one
            // sample per wire sample (no RRC). data subcarriers = fft-2 (skip
            // DC + Nyquist). The energy detector gate is ~0.6x the frame length.
            OFDM ofdm_probe(config.ofdm_fft, config.ofdm_cp);
            int dsc  = std::max(1, ofdm_probe.data_per_sym());   // data SC (excl. pilots)
            int nsym = (data_syms + dsc - 1) / dsc;
            int frame_samples = ofdm_probe.frame_len(data_syms);
            if (vm["energy_packet_size"].defaulted())
                config.energy_packet_size = (size_t)std::lround(0.6 * frame_samples);
            std::cout << "[MAIN] OFDM: fft=" << config.ofdm_fft << " cp=" << config.ofdm_cp
                      << " data_sc=" << dsc << " -> " << nsym << " data OFDM syms, frame="
                      << frame_samples << " samples, energy_packet_size="
                      << config.energy_packet_size << "\n";
        }
        else if (vm["energy_packet_size"].defaulted()) {
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

    // ── OFDM waveform: no symbol_rate/RRC chain; the OFDM samples ARE the
    //    baseband, sent at tx_rate directly. Only require rx_rate == tx_rate. ──
    if (config.waveform == "ofdm") {
        std::cout << "[MAIN] Waveform: OFDM (fft=" << config.ofdm_fft
                  << ", cp=" << config.ofdm_cp << ").  OFDM samples sent at tx_rate="
                  << config.tx_rate << " Hz directly (no RRC / match filter).\n";
        if (std::abs(config.rx_rate - config.tx_rate) > 1e-3 * config.tx_rate) {
            std::cerr << "  [FAIL] OFDM needs rx_rate == tx_rate (" << config.tx_rate
                      << "). Set --rx-rate " << config.tx_rate << ".\n";
            if (!skip_rate_check) return EXIT_FAILURE;
        } else {
            std::cout << "  [OK] rx_rate == tx_rate; OFDM does its own sync/CFO/eq.\n\n";
        }
    } else {

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
    }   // end single-carrier (non-OFDM) rate-check branch

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
    } else if (config.role == "sense") {
        // channel sensing: RX only, integrate energy over a window (no decode pipeline).
        mode = "sink";
        std::cout << "[MAIN] Role sense  -> RX device '" << config.rx_args
                  << "' — measuring channel occupancy\n";
    } else if (config.role == "source_arq" || config.role == "sink_arq") {
        // ── Two-box stop-and-wait ARQ ──
        // DATA always travels over RF (source TX -> sink RX). The ACK uses the
        // transport chosen by --ack-transport:
        //   tcp (default): ACK over a socket; each box needs only its DATA RF
        //                  path (one cable). Source connects to --ack-host:--ack-port;
        //                  sink listens there.
        //   rf           : ACK over the second RF path (RF B); each box is
        //                  full-duplex on ONE B210, so --tx-args == --rx-args
        //                  (same serial) with different --tx-subdev/--rx-subdev.
        if (config.ack_transport == "rf") {
            if (config.tx_args.empty() || config.tx_args != config.rx_args) {
                std::cerr << "[ERROR] " << config.role << " with --ack-transport rf needs "
                             "--tx-args and --rx-args set to the SAME serial (this box), "
                             "with different --tx-subdev/--rx-subdev for RF A vs RF B.\n";
                return EXIT_FAILURE;
            }
        } else if (config.ack_transport != "tcp") {
            std::cerr << "[ERROR] --ack-transport must be 'tcp' or 'rf'\n";
            return EXIT_FAILURE;
        }
        std::cout << "[MAIN] Role " << config.role << "  ACK transport: "
                  << config.ack_transport;
        if (config.ack_transport == "tcp")
            std::cout << "  (" << config.ack_host << ":" << config.ack_port << ")";
        std::cout << "\n";
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
    static const std::string STAR_WARS =
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

    // A test tone (sine/cosine) is a raw waveform, not framed data — no preamble,
    // chunks or CRC — so it cannot be ACKed. Only role tx (transmit) and role rx
    // (monitor) make sense; ARQ roles are rejected.
    const bool is_tone = (message_type == "sine" || message_type == "cosine");
    if (is_tone && config.role != "tx" && config.role != "rx") {
        std::cerr << "[ERROR] --message-type " << message_type << " (test tone) needs "
                  << "--role tx (transmit) or --role rx (monitor); it is not framed "
                  << "data, so ARQ (source_arq/sink_arq) does not apply.\n";
        return EXIT_FAILURE;
    }

    // Channel sensing (--role sense), like a tone, carries no framed payload.
    const bool sense_mode = (config.role == "sense");
    const bool no_payload = is_tone || sense_mode;

    // Framed payload: given bytes (text) or random bits. Both are split into
    // fixed-size chunks below, so message length / --num_bits drive the chunk count.
    std::string original_message;
    if (!payload_file.empty() && !no_payload) {
        // Raw binary payload from a file (e.g. a serialized gradient). Highest
        // priority — overrides --message / --message-type. Read as bytes.
        std::ifstream f(payload_file, std::ios::binary);
        if (!f) {
            std::cerr << "[ERROR] cannot open --payload-file '" << payload_file << "'\n";
            return EXIT_FAILURE;
        }
        original_message.assign(std::istreambuf_iterator<char>(f),
                                std::istreambuf_iterator<char>());
        std::cout << "[MAIN] Payload file: " << payload_file << " -> "
                  << original_message.size() << " bytes\n";
    } else if (message_type == "bytes" && !no_payload) {
        original_message = message_str.empty() ? STAR_WARS : message_str;
    } else if (message_type == "random" && !no_payload) {
        int nbytes = std::max(1, num_bits / 8);          // --num_bits random bits
        std::mt19937 rng(0xC0FFEEu);                     // fixed seed → reproducible
        original_message.resize(nbytes);
        for (auto& ch : original_message) ch = static_cast<char>(rng() & 0xFF);
        std::cout << "[MAIN] Random payload: " << nbytes << " bytes ("
                  << num_bits << " bits)\n";
    } else if (!no_payload) {
        std::cerr << "[ERROR] unknown --message-type '" << message_type
                  << "' (use: bytes | random | sine | cosine)\n";
        return EXIT_FAILURE;
    }

    // Split into fixed-size chunks (tones carry no framed message → chunks empty).
    // Pad the final short chunk up to bytes_length so EVERY packet is the same size
    // (the RX detect/sync path is sized for a full chunk).
    auto chunks = split_message_into_chunks(original_message, bytes_length);
    for (auto& c : chunks)
        if (c.size() < bytes_length) c.resize(bytes_length, ' ');
    if (chunks.size() > 255) {
        std::cerr << "[ERROR] payload needs " << chunks.size() << " chunks but the "
                  << "packet header (uint8 total) allows at most 255. Increase "
                  << "--bytes-length (currently " << bytes_length << ").\n";
        return EXIT_FAILURE;
    }
    if (!no_payload)
        std::cout << "[MAIN] Message: " << original_message.size() << " bytes -> "
                  << chunks.size() << " chunk(s) of " << bytes_length << " bytes\n";

    // ── Start physical layer ────────────────────────────────
    std::cout << "[MAIN] Mode: " << mode << "\n";
    std::cout << "[MAIN] Scheme: " << config.scheme << "\n";
    std::cout << "[MAIN] ACQ sync_threshold: " << config.sync_threshold
              << "  (raise for over-the-air; watch '[ACQ]   Peak correlation')\n";
    if (config.IIR_threshold_adaptive)
        std::cout << "[MAIN] Energy detector: AUTO threshold = noise_floor x "
                  << config.IIR_threshold_multiplier << " (--det-mult)"
                  << "  (raise over-the-air to reject ambient RF)\n";
    else
        std::cout << "[MAIN] Energy detector: FIXED threshold = "
                  << config.energy_threshold << " (--det-threshold)\n";
    std::cout << "[MAIN] TX " << config.tx_freq/1e9 << " GHz  gain="
              << config.tx_gain << " dB\n";
    std::cout << "[MAIN] RX " << config.rx_freq/1e9 << " GHz  gain="
              << config.rx_gain << " dB\n";

    // role rx + a sine/cosine "message" = raw tone monitor: configure the radio
    // but skip the decode pipeline (the monitor streams samples itself).
    const bool tone_monitor = (config.role == "rx" && is_tone);
    PHYSICAL_LAYER transceiver(config);
    transceiver.start(tone_monitor || sense_mode);

    // ── Channel sensing: measure occupancy over N windows, then exit ──
    if (sense_mode) {
        std::cout << "[MAIN] Sensing " << sense_count << " window(s) of "
                  << sense_window_ms << " ms, busy if avg power > "
                  << sense_threshold_db << " dB\n";
        transceiver.run_channel_sense(sense_window_ms / 1000.0,
                                      sense_threshold_db, sense_count);
        transceiver.stop();
        return EXIT_SUCCESS;
    }

    // ── Run according to role ───────────────────────────────
    if (config.role == "source_arq") {
        // Stop-and-wait ARQ transmitter: send each chunk, wait for its ACK,
        // retransmit on timeout, advance when ACKed. Exits when every chunk is
        // ACKed (or gives up per SOURCE max-attempts).
        std::unique_ptr<AckLink> ack;
        if (config.ack_transport == "tcp") {
            // Source is the TCP client — connect to the sink (retry until it's up).
            int fd = -1;
            for (int i = 0; i < 60 && fd < 0 && !global_stop_signal.load(); ++i) {
                try { fd = net::connect_to(config.ack_host, config.ack_port); }
                catch (const std::exception&) {
                    if (i == 0) std::cout << "[SOURCE-ARQ] waiting for sink ACK server at "
                                          << config.ack_host << ":" << config.ack_port << " ...\n";
                    std::this_thread::sleep_for(std::chrono::milliseconds(300));
                }
            }
            if (fd < 0) { std::cerr << "[SOURCE-ARQ] could not connect ACK socket\n"; transceiver.stop(); return EXIT_FAILURE; }
            std::cout << "[SOURCE-ARQ] ACK socket connected to " << config.ack_host
                      << ":" << config.ack_port << "\n";
            ack.reset(new TcpAckLink(fd));
        } else {
            ack.reset(new RfAckLink(transceiver, bytes_length, config.fec));
        }
        std::cout << "[SOURCE-ARQ] " << chunks.size() << " chunks, timeout "
                  << timeout_ms << " ms, ACK via " << ack->name()
                  << (config.fec ? ", FEC on" : "") << ". Ctrl-C to abort.\n";
        SOURCE source(transceiver, *ack, timeout_ms, num_bits, max_attempts, config.fec);
        source.start(chunks);
        while (!source.done() && !global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        source.stop();
        std::cout << "[SOURCE-ARQ] Finished.\n";

    } else if (config.role == "sink_arq") {
        // Stop-and-wait ARQ receiver: CRC-verify each data chunk, send an ACK,
        // reassemble. Exits once all chunks are received.
        std::unique_ptr<AckLink> ack;
        if (config.ack_transport == "tcp") {
            // Sink is the TCP server — accept the source's ACK connection.
            std::cout << "[SINK-ARQ] waiting for source to connect ACK socket on port "
                      << config.ack_port << " ...\n";
            int fd;
            try { fd = net::accept_one(config.ack_port); }
            catch (const std::exception& e) { std::cerr << "[SINK-ARQ] ACK accept failed: "
                      << e.what() << "\n"; transceiver.stop(); return EXIT_FAILURE; }
            std::cout << "[SINK-ARQ] ACK socket connected\n";
            ack.reset(new TcpAckLink(fd));
        } else {
            ack.reset(new RfAckLink(transceiver, bytes_length, config.fec));
        }
        std::cout << "[SINK-ARQ] Waiting for chunks; ACKing verified ones via "
                  << ack->name() << (config.fec ? ", FEC on" : "") << ". Ctrl-C to stop.\n";
        SINK sink(transceiver, *ack, timer_interval, config.fec, bytes_length);
        sink.start();
        while (!sink.done() && !global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        // Grace period so a lost final ACK is re-sent when the source retransmits
        // its last chunk (the sink re-ACKs duplicates).
        if (sink.done())
            std::this_thread::sleep_for(std::chrono::milliseconds(2000));
        sink.stop();
        sink.print_received_message();
        if (!out_file.empty()) sink.save_message(out_file);

    } else if (config.role == "tx") {
        // ONE-WAY TRANSMIT (no ARQ).  --tx-mode burst = a finite number of
        // transmissions (--tx-reps) with --interval gaps; --tx-mode continuous =
        // transmit until Ctrl-C (a repeating data loop, or an unbroken carrier for
        // the sine/cosine test tone).
        const bool continuous = (tx_mode == "continuous");
        if (is_tone) {
            // ── Raw test-tone generator: push samples straight to the USRP,
            //    bypassing modulation & pulse-shaping. ──
            const bool   cosine = (message_type == "cosine");
            const int    N      = 8000;                                // ~5 ms/block @ 1.6 MHz
            const double dphi   = 2.0 * M_PI * tone_freq / config.tx_rate;
            double phase = 0.0;
            std::cout << "[TX] Test tone: " << message_type << " @ " << tone_freq/1e3
                      << " kHz  amp=" << tone_amp << "  (" << tx_mode
                      << ").  Ctrl-C to stop.\n";
            auto gen = [&](std::vector<std::complex<float>>& blk) {
                blk.resize(N);
                for (int n = 0; n < N; ++n) {
                    float s = tone_amp * static_cast<float>(cosine ? std::cos(phase)
                                                                   : std::sin(phase));
                    blk[n] = std::complex<float>(s, 0.0f);
                    phase += dphi; if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
                }
            };
            std::vector<std::complex<float>> blk;
            if (continuous) {                                          // unbroken carrier
                while (!global_stop_signal.load()) {
                    if (transceiver.tx_pending() < 8) {                // keep the FIFO fed
                        gen(blk); transceiver.transmit_samples(blk);
                    } else std::this_thread::sleep_for(std::chrono::milliseconds(2));
                }
            } else {                                                   // tone bursts
                for (int r = 0; r < tx_reps && !global_stop_signal.load(); ++r) {
                    gen(blk); transceiver.transmit_samples(blk);
                    std::cout << "[TX] tone burst " << r + 1 << "/" << tx_reps << "\n";
                    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
                }
            }
        } else {
            // ── Framed data (bytes / random): burst = tx_reps cycles then stop,
            //    continuous = loop the message until Ctrl-C. ──
            uint8_t total = static_cast<uint8_t>(chunks.size());
            std::cout << "[TX] One-way transmit: " << (int)total << " chunk(s), scheme "
                      << config.scheme << "  ("
                      << (continuous ? std::string("continuous")
                                     : std::to_string(tx_reps) + " reps")
                      << ").  Ctrl-C to stop.\n";
            for (int r = 0; (continuous || r < tx_reps) && !global_stop_signal.load(); ++r) {
                for (uint8_t idx = 0; idx < total && !global_stop_signal.load(); ++idx) {
                    auto bits = build_packet_bits(chunks[idx], idx, total);
                    if (config.fec) bits = fec_encode_block(bits);   // rate-1/2 K=7
                    transceiver.transmit(bits);
                    std::cout << "[TX] queued chunk " << (int)idx + 1 << "/" << (int)total
                              << (continuous ? "  (continuous)"
                                             : "  (rep " + std::to_string(r + 1) + "/"
                                               + std::to_string(tx_reps) + ")") << "\n";
                    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
                }
            }
        }
        // let the final burst drain out of the TX pipeline before shutting down
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        std::cout << "[TX] Done.\n";

    } else if (config.role == "rx" && is_tone) {
        // TONE MONITOR. Stream raw samples and report the dominant tone's
        // frequency + power directly (no decode pipeline). Works with either
        // continuous or burst transmit — it doesn't rely on burst detection.
        std::cout << "[RX] Tone monitor for --message-type " << message_type << ".\n";
        transceiver.run_tone_monitor(global_stop_signal);

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
                  << ". "
                  << (stop_on_complete ? "Auto-stops when all chunks verified, or "
                                       : "Keeps receiving (--stop-on-complete false) until ")
                  << rx_idle_timeout
                  << " s after the last burst (Ctrl-C also stops).\n";
        std::vector<std::string> parts;
        std::vector<bool>        got;
        int total = 0;
        long rx_bursts = 0, crc_pass = 0, crc_fail = 0;
        bool got_any = false, announced_complete = false;
        auto last_rx = std::chrono::steady_clock::now();
        while (!global_stop_signal.load()) {
            std::pair<size_t, std::vector<uint8_t>> rx;
            if (transceiver.rx_bits_fifo.pop(rx)) {
                // Any burst (valid or not) counts as TX activity: keep the link
                // alive while the transmitter is still sending.
                got_any = true;
                rx_bursts++;
                last_rx = std::chrono::steady_clock::now();
                // FEC: Viterbi-decode the (encoded) demod bits back to packet
                // bits before the CRC check. Truncate any symbol padding first.
                std::vector<uint8_t> raw = rx.second;
                if (config.fec) {
                    int coded = fec_encoded_len(16 + (int)bytes_length * 8 + 16);
                    if ((int)raw.size() >= coded) raw.resize(coded);
                    raw = fec_decode_block(raw);
                }
                auto [idx, tot, payload, crc_ok] = decode_packet_bits(raw);
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
                          << "  (" << have << "/" << (int)tot << " verified)";
                // Text is printed inline; random bytes aren't readable, so only
                // the final hex summary is shown (below), not per-chunk garbage.
                if (message_type != "random")
                    std::cout << ": \"" << payload << "\"";
                std::cout << "\n";
                if (have == total) {
                    if (stop_on_complete) {
                        std::cout << "[RX] all " << total
                                  << " chunks CRC-verified — message complete, stopping.\n";
                        break;
                    } else if (!announced_complete) {
                        announced_complete = true;
                        std::cout << "[RX] all " << total << " chunks CRC-verified — "
                                  << "message complete (--stop-on-complete false: still "
                                  << "receiving; stops on idle timeout / Ctrl-C).\n";
                    }
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
        if (!out_file.empty()) {
            std::ofstream o(out_file, std::ios::binary);
            if (o) { o.write(full.data(), (std::streamsize)full.size());
                     std::cout << "[RX] wrote " << full.size() << " bytes to "
                               << out_file << "\n"; }
            else std::cerr << "[RX] could not open out-file " << out_file << "\n";
        }
        std::cout << "\n================ DECODED MESSAGE ================\n";
        if (message_type == "random") {
            // Random bytes aren't readable — show a byte count + hex preview.
            static const char* HX = "0123456789ABCDEF";
            std::string hs;
            for (size_t i = 0; i < full.size() && i < 32; ++i) {
                unsigned char c = static_cast<unsigned char>(full[i]);
                hs += HX[c >> 4]; hs += HX[c & 0xF]; hs += ' ';
            }
            std::cout << "[random payload] " << full.size()
                      << " bytes, all CRC-verified (bit-error free).\n"
                      << "hex: " << hs << (full.size() > 32 ? "..." : "") << "\n";
        } else {
            std::cout << full << "\n";
        }
        std::cout << "=================================================\n";

    } else if (mode == "source") {   // role both (legacy single-box loopback ARQ)
        RfAckLink ack(transceiver, bytes_length, config.fec);   // ACK over RF (loopback)
        SOURCE source(transceiver, ack, timeout_ms, num_bits, max_attempts, config.fec);
        source.start(chunks);

        std::cout << "[SOURCE] Running — Ctrl-C to stop\n";
        while (!global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(500));

        source.stop();

    } else {   // role both, mode sink (legacy single-box loopback ARQ)
        RfAckLink ack(transceiver, bytes_length, config.fec);   // ACK over RF (loopback)
        SINK sink(transceiver, ack, timer_interval, config.fec, bytes_length);
        sink.start();

        std::cout << "[SINK] Running — Ctrl-C to stop\n";
        while (!global_stop_signal.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(500));

        sink.stop();
        sink.print_received_message();
    }

    transceiver.stop();

    // Auto-render the figure from the captured signals (best effort — needs
    // python3 + numpy + matplotlib). Data files remain either way.
    if (viz_on) {
        std::string script;
        for (const char* c : {"tools/plot_viz.py", "../tools/plot_viz.py",
                              "../../tools/plot_viz.py"})
            if (std::filesystem::exists(c)) { script = c; break; }
        if (!script.empty()) {
            std::string cmd = "MPLBACKEND=Agg python3 \"" + script + "\" \"" + viz::dir
                + "\" --fs " + std::to_string(config.rx_rate)
                + " --save \"" + viz::dir + "/figure.png\" 2>/dev/null";
            int rc = std::system(cmd.c_str());
            if (rc == 0)
                std::cout << "[VIZ] figure saved to " << viz::dir << "/figure.png\n";
            else
                std::cout << "[VIZ] auto-plot failed (need python3+numpy+matplotlib). "
                             "Run: python3 " << script << " " << viz::dir << "\n";
        } else {
            std::cout << "[VIZ] plot_viz.py not found; run it on " << viz::dir << " to plot.\n";
        }
    }

    std::cout << "[MAIN] Finished\n";
    return EXIT_SUCCESS;
}
