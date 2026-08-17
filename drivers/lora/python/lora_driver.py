#!/usr/bin/env python3
"""
lora_driver.py — the LoRa PHY behind the UNIFORM API.

This is the top of the LoRa driver and the only file the middleware talks to. It
implements the same seam every other backend implements, so nothing in
experiments/ or experiments/ changes when an experiment moves to LoRa:

    transfer(buf) -> (bytes_at_the_peer, info)        the channel contract that
                                                       union/phy_link.py's runners call
    LoRaDriver(PhyDriver): transfer / broadcast        the union/driver.py contract

    ./run.sh --algo fl --channel lora --lora-sf 9      federated learning over LoRa
    ./run.sh --algo dl --role gossip --channel lora    decentralized learning over LoRa
    ./run.sh --algo marl_multi --role multi --channel lora    random access over LoRa

WHAT `info` REPORTS  (the same keys the other channels report, plus LoRa's own)
    crc_ok    every fragment of the message arrived
    ber       0.0 on success — the SX1276 only surfaces CRC-valid packets, so a
              corrupted frame is a LOSS here, never a silently wrong payload
    frags     how many 249-byte fragments the message needed
    retx      stop-and-wait retransmissions
    airtime_ms  real Semtech airtime of the whole exchange — the honest cost of
              having sent this payload at this spreading factor

    A 200 kB model at SF12 is ~820 fragments and minutes of airtime. That is not a
    defect of this driver, it is what LoRa is; the number being visible is the point.

TWO SHAPES OF USE
    sim backend  — both ends in one process on a shared medium. Everything below is
                   testable with no radio.
    real radios  — the two nodes run as separate processes (one per host), which is
                   the tx/rx role split; see README.md.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import framing                                    # noqa: E402
from lora_radio import make_radio, SimMedium, time_on_air_ms, MTU   # noqa: E402

# the union middleware (PhyDriver lives there)
_UNION = os.path.join(HERE, "..", "..", "..", "union")
sys.path.insert(0, os.path.abspath(_UNION))
try:
    from driver import PhyDriver                  # noqa: E402
except ImportError:                               # driver.py is optional at import time
    class PhyDriver:                              # minimal stand-in
        pass

try:
    from phy_link import check_arq as _check_arq  # the middleware's ARQ registry
except ImportError:
    def _check_arq(scheme, phy, supported=("stop-and-wait",)):
        s = (scheme or "stop-and-wait").lower()
        if s not in supported:
            raise ValueError(f"the {phy} PHY does not implement ARQ scheme {scheme!r}")
        return s


class LoRaChannel:
    """Carry bytes over the LoRa PHY, implementing the channel contract the runners
    use: transfer(buf) -> (buf_as_received, info).

    In `sim` mode the channel owns TWO radios on one medium — a sender and a
    receiver — and a transfer is the full fragment/ACK exchange between them. That
    keeps the accounting honest: the airtime and retransmission counts you see are
    the ones a real pair of modules would spend.
    """
    name = "lora"

    def __init__(self, backend="sim", sf=9, cr=5, bw_hz=125000, power_dbm=14,
                 freq_hz=915_000_000, snr_db=0.0, seed=0, max_attempts=8,
                 port=None, freq_mhz=None, verbose=False, arq="stop-and-wait"):
        self.backend, self.verbose = backend, verbose
        # framing.py implements stop-and-wait only; refuse anything else rather than
        # running a different policy than the experiment asked for.
        self.arq = _check_arq(arq, "lora")
        self.max_attempts = int(max_attempts)
        cfg = dict(sf=sf, cr=cr, bw_hz=bw_hz, power_dbm=power_dbm, freq_hz=freq_hz)
        if backend == "sim":
            self.medium = SimMedium(snr_db=snr_db, seed=seed)
            self.tx = make_radio("sim", medium=self.medium, **cfg)
            self.rx = make_radio("sim", medium=self.medium, **cfg)
        else:
            # A real module: this process owns ONE radio. A single-process transfer
            # has no peer to answer it, so this path belongs to the two-host roles.
            kw = dict(cfg)
            if port:      kw["port"] = port
            if freq_mhz:  kw["freq_mhz"] = freq_mhz
            self.medium = None
            self.tx = make_radio(backend, **kw)
            self.rx = None
        self.msg_id = 0
        self.total = dict(msgs=0, frags=0, retx=0, airtime_ms=0.0, lost=0)

    # ── the seam every runner calls ──
    def transfer(self, buf):
        """Fragment, send each fragment until it decodes at the peer, reassemble.

        One fragment at a time, retransmitted up to max_attempts: on the shared sim
        medium a dropped fragment simply never reaches the receiver's inbox, so the
        resend loop below IS the stop-and-wait ARQ, with every retransmission paid
        for in real airtime."""
        if self.rx is None:
            raise RuntimeError(
                "a single-process transfer needs both ends; with a real radio run the "
                "two nodes as separate processes (--role tx / --role rx). "
                "Use --lora-backend sim for an in-process run.")
        self.msg_id = (self.msg_id + 1) & 0xFF
        frames = framing.fragment(buf, self.msg_id, self.tx.mtu)
        asm = framing.Reassembler()
        airtime, retx, snrs, ok = 0.0, 0, [], True

        for frame in frames:
            for attempt in range(self.max_attempts):
                airtime += self.tx.send(frame)
                if attempt:
                    retx += 1
                got = self.rx.recv(timeout=0.0)
                if got is None:
                    continue                       # did not decode — send it again
                p = framing.parse(got[0])
                if p is None or p["kind"] != "data":
                    continue
                asm.add(p["msg_id"], p["idx"], p["n_frags"], p["payload"])
                snrs.append(got[1])
                break
            else:
                ok = False                         # this fragment never got through
                break

        data = asm.complete() if ok else None
        crc_ok = data is not None and len(data) >= len(buf)
        self.total["msgs"] += 1
        self.total["frags"] += len(frames)
        self.total["retx"] += retx
        self.total["airtime_ms"] += airtime
        if not crc_ok:
            self.total["lost"] += 1
        info = dict(crc_ok=bool(crc_ok), ber=0.0 if crc_ok else 1.0,
                    frags=len(frames), retx=retx, airtime_ms=round(airtime, 1),
                    sf=self.tx.sf,
                    snr_db=(sum(snrs) / len(snrs) if snrs else None))
        if self.verbose:
            print(f"    [lora] {len(buf)}B -> {len(frames)} frags, retx={retx}, "
                  f"airtime={airtime/1000:.2f}s, ok={crc_ok}")
        # On failure NOTHING arrived at the peer — the SX1276 surfaces only CRC-valid
        # packets, so a lost fragment is silence, not corruption. Hand back empty bytes
        # so the runner's unpack fails and the message is never delivered; returning the
        # original payload would quietly deliver a message the radio never carried.
        return (data[:len(buf)] if crc_ok else b""), info

    def stats(self):
        s = dict(self.total)
        s["airtime_s"] = round(s.pop("airtime_ms") / 1000.0, 2)
        s["sf"], s["bw"], s["backend"] = self.tx.sf, self.tx.bw_hz, self.backend
        s["arq"] = self.arq                      # which policy this run actually used
        return s

    def close(self):
        for r in (self.tx, self.rx):
            if r is not None:
                r.close()


class LoRaLink:
    """ONE END of a LoRa link, running as its own process — the tx / rx / relay roles.

    The role machinery belongs to the middleware, not to a PHY, so this offers exactly
    the same step(app) contract as the USRP driver's RadioRoundTrip. Swapping
    --channel usrp for --channel lora keeps every role working.

        ./run.sh --algo fl --channel lora --role server --lora-port /dev/ttyUSB0
        ./run.sh --algo fl --channel lora --role client --lora-port /dev/ttyUSB0 \\
                 --node 1 --steps 20

    One difference from the USRP link is a simplification: a LoRa module is a
    transceiver, so the reply comes back over the RADIO. The USRP rig sends its reply
    over TCP only because the N210 there is receive-only.

    Half duplex: the two ends strictly alternate (send, then listen), which is what
    the request/response shape of the uniform API already does.
    """

    def __init__(self, role, node=0, peer=None, backend="sim", port=None, sf=9, cr=5,
                 bw_hz=125000, power_dbm=14, freq_hz=915_000_000, snr_db=0.0,
                 max_attempts=8, reply_timeout=120.0, medium=None, verbose=False,
                 arq="stop-and-wait"):
        self.arq = _check_arq(arq, "lora")
        sys.path.insert(0, os.path.abspath(_UNION))
        from phy_link import Codec, _safe_unpack          # the middleware's codec
        self._Codec, self._unpack = Codec, _safe_unpack
        self.role, self.verbose = role, verbose
        self.me = int(node)
        # who this node talks to: the other end by default (0 <-> 1)
        self.peer = int(peer) if peer is not None else (1 - self.me if self.me < 2 else 0)
        self.max_attempts, self.reply_timeout = int(max_attempts), float(reply_timeout)
        cfg = dict(sf=sf, cr=cr, bw_hz=bw_hz, power_dbm=power_dbm, freq_hz=freq_hz)
        kw = dict(cfg)
        if backend == "sim":
            kw.update(medium=medium, snr_db=snr_db)
        elif port:
            kw["port"] = port
        self.radio = make_radio(backend, **kw)
        self.msg_id = 0
        print(f"[lora] {role} node {self.me} -> peer {self.peer} | {backend} | "
              f"SF{self.radio.sf} BW{self.radio.bw_hz} CR4/{self.radio.cr}")

    def _send(self, app, arr):
        self.msg_id = (self.msg_id + 1) & 0xFF
        buf = self._Codec.pack(app.spec.check(arr))
        ok, info = framing.send_message(self.radio, buf, msg_id=self.msg_id,
                                        max_attempts=self.max_attempts,
                                        src=self.me, dst=self.peer)
        if self.verbose:
            print(f"    [lora] sent {len(buf)}B in {info['frags']} frags, "
                  f"retx={info['retx']}, airtime={info['airtime_ms']/1000:.1f}s, ok={ok}")
        return ok

    def _recv(self, app, timeout):
        data, info = framing.recv_message(self.radio, timeout=timeout, me=self.me)
        if data is None:
            return None
        return self._unpack(data, app.spec)

    def step(self, app):
        if self.role == "tx":                       # initiator: send, then listen
            x = app.produce()
            if x is None:
                return False
            self._send(app, x)
            reply = self._recv(app, self.reply_timeout)
            if reply is not None:
                app.consume(reply)
            app.on_result(reply is not None)
            return True

        if self.role == "relay":                    # receive, forward, carry the reply back
            msg = self._recv(app, self.reply_timeout)
            if msg is not None:
                app.consume(msg)
            up, self.peer = self.peer, self._downstream()
            y = app.produce()
            if y is not None:
                self._send(app, y)
            back = self._recv(app, self.reply_timeout)
            self.peer = up
            if back is not None:
                app.consume(back)
                out = app.produce()
                if out is not None:
                    self._send(app, out)
            app.on_result(back is not None)
            return True

        # rx / responder: listen, then answer
        msg = self._recv(app, self.reply_timeout)
        if msg is not None:
            app.consume(msg)
        y = app.produce()
        if y is not None:
            self._send(app, y)
        return True

    def _downstream(self):
        """A relay's next hop: the node after it. Two-hop chains are 0 -> 1 -> 2."""
        return self.me + 1

    def close(self):
        self.radio.close()


class LoRaDriver(PhyDriver):
    """The union/driver.py contract, backed by the LoRa PHY.

    transfer()  a message to the peer and its reply — the data-link archetype
                (fl, dl, clip_semcom, echo).
    broadcast() one slotted round on the shared medium — LoRa IS a shared medium, so
                two nodes transmitting in the same slot genuinely collide and neither
                decodes, which is exactly what the random-access algorithms model.
    superpose() is not offered: over-the-air computation needs coherent addition of
                simultaneous transmissions, which an SX1276 packet radio cannot do.
    """
    name = "lora"

    def __init__(self, **kw):
        self.channel = LoRaChannel(**kw)

    def transfer(self, payload):
        return self.channel.transfer(payload)

    def broadcast(self, bursts):
        """0 transmitters -> idle; exactly 1 -> carried and ACKed iff it decodes;
        >= 2 -> collision, nobody decodes."""
        txers = [i for i, b in enumerate(bursts) if b is not None]
        acks = [False] * len(bursts)
        if len(txers) == 1:
            _, info = self.channel.transfer(bursts[txers[0]])
            acks[txers[0]] = bool(info["crc_ok"])
        return acks

    def superpose(self, coded):
        raise NotImplementedError(
            "over-the-air computation needs coherent superposition; an SX1276 packet "
            "radio cannot do it. Use drivers/usrp for the aircomp archetype.")

    def close(self):
        self.channel.close()


def make_channel(**kw):
    """Entry point the middleware uses for --channel lora."""
    return LoRaChannel(**kw)
