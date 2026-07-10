#!/usr/bin/env python3
"""
jammer.py — active RF jammer built on the B210 SDR PHY.

Transmits a configurable interfering waveform to contest the channel — for
random-access / MARL experiments where you want a controllable interferer (make the
`channel-busy` observation meaningful, create real collisions, stress a policy).

Waveforms:
  chirp   LoRa/CSS linear frequency sweep (wideband energy across the band) — default,
          the most effective broadband jammer. Tune --chirp-bw / --chirp-sf.
  tone    a single sine/cosine carrier at --tone-freq (narrowband).
  scheme  a real modulated signal (random data of --scheme, e.g. QPSK) — looks like a
          legitimate transmitter, good for realistic contention.

Timing:
  --mode continuous  unbroken transmission for --duration seconds.
  --mode burst       repeated bursts of --burst-ms with --interval ms gaps, for
                     --duration seconds (a pulsed jammer / duty-cycled interferer).

Everything is configurable: --scheme, --tx-gain, --freq, --interval, --duration,
--tx-rate, --tx-args, chirp/tone params. Examples:

    # broadband chirp sweep, full band, 10 s, high power
    python3 jammer.py --waveform chirp --chirp-bw 1.6e6 --tx-gain 85 --duration 10
    # pulsed QPSK interferer: 20 ms bursts every 100 ms for 30 s
    python3 jammer.py --waveform scheme --scheme QPSK --mode burst \
        --burst-ms 20 --interval 100 --duration 30 --tx-gain 80
    # narrowband tone at +200 kHz
    python3 jammer.py --waveform tone --tone-freq 200e3 --tx-gain 75 --duration 5
    python3 jammer.py --dry-run ...        # print the command(s), transmit nothing
"""
import argparse
import os
import subprocess
import sys
import time

# import the auto-generated PHY wrapper from ../../python
_PHY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "python"))
sys.path.insert(0, _PHY)
import sdr  # noqa: E402


class Jammer:
    """Configurable active jammer over the B210 TX. Build a command with .command()
    or transmit for `duration_s` with .run()."""

    def __init__(self, waveform="chirp", scheme="QPSK", freq=915e6, tx_gain=80,
                 tx_rate=1.6e6, tx_args="serial=30CD424", mode="continuous",
                 interval_ms=100, burst_ms=20, duration_s=10.0,
                 chirp_bw=0.0, chirp_sf=8, chirp_down=False, tone_freq=200e3,
                 num_bits=16384, binary=None):
        self.waveform = waveform
        self.scheme = scheme
        self.freq = freq
        self.tx_gain = tx_gain
        self.tx_rate = tx_rate
        self.tx_args = tx_args
        self.mode = mode
        self.interval_ms = interval_ms
        self.burst_ms = burst_ms
        self.duration_s = duration_s
        self.chirp_bw = chirp_bw
        self.chirp_sf = chirp_sf
        self.chirp_down = chirp_down
        self.tone_freq = tone_freq
        self.num_bits = num_bits
        self.binary = binary

    def _opts(self):
        """Map (waveform, mode) -> sdr_system options."""
        o = dict(role="tx", tx_args=self.tx_args, tx_freq=self.freq,
                 tx_rate=self.tx_rate, tx_gain=self.tx_gain, tx_ant="TX/RX",
                 tx_subdev="A:A", viz=False, binary=self.binary)
        # waveform
        if self.waveform == "chirp":
            o.update(message_type="chirp", chirp_bw=self.chirp_bw, chirp_sf=self.chirp_sf)
            if self.chirp_down:
                o["chirp_down"] = True
        elif self.waveform == "tone":
            o.update(message_type="cosine", tone_freq=self.tone_freq)
        elif self.waveform == "scheme":
            o.update(message_type="random", scheme=self.scheme, num_bits=self.num_bits,
                     fec=True)
        else:
            raise ValueError("waveform must be chirp | tone | scheme")
        # timing
        if self.mode == "continuous":
            o["tx_mode"] = "continuous"
        elif self.mode == "burst":
            # native burst loop: many reps with --interval gaps; run() stops at duration.
            o.update(tx_mode="burst", interval=self.interval_ms, tx_reps=10_000_000)
        else:
            raise ValueError("mode must be continuous | burst")
        return o

    def command(self):
        return sdr.SDR(**self._opts()).command()

    def run(self, dry_run=False):
        cmd = self.command()
        print("[jammer] %s / %s  freq=%.3f MHz  tx-gain=%.0f  %s  duration=%.1fs"
              % (self.waveform, self.mode, self.freq / 1e6, self.tx_gain,
                 ("chirp-bw=%.0f kHz sf=%d" % ((self.chirp_bw or self.tx_rate) / 1e3,
                                               self.chirp_sf)) if self.waveform == "chirp"
                 else ("scheme=%s" % self.scheme if self.waveform == "scheme"
                       else "tone=%.0f kHz" % (self.tone_freq / 1e3)),
                 self.duration_s))
        print("[jammer] %s" % cmd)
        if dry_run:
            return 0
        import shlex
        p = subprocess.Popen(shlex.split(cmd))
        t0 = time.time()
        try:
            while time.time() - t0 < self.duration_s:
                if p.poll() is not None:
                    print("[jammer] tx process exited early (rc=%s)" % p.returncode)
                    return p.returncode or 1
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n[jammer] interrupted")
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("[jammer] stopped after %.1fs" % (time.time() - t0))
        return 0


def main(argv):
    a = argparse.ArgumentParser(description="Active RF jammer on the B210 SDR PHY",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    a.add_argument("--waveform", choices=["chirp", "tone", "scheme"], default="chirp")
    a.add_argument("--scheme", default="QPSK", help="modulation for --waveform scheme")
    a.add_argument("--freq", type=float, default=915e6, help="center frequency (Hz)")
    a.add_argument("--tx-gain", type=float, default=80)
    a.add_argument("--tx-rate", type=float, default=1.6e6)
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--mode", choices=["continuous", "burst"], default="continuous")
    a.add_argument("--interval", type=int, default=100, help="burst mode: ms between bursts")
    a.add_argument("--burst-ms", type=int, default=20, help="burst mode: burst length (ms)")
    a.add_argument("--duration", type=float, default=10.0, help="total jam time (s)")
    a.add_argument("--chirp-bw", type=float, default=0.0, help="chirp sweep BW (Hz); 0=full band")
    a.add_argument("--chirp-sf", type=int, default=8, help="chirp spreading factor 7-12")
    a.add_argument("--chirp-down", action="store_true", help="down-chirp instead of up")
    a.add_argument("--tone-freq", type=float, default=200e3, help="tone baseband freq (Hz)")
    a.add_argument("--num-bits", type=int, default=16384, help="scheme: random payload bits")
    a.add_argument("--dry-run", action="store_true", help="print the command, transmit nothing")
    args = a.parse_args(argv)

    j = Jammer(waveform=args.waveform, scheme=args.scheme, freq=args.freq,
               tx_gain=args.tx_gain, tx_rate=args.tx_rate, tx_args=args.tx_args,
               mode=args.mode, interval_ms=args.interval, burst_ms=args.burst_ms,
               duration_s=args.duration, chirp_bw=args.chirp_bw, chirp_sf=args.chirp_sf,
               chirp_down=args.chirp_down, tone_freq=args.tone_freq, num_bits=args.num_bits)
    sys.exit(j.run(dry_run=args.dry_run))


if __name__ == "__main__":
    main(sys.argv[1:])
