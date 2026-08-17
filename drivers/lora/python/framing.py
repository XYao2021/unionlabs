#!/usr/bin/env python3
"""
framing.py — carry a message of ANY size over a 255-byte LoRa PHY.

The uniform API hands the driver whole payloads: an echo burst is 32 bytes, a
federated-learning model vector is ~200 kB. LoRa's MTU is 255 bytes and that is a
property of the PHY, not a tunable. So the driver fragments, numbers, and
reassembles, and asks for a retransmission when a fragment does not arrive.

WIRE FORMAT — 8 bytes of header, then up to 247 bytes of payload

    magic(1)=0x4C  src(1)  dst(1)  msg_id(1)  frag_idx(2, LE)  n_frags(2, LE)  payload

    ACK frame:     magic(1)=0x41  src(1)  dst(1)  msg_id(1)  frag_idx(2, LE)

ADDRESSING. LoRa is a broadcast medium: every node in range hears every frame, so the
header carries who sent it and who it is for, and a node drops what is not addressed to
it. dst=0xFF (BROADCAST) is for everyone. Without this a third node in the network would
absorb an exchange between two others.

Stop-and-wait: send fragment k, wait for its ACK, then send k+1. Slow but correct
on a half-duplex radio, and it is what the SX1276 firmware's one-packet-at-a-time
model supports. The cost is explicit in the returned info: `frags`, `retx` and
`airtime_ms` are real, so an experiment can SEE what a 200 kB model costs at SF12.

WHY THIS MATTERS FOR THE UNIFORM API
    Every algorithm in experiments/ already runs over the USRP PHY, whose driver
    chunks at 125 bytes with its own stop-and-wait ARQ. This is the same idea for
    LoRa, so the algorithms need no knowledge of either.
"""
import struct

HDR_MAGIC = 0x4C          # 'L' — a data fragment
ACK_MAGIC = 0x41          # 'A' — an acknowledgement
BROADCAST = 0xFF          # dst that every node accepts
HDR = struct.Struct("<BBBBHH")
ACK = struct.Struct("<BBBBH")
HDR_LEN = HDR.size        # 8
ACK_LEN = ACK.size        # 6


def payload_per_frag(mtu=255):
    return mtu - HDR_LEN


def fragment(data, msg_id=0, mtu=255, src=0, dst=BROADCAST):
    """Split `data` into wire frames. Always at least one frame, so a zero-length
    message still crosses the link."""
    cap = payload_per_frag(mtu)
    chunks = [data[i:i + cap] for i in range(0, len(data), cap)] or [b""]
    n = len(chunks)
    if n > 0xFFFF:
        raise ValueError(f"{len(data)} B needs {n} fragments, over the 65535 limit "
                         f"— raise the MTU or compress the payload")
    return [HDR.pack(HDR_MAGIC, src & 0xFF, dst & 0xFF, msg_id & 0xFF, i, n) + c
            for i, c in enumerate(chunks)]


def parse(frame):
    """-> dict(kind, src, dst, msg_id, idx, n_frags, payload), or None when the bytes
    are not ours — another experiment sharing the band, or a corrupt header."""
    if len(frame) >= ACK_LEN and frame[0] == ACK_MAGIC:
        _, src, dst, mid, idx = ACK.unpack(frame[:ACK_LEN])
        return dict(kind="ack", src=src, dst=dst, msg_id=mid, idx=idx,
                    n_frags=None, payload=None)
    if len(frame) >= HDR_LEN and frame[0] == HDR_MAGIC:
        _, src, dst, mid, idx, n = HDR.unpack(frame[:HDR_LEN])
        return dict(kind="data", src=src, dst=dst, msg_id=mid, idx=idx,
                    n_frags=n, payload=frame[HDR_LEN:])
    return None


def for_me(p, me):
    """Is this parsed frame addressed to this node? me=None accepts everything (a
    two-node link has nobody else to confuse it with)."""
    if p is None or me is None:
        return p is not None
    return p["dst"] == (me & 0xFF) or p["dst"] == BROADCAST


def ack_frame(msg_id, idx, src=0, dst=BROADCAST):
    return ACK.pack(ACK_MAGIC, src & 0xFF, dst & 0xFF, msg_id & 0xFF, idx)


class Reassembler:
    """Collects fragments of one message until every index has arrived."""

    def __init__(self):
        self.msg_id = None
        self.parts = {}
        self.n_frags = None

    def add(self, msg_id, idx, n_frags, payload):
        if self.msg_id != msg_id:                 # a new message supersedes the old
            self.msg_id, self.parts, self.n_frags = msg_id, {}, n_frags
        self.parts[idx] = payload
        self.n_frags = n_frags
        return self.complete()

    def complete(self):
        if self.n_frags is None or len(self.parts) < self.n_frags:
            return None
        return b"".join(self.parts[i] for i in range(self.n_frags))

    def missing(self):
        if self.n_frags is None:
            return []
        return [i for i in range(self.n_frags) if i not in self.parts]


def send_message(radio, data, msg_id=0, ack_timeout=None, max_attempts=8, wait_ack=True,
                 src=0, dst=BROADCAST):
    """Fragment `data` and push it out with stop-and-wait ARQ.

    Returns (delivered, info). `delivered` is False when some fragment used up its
    attempts — the caller decides whether that is a lost round or a hard error.

    ack_timeout defaults to 3x the airtime of a full frame plus 200 ms, which scales
    correctly across SF7..SF12 instead of being a magic constant."""
    frames = fragment(data, msg_id, radio.mtu, src=src, dst=dst)
    if ack_timeout is None:
        ack_timeout = 3.0 * radio.toa_ms(radio.mtu) / 1000.0 + 0.2
    info = dict(frags=len(frames), retx=0, airtime_ms=0.0, bytes=len(data))
    for idx, frame in enumerate(frames):
        for attempt in range(max_attempts):
            info["airtime_ms"] += radio.send(frame)
            if not wait_ack:
                break
            if _await_ack(radio, msg_id, idx, ack_timeout, me=src):
                break
            info["retx"] += 1
        else:
            info["lost_frag"] = idx
            return False, info
    return True, info


def _await_ack(radio, msg_id, idx, timeout, me=None):
    import time
    deadline = time.monotonic() + timeout
    while True:
        got = radio.recv(timeout=max(0.01, deadline - time.monotonic()))
        if got is not None:
            p = parse(got[0])
            if (p and p["kind"] == "ack" and for_me(p, me)
                    and p["msg_id"] == (msg_id & 0xFF) and p["idx"] == idx):
                return True
        if time.monotonic() >= deadline:
            return False


def recv_message(radio, timeout=30.0, send_acks=True, me=None):
    """Reassemble one whole message addressed to `me`. Frames for other nodes are
    ignored — on a broadcast medium this node hears exchanges it is not part of.
    Returns (data, info), or (None, info) on timeout."""
    import time
    asm = Reassembler()
    info = dict(frags=0, snr=[], rssi=[], airtime_ms=0.0, src=None)
    deadline = time.monotonic() + timeout
    while True:
        got = radio.recv(timeout=max(0.0, deadline - time.monotonic()))
        if got is None:
            if time.monotonic() >= deadline:
                break
            continue
        frame, snr, rssi = got
        p = parse(frame)
        if p is None or p["kind"] != "data" or not for_me(p, me):
            continue                       # not ours: another pair, or another experiment
        info["frags"] += 1
        info["src"] = p["src"]
        info["snr"].append(snr)
        info["rssi"].append(rssi)
        if send_acks:
            info["airtime_ms"] += radio.send(
                ack_frame(p["msg_id"], p["idx"], src=(me or 0), dst=p["src"]))
        done = asm.add(p["msg_id"], p["idx"], p["n_frags"], p["payload"])
        if done is not None:
            info["snr_mean"] = sum(info["snr"]) / max(1, len(info["snr"]))
            info["missing"] = []
            return done, info
        if time.monotonic() >= deadline:
            break
    info["missing"] = asm.missing()
    return None, info
