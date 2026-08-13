#!/usr/bin/env python3
"""
phy_link.py — the uniform algorithm <-> PHY abstraction layer.

A user drops an algorithm into  algorithms/<name>/app.py  as a subclass of SdrApp.
The framework grabs the algorithm's OUTPUT at the transmitter, sends it over the
PHY, and hands the RECEIVED message back to the algorithm's INPUT — a synchronous
request/response round-trip. The algorithm never touches the radio.

THE CONTRACT (what an uploaded algorithm implements)
----------------------------------------------------
    class App(SdrApp):
        spec = PayloadSpec("float32", (N,))     # declare output shape + type
        def produce(self) -> np.ndarray | None  # the thing to transmit (None = done)
        def consume(self, msg: np.ndarray)      # the received array, fed back in
        def on_result(self, ack: bool)          # optional: delivered/collision (RL reward)

    self.role is "tx" or "rx" (set by the runner) — branch on it if
    the two ends behave differently (e.g. FL client vs server).

ROUND-TRIP (one step)
    tx:  x = produce() -> [PHY] -> rx.consume(x)
                                          y = rx.produce()
                consume(y) <- [PHY] <-    -----------------------
                on_result(ack)

TRANSPORT BACKENDS
    ideal  : lossless, in-process           (logic check)
    pyphy  : real modem + AWGN, in-process  (radio-free; corrupts payloads)
    radio  : the USRP link (source_arq/sink) + TCP reply   (two hosts)

This generalises the CLIP app's phy_port.py so it is not app-specific.
"""
import os, sys, struct
import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  The contract
# ══════════════════════════════════════════════════════════════════════════════
class PayloadSpec:
    """An algorithm DECLARES its output type + shape. shape=None => any length."""
    def __init__(self, dtype="float32", shape=None):
        self.dtype, self.shape = dtype, (tuple(shape) if shape is not None else None)

    def check(self, arr):
        a = np.asarray(arr)
        if str(a.dtype) != self.dtype:
            a = a.astype(self.dtype)
        if self.shape is not None and a.shape != self.shape:
            raise ValueError(f"payload shape {a.shape} != declared {self.shape}")
        return a

    def __repr__(self):
        return f"PayloadSpec(dtype={self.dtype!r}, shape={self.shape})"


class SdrApp:
    """Base class every uploaded algorithm subclasses."""
    spec = PayloadSpec("float32", None)

    def __init__(self, role="tx", **kw):
        self.role = role

    def produce(self):                      # -> np.ndarray | None
        raise NotImplementedError("your App must implement produce()")

    def consume(self, msg):                 # msg: np.ndarray
        pass

    def on_result(self, ack):               # optional (control/RL apps)
        pass


def adapt(obj, role="tx"):
    """Wrap ANY object into an SdrApp — the object needs NO knowledge of this
    framework. It only has to expose 'what to transmit' and 'what to receive':

        transmit() / produce()   -> np.ndarray | None   (the output to send)
        receive(msg) / consume(msg)                      (the received input)
        spec        (optional)   PayloadSpec or (dtype, shape) tuple
        on_result(ack) (optional)

    So a user drops a plain algorithm and a one-line make(role) binding; the PHY,
    codec, round-trip, and radio are all handled here."""
    if isinstance(obj, SdrApp):
        obj.role = getattr(obj, "role", role)
        return obj
    a = SdrApp(role)
    s = getattr(obj, "spec", None)
    if isinstance(s, PayloadSpec):
        a.spec = s
    elif isinstance(s, (tuple, list)):
        a.spec = PayloadSpec(*s)
    else:
        a.spec = PayloadSpec("float32", None)
    tx = getattr(obj, "transmit", None) or getattr(obj, "produce", None)
    if tx is None:
        raise TypeError("algorithm must expose transmit() (what to transmit)")
    rx = getattr(obj, "receive", None) or getattr(obj, "consume", None) or (lambda m: None)
    res = getattr(obj, "on_result", None) or (lambda ok: None)
    a.role = getattr(obj, "role", role)
    a.produce = lambda: tx()
    a.consume = lambda msg: rx(msg)
    a.on_result = lambda ok: res(ok)
    a._src = obj                                # keep a handle to the raw algorithm object
    return a


# ══════════════════════════════════════════════════════════════════════════════
#  Codec — any numpy array <-> self-describing bytes (dtype + shape header)
# ══════════════════════════════════════════════════════════════════════════════
_MAGIC = b"SDRA"
_DT = {"float32": 0, "float64": 1, "int32": 2, "int16": 3,
       "int8": 4, "uint8": 5, "float16": 6}
_DT_INV = {v: k for k, v in _DT.items()}


class Codec:
    @staticmethod
    def pack(arr):
        a = np.ascontiguousarray(arr)
        if str(a.dtype) not in _DT:
            a = a.astype(np.float32)
        hdr = _MAGIC + struct.pack("<BBB", 1, _DT[str(a.dtype)], a.ndim)
        hdr += b"".join(struct.pack("<I", d) for d in a.shape)
        return hdr + a.tobytes()

    @staticmethod
    def unpack(buf):
        if buf[:4] != _MAGIC:
            raise ValueError("bad SDRA frame magic")
        _, code, ndim = struct.unpack_from("<BBB", buf, 4)
        off = 7
        shape = struct.unpack_from("<" + "I" * ndim, buf, off)
        off += 4 * ndim
        dt = np.dtype(_DT_INV[code])
        n = int(np.prod(shape)) if ndim else 1
        arr = np.frombuffer(buf, dt, count=n, offset=off).reshape(shape).copy()
        return arr


# ══════════════════════════════════════════════════════════════════════════════
#  Radio-free channels (bytes -> bytes) for loopback testing
# ══════════════════════════════════════════════════════════════════════════════
class IdealChannel:
    name = "ideal"
    def transfer(self, buf):
        return buf, dict(ber=0.0, crc_ok=True)


class PyphyChannel:
    """Push payload BYTES through the repo's REAL modem (pyphy) with AWGN at a
    target Es/N0. No CRC gate: residual bit errors corrupt the payload — a faithful
    radio-free stand-in for the wireless channel. (Generalised from phy_port.py.)"""
    name = "pyphy"

    def __init__(self, scheme="QPSK", fec="turbo", k=256, snr_db=8.0, soft=True, seed=0):
        import pyphy
        self.p = pyphy
        self.scheme, self.fec, self.k = scheme, (fec or None), k
        self.snr_db, self.soft = snr_db, soft
        self.rng = np.random.RandomState(seed)
        self.bps = {"BPSK": 1, "QPSK": 2, "8-PSK": 3, "16-QAM": 4,
                    "DBPSK": 1, "DQPSK": 2}.get(scheme, 2)

    def transfer(self, buf):
        p = self.p
        bits = np.unpackbits(np.frombuffer(buf, np.uint8)); nbits = bits.size
        tx = p.fec_encode(bits.astype(np.uint8), self.fec, self.k) if self.fec else bits.astype(np.uint8)
        pad = (-tx.size) % self.bps
        if pad:
            tx = np.concatenate([tx, np.zeros(pad, np.uint8)])
        syms = p.modulate(tx, self.scheme).astype(np.complex64)
        es = float(np.mean(np.abs(syms) ** 2)) or 1.0
        sigma = np.sqrt(es / (2.0 * 10 ** (self.snr_db / 10.0)))
        rx = (syms + sigma * (self.rng.randn(syms.size) + 1j * self.rng.randn(syms.size))).astype(np.complex64)
        if self.fec and self.soft:
            llr = p.soft_llr(rx, self.scheme, float(2 * sigma ** 2))
            rbits = p.fec_decode_soft(llr, self.fec, self.k, info_len=nbits)
        else:
            hard = p.demodulate(rx, self.scheme)
            rbits = p.fec_decode(hard.astype(np.uint8), self.fec, self.k, info_len=nbits) if self.fec else hard[:nbits]
        rbits = np.asarray(rbits, np.uint8)[:nbits]
        if rbits.size < nbits:
            rbits = np.concatenate([rbits, np.zeros(nbits - rbits.size, np.uint8)])
        ber = float(np.mean(rbits != bits))
        return np.packbits(rbits).tobytes()[:len(buf)], dict(ber=ber, crc_ok=(ber == 0), snr_db=self.snr_db)


def make_channel(backend="ideal", **kw):
    if backend == "ideal":
        return IdealChannel()
    if backend == "pyphy":
        return PyphyChannel(**kw)
    raise ValueError(f"radio-free backend must be ideal|pyphy (got {backend!r})")


# ══════════════════════════════════════════════════════════════════════════════
#  Round-trip drivers
# ══════════════════════════════════════════════════════════════════════════════
def _safe_unpack(buf, spec):
    """unpack + sanity; returns (array or None). None => the frame was destroyed."""
    try:
        a = Codec.unpack(buf)
        if not np.all(np.isfinite(a.astype(np.float64))) or np.max(np.abs(a.astype(np.float64))) > 1e30:
            return None
        return a
    except Exception:
        return None


def run_loopback(tx, rx, channel, steps=10, verbose=True):
    """Radio-free synchronous request/response: both apps in one process, the
    channel between them. One step = tx.produce -> rx.consume ->
    rx.produce (reply) -> tx.consume, plus on_result(delivered)."""
    stats = dict(steps=0, delivered=0, req_ber=[], rep_ber=[])
    for t in range(steps):
        x = tx.produce()
        if x is None:
            break
        stats["steps"] += 1
        # ╔═══════════ PORT TO THE PHY (request leg) ═══════════╗
        # ║ the produced burst is carried by the channel = the  ║
        # ║ modem/radio; i1["crc_ok"] = it decoded at the peer. ║
        req, i1 = channel.transfer(Codec.pack(tx.spec.check(x)))
        # ╚═════════════════════════════════════════════════════╝
        stats["req_ber"].append(i1["ber"])
        msg = _safe_unpack(req, rx.spec)
        if msg is None:                                  # request destroyed
            tx.on_result(False); continue
        rx.consume(msg)
        y = rx.produce()
        if y is None:                                    # rx has no reply (one-way / control app)
            ok = i1["crc_ok"]; stats["delivered"] += int(ok); tx.on_result(ok); continue
        rep, i2 = channel.transfer(Codec.pack(rx.spec.check(y)))     # PORT TO THE PHY (reply leg)
        stats["rep_ber"].append(i2["ber"])
        reply = _safe_unpack(rep, tx.spec)
        ack = i1["crc_ok"] and i2["crc_ok"] and reply is not None
        if reply is not None:
            tx.consume(reply)
        tx.on_result(ack)
        stats["delivered"] += int(ack)
        if verbose:
            print(f"  step {t}: req_ber={i1['ber']:.4f} rep_ber={i2['ber']:.4f} "
                  f"delivered={ack}")
    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  Multi-node extension: N agents contend for ONE access point (slotted multi-access)
# ══════════════════════════════════════════════════════════════════════════════
def run_slotted(agents, channel, slots, verbose=True, record_every=0):
    """MULTI-AGENT random access. N agents share one slotted medium + one AP. Each slot every
    agent's produce() returns a burst (transmit) or None (defer). Resolution at the AP:
        0 transmitters -> idle;
        exactly 1      -> that burst is carried by the PHY and ACKed iff it decodes;
        >= 2           -> COLLISION (nobody decodes, no ACK).
    Each agent learns from its own on_result(ack) — it cannot see the collision directly, only
    its own no-ACK, exactly like the real decentralised AP (`ap_multi.py`).

    record_every > 0 -> also return st["traj"], a per-checkpoint trajectory
    (slot, cumulative delivered/collisions/idle, per-agent p_transmit) for plotting."""
    n = len(agents)
    st = dict(slots=0, delivered=0, collisions=0, idle=0)
    traj = []
    for t in range(slots):
        payloads = [a.produce() for a in agents]                 # each agent decides
        txers = [i for i, p in enumerate(payloads) if p is not None]
        acks = [False] * n
        if len(txers) == 0:
            st["idle"] += 1
        elif len(txers) == 1:
            i = txers[0]
            # ╔══════════════════════ PORT TO THE PHY ══════════════════════╗
            # ║ the single winning burst is carried by the modem / radio;   ║
            # ║ crc_ok = it decoded at the access point. (>=2 collide above; ║
            # ║ swap `channel` for the real radio to run it over USRPs.)    ║
            out, info = channel.transfer(Codec.pack(agents[i].spec.check(payloads[i])))
            # ╚═════════════════════════════════════════════════════════════╝
            acks[i] = info["crc_ok"]
            st["delivered"] += int(acks[i])
        else:
            st["collisions"] += 1                                # >=2 overlap -> nothing decodes
        for i, a in enumerate(agents):
            a.on_result(acks[i])                                 # each agent's own reward signal
        st["slots"] += 1
        if record_every and (t + 1) % record_every == 0:
            ptx = [(getattr(a._src, "p_transmit", None) or (lambda: float("nan")))()
                   for a in agents]
            traj.append(dict(slot=t + 1, delivered=st["delivered"],
                             collisions=st["collisions"], idle=st["idle"], p_tx=ptx))
        if verbose and (t + 1) % max(1, slots // 8) == 0:
            print(f"  slot {t+1}/{slots}: throughput={st['delivered']/(t+1):.2f} "
                  f"collision-rate={st['collisions']/(t+1):.2f} idle={st['idle']}")
    if record_every:
        st["traj"] = traj
    return st


# ── two-host radio round-trip: request over the USRP link, reply over TCP ──────
#   (mirrors fl.py's proven --uplink wireless --downlink tcp; needs UHD + two hosts)
class RadioRoundTrip:
    """Initiator: send request over source_arq (wireless), receive reply over TCP.
    Responder: receive request over sink_arq (wireless), send reply over TCP.
    Wireless is the B210->N210 uplink; the reply goes over TCP so the RX-only N210
    never has to transmit — the exact split fl.py uses."""

    def __init__(self, role, tx_args="", rx_args="", ack_host="127.0.0.1", ack_port=5599,
                 net_host="127.0.0.1", net_port=5700, scheme="DQPSK", waveform="sc",
                 tx_gain=70, rx_gain=30, rx_subdev="A:0", tx_subdev="A:A", chunk=125):
        # sdr.py lives in the USRP driver (union/ -> repo -> drivers/usrp_uhd/python)
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "drivers", "usrp_uhd", "python"))
        import sdr, socket, struct as _st
        self.sdr, self.socket, self._st = sdr, socket, _st
        self.role = role
        self.net_host, self.net_port = net_host, net_port
        self.cfg = dict(scheme=scheme, waveform=waveform, fec=True, rx_freq=915e6, tx_freq=915e6,
                        tx_rate=2e6, rx_rate=2e6, symbol_rate=1e6, rx_ant="RX2", tx_ant="TX/RX",
                        rx_subdev=rx_subdev, tx_subdev=tx_subdev, det_mult=3,
                        ack_transport="tcp", ack_port=ack_port, bytes_length=chunk, viz=False)
        self.tx_args, self.rx_args = tx_args, rx_args
        self.ack_host, self.tx_gain, self.rx_gain = ack_host, tx_gain, rx_gain
        import tempfile
        self.tmp = tempfile.gettempdir()

    def _wl_send(self, buf):                    # wireless uplink (source_arq)
        path = os.path.join(self.tmp, "phylink_tx.bin"); open(path, "wb").write(buf)
        self.sdr.source_arq(tx_args=self.tx_args, rx_args=self.tx_args, tx_gain=self.tx_gain,
                            ack_host=self.ack_host, max_attempts=50, payload_file=path, **self.cfg).run()

    def _wl_recv(self):                         # wireless uplink (sink_arq)
        path = os.path.join(self.tmp, "phylink_rx.bin")
        if os.path.exists(path): os.remove(path)
        self.sdr.sink_arq(rx_args=self.rx_args, tx_args=self.rx_args, rx_gain=self.rx_gain,
                          out_file=path, **self.cfg).run()
        return open(path, "rb").read()

    def _tcp_frame(self, sock, buf):
        sock.sendall(self._st.pack(">I", len(buf)) + buf)

    def _tcp_read(self, sock):
        n = self._st.unpack(">I", self._recvn(sock, 4))[0]; return self._recvn(sock, n)

    def _recvn(self, sock, n):
        b = b""
        while len(b) < n:
            c = sock.recv(n - len(b));  b += c
            if not c: raise ConnectionError("peer closed")
        return b

    # request/response per step. `app` supplies produce/consume; role decides order.
    def step(self, app):
        if self.role == "tx":                    # client: send req, TCP-recv reply
            x = app.produce()
            if x is None: return False
            self._wl_send(Codec.pack(app.spec.check(x)))
            import time
            s = None                                    # rx binds :net_port only AFTER
            for _ in range(50):                         # it decodes our request -> retry the connect
                try:
                    s = self.socket.create_connection((self.net_host, self.net_port), timeout=2.0)
                    break
                except OSError:
                    time.sleep(0.2)
            if s is None:
                raise ConnectionError(f"reply server {self.net_host}:{self.net_port} never came up")
            try:
                app.consume(_safe_unpack(self._tcp_read(s), app.spec)); app.on_result(True)
            finally:
                s.close()
            return True
        else:                                           # server: wl-recv req, TCP-send reply
            msg = _safe_unpack(self._wl_recv(), app.spec)
            if msg is not None: app.consume(msg)
            y = app.produce()
            srv = self.socket.socket(); srv.setsockopt(self.socket.SOL_SOCKET, self.socket.SO_REUSEADDR, 1)
            srv.bind((self.net_host, self.net_port)); srv.listen(1)
            conn, _ = srv.accept()
            try:
                self._tcp_frame(conn, Codec.pack(app.spec.check(y if y is not None else np.zeros(1, np.float32))))
            finally:
                conn.close(); srv.close()
            return True
