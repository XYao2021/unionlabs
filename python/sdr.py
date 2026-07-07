"""
sdr.py — Python wrapper for the USRP B210 SDR PHY (sdr_system).

AUTO-GENERATED from `sdr_system --help` by tools/gen_python_api.py.
DO NOT EDIT — rerun the generator (or rebuild the C++) to refresh.

Quick start:
    from sdr import SDR, tx, rx, sink_arq, source_arq, run_pair
    tx(scheme="QPSK", tx_gain=78, fec=True).run()          # one process
    run_pair(sink_arq(scheme="QPSK", fec=True),            # BOTH ends,
             source_arq(scheme="QPSK", fec=True))          #   RX then TX
"""
import os, shlex, subprocess, time

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BINARY = os.environ.get("SDR_SYSTEM_BIN") or \
    os.path.normpath(os.path.join(_HERE, "..", "build", "sdr_system"))

# cpp-option-name -> (has_arg, default, help)
OPTIONS = {
    "mode": (True, 'source', "Operation mode: source or sink"),
    "role": (True, 'both', "tx = transmit only (one B210), rx = receive only (other B210), both = original single-box full-duplex/loopbac k"),
    "tx-reps": (True, '20', "role tx: how many times to cycle through all chunks (one-way, no ACK)"),
    "rx-idle-timeout": (True, '8', "role rx: auto-stop and print the message after this many seconds with no new bursts (TX has finished). 0 = run until Ctrl-C"),
    "ack-transport": (True, 'tcp', "ARQ ACK channel: tcp (default, ACK over a socket — no reverse RF needed) or rf (ACK over the second RF path, RF B)"),
    "ack-host": (True, '127.0.0.1', "TCP ACK: host/IP of the sink that the source connects to (default localhost)"),
    "ack-port": (True, '5599', "TCP ACK: socket port"),
    "skip-rate-check": (False, None, "bypass the startup rate-chain consistency check (run even if rates mismatch)"),
    "viz": (True, '1', "capture TX/RX signals and auto-save the plot to <viz-dir>/<scheme>/figure.png (default true; --viz false disables)"),
    "viz-dir": (True, 'viz', "base directory for --viz output (a per-modulation subfolder is made)"),
    "timeout": (True, '3000', "ACK timeout in ms (source)"),
    "timer_interval": (True, '1000', "ACK timer interval in ms (sink)"),
    "num_bits": (True, '1000', "Payload bits per packet"),
    "interval": (True, '3000', "TX interval between packets (ms)"),
    "tx-mode": (True, 'burst', "role tx transmission mode: burst (discrete packets/tone bursts with --interval gaps, repeated --tx-reps times then stop) or continuous (transmit until Ctrl-C — a continuous data loop or an unbroken carrier for sine/cosine)"),
    "message-type": (True, 'bytes', "payload: bytes (given text, default Star Wars crawl; set with --message) | random (num_bits random bits) | sine | cosine (raw baseband test tone, role tx only)"),
    "message": (True, None, "text payload for --message-type bytes (overrides the default Star Wars crawl)"),
    "tone-freq": (True, '100000', "sine/cosine baseband frequency in Hz (default 100 kHz)"),
    "tone-amp": (True, '0.5', "sine/cosine amplitude, keep < 1.0 to avoid DAC clipping"),
    "preamble": (True, 'm-sequence', "Preamble type: m-sequence or zadoff"),
    "m": (True, '5', "m-sequence order (length = 2^m-1)"),
    "add_preamble": (True, '1', "Prepend preamble to each packet"),
    "U": (True, '2', "TX pulse-shaper upsampling factor (wire sps = U/D)"),
    "D": (True, '1', "TX pulse-shaper downsampling factor (wire sps = U/D)"),
    "filter_type": (True, 'rrc', "Filter type: rrc / rc / lp"),
    "symbol_rate": (True, '800000', "Symbol rate (Hz)"),
    "num_taps": (True, '151', "Filter tap count"),
    "roll_off": (True, '0.25', "Roll-off factor"),
    "num_threads": (True, '1', "FFT threads"),
    "tx-args": (True, None, "UHD TX device args (e.g. serial=XXXXXXX)"),
    "tx-rate": (True, '1600000', "TX sample rate (Hz) = symbol_rate * U/D"),
    "tx-freq": (True, '2412000000', "TX centre frequency (Hz)"),
    "tx-gain": (True, '20', "TX gain (dB)"),
    "tx-bw": (True, '1000000', "TX bandwidth (Hz)"),
    "tx-ant": (True, 'TX/RX', "TX antenna port"),
    "tx-channel": (True, '0', "TX channel index"),
    "tx-subdev": (True, 'A:A', "TX subdev spec"),
    "rx-args": (True, None, "UHD RX device args"),
    "rx-rate": (True, '1600000', "RX sample rate (Hz) = tx_rate (integer samples/symbol)"),
    "rx-freq": (True, '2412000000', "RX centre frequency (Hz)"),
    "rx-gain": (True, '30', "RX gain (dB)"),
    "rx-bw": (True, '1000000', "RX bandwidth (Hz)"),
    "rx-ant": (True, 'RX2', "RX antenna port"),
    "rx-channel": (True, '0', "RX channel index"),
    "rx-subdev": (True, 'A:A', "RX subdev spec"),
    "ref": (True, 'internal', "Clock reference: internal / external / mimo"),
    "settling": (True, '0.20000000000000001) Settling time (s', ""),
    "uhd_timeout": (True, '1000', "UHD TX timeout (ms)"),
    "alpha": (True, '0.949999988', "Energy-detector IIR smoothing: filtered = (1-alpha)*inst + alpha*prev, so LARGER alpha = MORE smoothing. The old 0.02 barely smoothed, so the detector fired on every noise spike (thousands of false bursts) and cut real bursts apart on the RRC envelope. 0.95 (~20-sample time constant) gives one clean capture per burst."),
    "energy_threshold": (True, '1.00000001e-07', "Fixed energy threshold (used only if the adaptive detector is off). Alias: --det-threshold"),
    "det-threshold": (True, None, "alias for --energy_threshold (fixed detector threshold)"),
    "energy_packet_size": (True, '3300', "Samples to collect after energy detection"),
    "IIR_window_size": (True, '20', "IIR window size"),
    "IIR_threshold_adaptive": (True, '1', "Use the auto (adaptive) detection threshold = noise_floor × multiplier. Alias: --det-adaptive"),
    "det-adaptive": (True, None, "alias for --IIR_threshold_adaptive (use the auto detector threshold)"),
    "IIR_threshold_multiplier": (True, '5', "Auto detector threshold = measured noise_floor × this. RAISE it over-the-air (e.g. 10-30) so the detector fires only on real bursts, not ambient RF; too high and it misses weak bursts. Alias: --det-mult"),
    "det-mult": (True, None, "alias for --IIR_threshold_multiplier (auto-threshold noise multiplier)"),
    "det-continuous": (True, '1', "Continuously re-track the noise floor during idle (default true), vs a one-shot startup calibration. Robust to drifting ambient noise."),
    "sps_sync": (True, '5', "Samples per symbol at match-filter output"),
    "sync_threshold": (True, '15', "ACQ correlation threshold — raise it over-the-air so ambient-noise bursts are rejected (a real preamble peaks near the preamble length ~31 after AGC; noise correlates far lower). Watch the '[ACQ] Peak correlation' lines and set it below the true peak but above the noise. Alias: --sync-threshold"),
    "sync-threshold": (True, None, "alias for --sync_threshold (hyphenated spelling)"),
    "recv_msg_len": (True, '508', "Data symbols to extract (QPSK: 1016bits/2bps=508)"),
    "samps_per_buff": (True, '10000', "Samples per UHD receive buffer"),
    "num_recv_request": (True, '0', "Total samples to receive (0=continuous)"),
    "AGC_type": (True, 'Feed', "AGC type: Feed or Closed"),
    "dc-block": (True, '0', "Experimental per-burst DC-block high-pass on the RX (default false). A gentle cutoff barely dents the cable leakage; an aggressive one distorts the preamble and breaks sync — prefer --tx-dc-i/--tx-dc-q."),
    "tx-dc-i": (True, '0', "Manual TX LO-leakage null, I component (normalized [-1,1]). Tune with --tx-dc-q to minimize the RX DC spike on a direct cable (dense QAM)."),
    "tx-dc-q": (True, '0', "Manual TX LO-leakage null, Q component (normalized [-1,1])."),
    "scheme": (True, 'QPSK', "Modulation: QPSK / DQPSK / DBPSK / 16-QAM / ..."),
    "fec": (True, '0', "Forward Error Correction (rate-1/2 K=7 convolutional + Viterbi). Corrects bit errors so a noisy link decodes error-free; must match on both ends. Halves the payload rate (2x the symbols)."),
    "waveform": (True, 'sc', "Waveform: sc (single-carrier) or ofdm. OFDM handles multipath/CFO natively (per-subcarrier equalization) — best for dense QAM."),
    "ofdm-fft": (True, '64', "OFDM FFT size (number of subcarriers)"),
    "ofdm-cp": (True, '16', "OFDM cyclic-prefix length (>= channel delay spread)"),
    "ofdm-tx-peak": (True, '0.5', "OFDM TX peak scaling (high PAPR — keep the DAC out of clipping)"),
    "sps": (True, '2', "Samples per symbol (informational)"),
    "timing_loop_bw": (True, '0.0149999997', "Gardner TED loop bandwidth BnT"),
    "timing_damping": (True, '0.707000017', "Gardner TED damping factor"),
    "phase_loop_bw": (True, '0.0199999996', "Phase PLL loop bandwidth"),
    "phase_damping": (True, '0.707000017', "Phase PLL damping factor"),
    "eq_taps": (True, '11', "Number of equaliser taps"),
    "eq_mu": (True, '0.300000012', "Equalizer NLMS step (used for DD tracking / real-preamble training)"),
    "eq_dd": (True, '0', "Equalizer decision-directed tracking after training (default off: the LS-trained eq is exact frozen; DD can diverge on noisy dense QAM)"),
    "eq_type": (True, 'None', "Equaliser type: LMS / RLS / DFE / None. Default None: on a clean cabled link no equaliser is needed, and the LMS loop currently DIVERGES on the real signal (decision-directed error grows and destroys the symbols) — verified on hardware, where None decodes the message and LMS produces garbage. Leave None until the LMS/RLS/DFE update is debugged."),
}

# python-identifier -> cpp-option-name
PY2CPP = {
    "mode": "mode",
    "role": "role",
    "tx_reps": "tx-reps",
    "rx_idle_timeout": "rx-idle-timeout",
    "ack_transport": "ack-transport",
    "ack_host": "ack-host",
    "ack_port": "ack-port",
    "skip_rate_check": "skip-rate-check",
    "viz": "viz",
    "viz_dir": "viz-dir",
    "timeout": "timeout",
    "timer_interval": "timer_interval",
    "num_bits": "num_bits",
    "interval": "interval",
    "tx_mode": "tx-mode",
    "message_type": "message-type",
    "message": "message",
    "tone_freq": "tone-freq",
    "tone_amp": "tone-amp",
    "preamble": "preamble",
    "m": "m",
    "add_preamble": "add_preamble",
    "U": "U",
    "D": "D",
    "filter_type": "filter_type",
    "symbol_rate": "symbol_rate",
    "num_taps": "num_taps",
    "roll_off": "roll_off",
    "num_threads": "num_threads",
    "tx_args": "tx-args",
    "tx_rate": "tx-rate",
    "tx_freq": "tx-freq",
    "tx_gain": "tx-gain",
    "tx_bw": "tx-bw",
    "tx_ant": "tx-ant",
    "tx_channel": "tx-channel",
    "tx_subdev": "tx-subdev",
    "rx_args": "rx-args",
    "rx_rate": "rx-rate",
    "rx_freq": "rx-freq",
    "rx_gain": "rx-gain",
    "rx_bw": "rx-bw",
    "rx_ant": "rx-ant",
    "rx_channel": "rx-channel",
    "rx_subdev": "rx-subdev",
    "ref": "ref",
    "settling": "settling",
    "uhd_timeout": "uhd_timeout",
    "alpha": "alpha",
    "energy_threshold": "energy_threshold",
    "det_threshold": "det-threshold",
    "energy_packet_size": "energy_packet_size",
    "IIR_window_size": "IIR_window_size",
    "IIR_threshold_adaptive": "IIR_threshold_adaptive",
    "det_adaptive": "det-adaptive",
    "IIR_threshold_multiplier": "IIR_threshold_multiplier",
    "det_mult": "det-mult",
    "det_continuous": "det-continuous",
    "sps_sync": "sps_sync",
    "sync_threshold": "sync_threshold",
    "recv_msg_len": "recv_msg_len",
    "samps_per_buff": "samps_per_buff",
    "num_recv_request": "num_recv_request",
    "AGC_type": "AGC_type",
    "dc_block": "dc-block",
    "tx_dc_i": "tx-dc-i",
    "tx_dc_q": "tx-dc-q",
    "scheme": "scheme",
    "fec": "fec",
    "waveform": "waveform",
    "ofdm_fft": "ofdm-fft",
    "ofdm_cp": "ofdm-cp",
    "ofdm_tx_peak": "ofdm-tx-peak",
    "sps": "sps",
    "timing_loop_bw": "timing_loop_bw",
    "timing_damping": "timing_damping",
    "phase_loop_bw": "phase_loop_bw",
    "phase_damping": "phase_damping",
    "eq_taps": "eq_taps",
    "eq_mu": "eq_mu",
    "eq_dd": "eq_dd",
    "eq_type": "eq_type",
}

_UNSET = object()


def _resolve(key):
    if key in OPTIONS: return key
    if key in PY2CPP:  return PY2CPP[key]
    alt = key.replace("_", "-")
    if alt in OPTIONS: return alt
    raise KeyError("unknown sdr_system option: %r" % key)


class SDR:
    """One sdr_system invocation. Set any --option as a keyword (hyphens or
    underscores both work); call .run() (blocking) or .popen() (background)."""
    def __init__(self,
                 mode=_UNSET, # =source  Operation mode: source or sink
                 role=_UNSET, # =both  tx = transmit only (one B210), rx = receive only (other...
                 tx_reps=_UNSET, # =20  role tx: how many times to cycle through all chunks (one-...
                 rx_idle_timeout=_UNSET, # =8  role rx: auto-stop and print the message after this many...
                 ack_transport=_UNSET, # =tcp  ARQ ACK channel: tcp (default, ACK over a socket — no...
                 ack_host=_UNSET, # =127.0.0.1  TCP ACK: host/IP of the sink that the source connects to...
                 ack_port=_UNSET, # =5599  TCP ACK: socket port
                 skip_rate_check=_UNSET, # flag  bypass the startup rate-chain consistency check (run even...
                 viz=_UNSET, # =1  capture TX/RX signals and auto-save the plot to <viz-...
                 viz_dir=_UNSET, # =viz  base directory for --viz output (a per-modulation subfolder...
                 timeout=_UNSET, # =3000  ACK timeout in ms (source)
                 timer_interval=_UNSET, # =1000  ACK timer interval in ms (sink)
                 num_bits=_UNSET, # =1000  Payload bits per packet
                 interval=_UNSET, # =3000  TX interval between packets (ms)
                 tx_mode=_UNSET, # =burst  role tx transmission mode: burst (discrete packets/tone...
                 message_type=_UNSET, # =bytes  payload: bytes (given text, default Star Wars crawl; set...
                 message=_UNSET, # text payload for --message-type bytes (overrides the...
                 tone_freq=_UNSET, # =100000  sine/cosine baseband frequency in Hz (default 100 kHz)
                 tone_amp=_UNSET, # =0.5  sine/cosine amplitude, keep < 1.0 to avoid DAC clipping
                 preamble=_UNSET, # =m-sequence  Preamble type: m-sequence or zadoff
                 m=_UNSET, # =5  m-sequence order (length = 2^m-1)
                 add_preamble=_UNSET, # =1  Prepend preamble to each packet
                 U=_UNSET, # =2  TX pulse-shaper upsampling factor (wire sps = U/D)
                 D=_UNSET, # =1  TX pulse-shaper downsampling factor (wire sps = U/D)
                 filter_type=_UNSET, # =rrc  Filter type: rrc / rc / lp
                 symbol_rate=_UNSET, # =800000  Symbol rate (Hz)
                 num_taps=_UNSET, # =151  Filter tap count
                 roll_off=_UNSET, # =0.25  Roll-off factor
                 num_threads=_UNSET, # =1  FFT threads
                 tx_args=_UNSET, # UHD TX device args (e.g. serial=XXXXXXX)
                 tx_rate=_UNSET, # =1600000  TX sample rate (Hz) = symbol_rate * U/D
                 tx_freq=_UNSET, # =2412000000  TX centre frequency (Hz)
                 tx_gain=_UNSET, # =20  TX gain (dB)
                 tx_bw=_UNSET, # =1000000  TX bandwidth (Hz)
                 tx_ant=_UNSET, # =TX/RX  TX antenna port
                 tx_channel=_UNSET, # =0  TX channel index
                 tx_subdev=_UNSET, # =A:A  TX subdev spec
                 rx_args=_UNSET, # UHD RX device args
                 rx_rate=_UNSET, # =1600000  RX sample rate (Hz) = tx_rate (integer samples/symbol)
                 rx_freq=_UNSET, # =2412000000  RX centre frequency (Hz)
                 rx_gain=_UNSET, # =30  RX gain (dB)
                 rx_bw=_UNSET, # =1000000  RX bandwidth (Hz)
                 rx_ant=_UNSET, # =RX2  RX antenna port
                 rx_channel=_UNSET, # =0  RX channel index
                 rx_subdev=_UNSET, # =A:A  RX subdev spec
                 ref=_UNSET, # =internal  Clock reference: internal / external / mimo
                 settling=_UNSET, # =0.20000000000000001) Settling time (s  
                 uhd_timeout=_UNSET, # =1000  UHD TX timeout (ms)
                 alpha=_UNSET, # =0.949999988  Energy-detector IIR smoothing: filtered = (1-alpha)*inst +...
                 energy_threshold=_UNSET, # =1.00000001e-07  Fixed energy threshold (used only if the adaptive detector...
                 det_threshold=_UNSET, # alias for --energy_threshold (fixed detector threshold)
                 energy_packet_size=_UNSET, # =3300  Samples to collect after energy detection
                 IIR_window_size=_UNSET, # =20  IIR window size
                 IIR_threshold_adaptive=_UNSET, # =1  Use the auto (adaptive) detection threshold = noise_floor ×...
                 det_adaptive=_UNSET, # alias for --IIR_threshold_adaptive (use the auto detector...
                 IIR_threshold_multiplier=_UNSET, # =5  Auto detector threshold = measured noise_floor × this....
                 det_mult=_UNSET, # alias for --IIR_threshold_multiplier (auto-threshold noise...
                 det_continuous=_UNSET, # =1  Continuously re-track the noise floor during idle (default...
                 sps_sync=_UNSET, # =5  Samples per symbol at match-filter output
                 sync_threshold=_UNSET, # =15  ACQ correlation threshold — raise it over-the-air so...
                 recv_msg_len=_UNSET, # =508  Data symbols to extract (QPSK: 1016bits/2bps=508)
                 samps_per_buff=_UNSET, # =10000  Samples per UHD receive buffer
                 num_recv_request=_UNSET, # =0  Total samples to receive (0=continuous)
                 AGC_type=_UNSET, # =Feed  AGC type: Feed or Closed
                 dc_block=_UNSET, # =0  Experimental per-burst DC-block high-pass on the RX...
                 tx_dc_i=_UNSET, # =0  Manual TX LO-leakage null, I component (normalized [-1,1])....
                 tx_dc_q=_UNSET, # =0  Manual TX LO-leakage null, Q component (normalized [-1,1]).
                 scheme=_UNSET, # =QPSK  Modulation: QPSK / DQPSK / DBPSK / 16-QAM / ...
                 fec=_UNSET, # =0  Forward Error Correction (rate-1/2 K=7 convolutional +...
                 waveform=_UNSET, # =sc  Waveform: sc (single-carrier) or ofdm. OFDM handles...
                 ofdm_fft=_UNSET, # =64  OFDM FFT size (number of subcarriers)
                 ofdm_cp=_UNSET, # =16  OFDM cyclic-prefix length (>= channel delay spread)
                 ofdm_tx_peak=_UNSET, # =0.5  OFDM TX peak scaling (high PAPR — keep the DAC out of...
                 sps=_UNSET, # =2  Samples per symbol (informational)
                 timing_loop_bw=_UNSET, # =0.0149999997  Gardner TED loop bandwidth BnT
                 timing_damping=_UNSET, # =0.707000017  Gardner TED damping factor
                 phase_loop_bw=_UNSET, # =0.0199999996  Phase PLL loop bandwidth
                 phase_damping=_UNSET, # =0.707000017  Phase PLL damping factor
                 eq_taps=_UNSET, # =11  Number of equaliser taps
                 eq_mu=_UNSET, # =0.300000012  Equalizer NLMS step (used for DD tracking / real-preamble...
                 eq_dd=_UNSET, # =0  Equalizer decision-directed tracking after training...
                 eq_type=_UNSET, # =None  Equaliser type: LMS / RLS / DFE / None. Default None: on a...
                 binary=None, extra=None):
        self.binary = binary or DEFAULT_BINARY
        self.opts = {}
        self.extra = list(extra or [])
        _kw = dict(
            mode=mode,
            role=role,
            tx_reps=tx_reps,
            rx_idle_timeout=rx_idle_timeout,
            ack_transport=ack_transport,
            ack_host=ack_host,
            ack_port=ack_port,
            skip_rate_check=skip_rate_check,
            viz=viz,
            viz_dir=viz_dir,
            timeout=timeout,
            timer_interval=timer_interval,
            num_bits=num_bits,
            interval=interval,
            tx_mode=tx_mode,
            message_type=message_type,
            message=message,
            tone_freq=tone_freq,
            tone_amp=tone_amp,
            preamble=preamble,
            m=m,
            add_preamble=add_preamble,
            U=U,
            D=D,
            filter_type=filter_type,
            symbol_rate=symbol_rate,
            num_taps=num_taps,
            roll_off=roll_off,
            num_threads=num_threads,
            tx_args=tx_args,
            tx_rate=tx_rate,
            tx_freq=tx_freq,
            tx_gain=tx_gain,
            tx_bw=tx_bw,
            tx_ant=tx_ant,
            tx_channel=tx_channel,
            tx_subdev=tx_subdev,
            rx_args=rx_args,
            rx_rate=rx_rate,
            rx_freq=rx_freq,
            rx_gain=rx_gain,
            rx_bw=rx_bw,
            rx_ant=rx_ant,
            rx_channel=rx_channel,
            rx_subdev=rx_subdev,
            ref=ref,
            settling=settling,
            uhd_timeout=uhd_timeout,
            alpha=alpha,
            energy_threshold=energy_threshold,
            det_threshold=det_threshold,
            energy_packet_size=energy_packet_size,
            IIR_window_size=IIR_window_size,
            IIR_threshold_adaptive=IIR_threshold_adaptive,
            det_adaptive=det_adaptive,
            IIR_threshold_multiplier=IIR_threshold_multiplier,
            det_mult=det_mult,
            det_continuous=det_continuous,
            sps_sync=sps_sync,
            sync_threshold=sync_threshold,
            recv_msg_len=recv_msg_len,
            samps_per_buff=samps_per_buff,
            num_recv_request=num_recv_request,
            AGC_type=AGC_type,
            dc_block=dc_block,
            tx_dc_i=tx_dc_i,
            tx_dc_q=tx_dc_q,
            scheme=scheme,
            fec=fec,
            waveform=waveform,
            ofdm_fft=ofdm_fft,
            ofdm_cp=ofdm_cp,
            ofdm_tx_peak=ofdm_tx_peak,
            sps=sps,
            timing_loop_bw=timing_loop_bw,
            timing_damping=timing_damping,
            phase_loop_bw=phase_loop_bw,
            phase_damping=phase_damping,
            eq_taps=eq_taps,
            eq_mu=eq_mu,
            eq_dd=eq_dd,
            eq_type=eq_type
        )
        for _k, _v in _kw.items():
            if _v is not _UNSET:
                self.set(**{_k: _v})

    def set(self, **opts):
        for k, v in opts.items():
            self.opts[_resolve(k)] = v
        return self

    def argv(self):
        cmd = [self.binary]
        for k, v in self.opts.items():
            has_arg = OPTIONS[k][0]
            if not has_arg:                         # bool_switch flag
                if v: cmd.append("--" + k)
            else:
                s = ("true" if v else "false") if isinstance(v, bool) else str(v)
                cmd += ["--" + k, s]
        return cmd + self.extra

    def command(self):
        return " ".join(shlex.quote(a) for a in self.argv())

    def run(self, **overrides):
        self.set(**overrides)
        return subprocess.run(self.argv())

    def popen(self, **overrides):
        self.set(**overrides)
        return subprocess.Popen(self.argv())

    def __repr__(self):
        return "SDR(%s)" % self.command()


# ── convenience constructors (pick a role) ───────────────────────────
def tx(**o):         return SDR(role="tx", **o)          # transmit only
def rx(**o):         return SDR(role="rx", **o)          # receive only
def sink_arq(**o):   return SDR(role="sink_arq", **o)    # ARQ receiver (2 boxes)
def source_arq(**o): return SDR(role="source_arq", **o) # ARQ sender   (2 boxes)
def both(**o):       return SDR(role="both", **o)        # 1 box: TX + RX at once
loopback = both                                          # alias


def run_pair(rx_side, tx_side, rx_head_start=4.0, rx_grace=20.0):
    """Drive BOTH ends: start the receiver, give it a head start, start the
    transmitter, wait for TX to finish, then wait (up to rx_grace s) for RX
    to self-terminate — else stop it. Returns (rx_returncode, tx_returncode)."""
    rxp = rx_side.popen()
    try:
        time.sleep(rx_head_start)
        txp = tx_side.popen()
        txp.wait()
        try:
            rxp.wait(timeout=rx_grace)
        except subprocess.TimeoutExpired:
            rxp.terminate()
            try: rxp.wait(timeout=5)
            except subprocess.TimeoutExpired: rxp.kill()
        return rxp.returncode, txp.returncode
    finally:
        if rxp.poll() is None:
            rxp.terminate()


def options():
    """Print every exposed option with its default and help."""
    for name, (ha, d, h) in OPTIONS.items():
        tag = "(flag)" if not ha else "= " + str(d)
        print("  --%-22s %-14s %s" % (name, tag, h))


if __name__ == "__main__":
    print("sdr_system binary:", DEFAULT_BINARY)
    print("%d options:\n" % len(OPTIONS)); options()
