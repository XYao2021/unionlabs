#!/usr/bin/env python3
"""
phy_link.py — the uniform algorithm <-> PHY abstraction layer.

A user drops an algorithm into  experiments/<name>/app.py  as a subclass of SdrApp.
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
#  ARQ schemes — the retransmission policy, named explicitly
# ══════════════════════════════════════════════════════════════════════════════
# Exactly one scheme exists today: stop-and-wait, which is what the C++ PHY implements
# (drivers/usrp/include/ACQ_stop_and_wait.hpp) and what the LoRa driver's framing.py
# does. It is still a NAMED CHOICE rather than an assumption, for two reasons: a run
# records which policy it used, and adding go-back-N or selective-repeat later is an
# entry here plus its implementation, not an edit to every call site.
#
# To add one:
#   1. add its name below;
#   2. implement it in the C++ PHY (a sibling of ACQ_stop_and_wait.hpp) and/or in
#      drivers/lora/python/framing.py;
#   3. a PHY that does not implement it should say so rather than silently
#      falling back — see _check_arq().
ARQ_SCHEMES = ("stop-and-wait",)


def check_arq(scheme, phy, supported=ARQ_SCHEMES):
    """Fail loudly when a PHY is asked for an ARQ policy it does not implement."""
    s = (scheme or "stop-and-wait").lower()
    if s not in supported:
        raise ValueError(
            f"the {phy} PHY does not implement ARQ scheme {scheme!r}; it supports "
            f"{', '.join(supported)}. Implement it first (see ARQ_SCHEMES in "
            f"union/phy_link.py) rather than falling back silently.")
    return s


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
        try:
            import pyphy
        except ImportError:
            # The commonest first-run failure, and the raw ImportError explains nothing.
            # pyphy is a compiled extension built for ONE Python version and platform, so
            # a prebuilt .so in the repo will not match most machines.
            import glob
            built = [os.path.basename(p) for p in
                     glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "drivers", "usrp", "bindings", "*.so"))]
            raise SystemExit(
                "--channel usrp needs the 'pyphy' extension, which is not importable.\n"
                f"  Your Python:   {sys.version.split()[0]} on {sys.platform}\n"
                f"  Built here:    {', '.join(built) if built else '(none)'}\n"
                "\n"
                "pyphy is COMPILED, so it only loads in the Python version and platform it\n"
                "was built for. Build it for yours:\n"
                "    drivers/usrp/bindings/build.sh\n"
                "\n"
                "Or skip it entirely — these need no build and no hardware:\n"
                "    ./run.sh --algo <name>                  # --channel ideal, lossless\n"
                "    ./run.sh --algo <name> --channel lora   # the LoRa PHY\n"
                "Check what does work on this machine with:  ./run.sh selftest")
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


def make_channel(kind="ideal", **kw):
    """Pick the PHY the runners carry bytes over. Every backend implements the same
    one-line contract, transfer(buf) -> (bytes_at_the_peer, info), which is why the
    algorithms never learn which radio they are running on.

        ideal  lossless, in-process                       drivers/sim
        pyphy  the repo's real C++ modem + AWGN           drivers/usrp
        lora   the LoRa PHY (SX1276): 255-byte MTU,       drivers/lora
               fragmentation + ARQ, real Semtech airtime
    """
    if kind == "ideal":
        return IdealChannel()
    if kind == "pyphy":
        return PyphyChannel(**kw)
    if kind == "lora":
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "drivers", "lora", "python"))
        import lora_driver
        return lora_driver.LoRaChannel(**kw)
    raise ValueError(f"channel must be ideal|pyphy|lora (got {kind!r})")


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
#  Multi-hop extension: a RELAY is a node that receives AND transmits
# ══════════════════════════════════════════════════════════════════════════════
def run_chain(nodes, channel, steps=10, verbose=True):
    """Radio-free MULTI-HOP round-trip over  nodes = [initiator, relay, ..., responder].

    Every hop is carried by the PHY, so an R-relay chain costs 2*(R+1) transmissions
    per round-trip. A relay is consumed-then-produced on the way OUT and again on the
    way BACK, which is exactly what "receives and transmits" means here: a pass-through
    relay just re-emits whatever it last received, while a relay that returns something
    else from produce() is processing in the middle of the link (re-encoding, partial
    aggregation, compression).

        initiator.produce -> [hop] -> relay -> [hop] -> responder.consume
                                                        responder.produce
        initiator.consume <- [hop] <- relay <- [hop] <-
    """
    ini, relays, res = nodes[0], list(nodes[1:-1]), nodes[-1]
    st = dict(steps=0, delivered=0, hops=0, ber=[], relays=len(relays))

    def hop(payload, src, dst):
        """Carry one payload over the PHY from src to dst -> (msg or None, crc_ok)."""
        out, info = channel.transfer(Codec.pack(src.spec.check(payload)))
        st["ber"].append(info["ber"])
        st["hops"] += 1
        return _safe_unpack(out, dst.spec), info["crc_ok"]

    def leg(payload, src, hops_to):
        """Walk payload through a list of nodes, each consuming then producing."""
        ok = True
        for dst in hops_to:
            msg, crc = hop(payload, src, dst)
            ok = ok and crc
            if msg is None:
                return None, src, False
            dst.consume(msg)
            payload, src = dst.produce(), dst
            if payload is None:
                break
        return payload, src, ok

    for t in range(steps):
        x = ini.produce()
        if x is None:
            break
        st["steps"] += 1
        # ── forward leg: initiator -> relays -> responder ──
        payload, src, ok = leg(x, ini, relays + [res])
        if payload is None:                      # nothing coming back (one-way app, or lost)
            st["delivered"] += int(ok)
            ini.on_result(ok)
            continue
        # ── return leg: responder -> relays -> initiator ──
        payload, src, ok2 = leg(payload, src, relays[::-1])
        ok = ok and ok2
        if payload is not None and ok:
            reply, crc = hop(payload, src, ini)
            ok = ok and crc and reply is not None
            if reply is not None:
                ini.consume(reply)
        else:
            ok = False
        ini.on_result(ok)
        st["delivered"] += int(ok)
        if verbose:
            print(f"  step {t}: {2 * (len(relays) + 1)} hops delivered={ok}")
    return st


# ══════════════════════════════════════════════════════════════════════════════
#  Peer-to-peer extension: N nodes gossip over a graph — NO access point, NO server
# ══════════════════════════════════════════════════════════════════════════════
def gossip_edges(n, topology="ring"):
    """Undirected edges of the peer graph — THE EXPERIMENTER'S CHOICE. One edge == one
    symmetric exchange (each end sends its own payload to the other), so an edge costs
    2 PHY transfers.

    Three settings. The two standard graphs are fixed and need no description, and any
    other graph is given as an explicit edge list:

        ring (default)  0-1-2-...-0     each peer talks to 2 neighbours, n edges
        full            every pair      fastest consensus, most traffic, n(n-1)/2 edges
        custom          "0-1,1-2,2-0"   an explicit edge list — any graph at all, e.g.
                                        a line  0-1,1-2,2-3  or a star  0-1,0-2,0-3
    """
    t = str(topology or "ring").strip().lower()
    if n < 2:
        return []
    if t == "full":
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    if t == "ring":
        return [(0, 1)] if n == 2 else [(i, (i + 1) % n) for i in range(n)]
    # an explicit edge list: "0-1,1-2,2-0"  (also accepts ':' or whitespace as separators)
    if any(c.isdigit() for c in t):
        edges, seen = [], set()
        for part in t.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.replace(":", "-").split("-")
            if len(bits) != 2:
                raise ValueError(f"bad edge {part!r} in --topology (want e.g. 0-1,1-2)")
            try:
                i, j = int(bits[0]), int(bits[1])
            except ValueError:
                raise ValueError(f"bad edge {part!r} in --topology (want e.g. 0-1,1-2)")
            if not (0 <= i < n and 0 <= j < n):
                raise ValueError(f"edge {part!r} refers to a node outside 0..{n-1}")
            if i == j:
                raise ValueError(f"edge {part!r} is a self-loop")
            key = (min(i, j), max(i, j))
            if key not in seen:                       # ignore a repeated edge
                seen.add(key); edges.append(key)
        if not edges:
            raise ValueError("--topology edge list is empty")
        return edges
    raise ValueError(f"unknown topology {topology!r} — use ring, full, or an explicit "
                     f"edge list like 0-1,1-2,2-0")


def run_gossip(nodes, channel, rounds=10, topology="ring", verbose=True):
    """DECENTRALISED round: N peers, no server and no access point. Every round each
    node produces once (its current payload) and that payload is carried over the PHY
    to each of its graph neighbours; what a node receives is mixed in by the algorithm
    itself on its next produce().

    This is the transfer archetype applied edge-by-edge over a graph — no new PHY verb
    is needed, so the same algorithm runs over the radio by pairing up the nodes.

    Neighbours only: a node never sees the whole network, which is the entire point of
    the decentralised setting (consensus has to emerge from local exchanges)."""
    n = len(nodes)
    edges = gossip_edges(n, topology)
    st = dict(rounds=0, exchanges=0, delivered=0, lost=0, hops=0, ber=[],
              topology=topology, nodes=n, edges=len(edges))
    for t in range(rounds):
        payloads = [nd.produce() for nd in nodes]        # each peer mixes, trains, emits
        if any(p is None for p in payloads):
            break
        st["rounds"] += 1
        for (i, j) in edges:
            for a, b in ((i, j), (j, i)):                # symmetric: both directions
                # ╔══════════════════ PORT TO THE PHY ══════════════════╗
                out, info = channel.transfer(Codec.pack(nodes[a].spec.check(payloads[a])))
                # ╚═════════════════════════════════════════════════════╝
                st["ber"].append(info["ber"]); st["hops"] += 1
                msg = _safe_unpack(out, nodes[b].spec)
                ok = bool(info["crc_ok"]) and msg is not None
                st["delivered"] += int(ok); st["lost"] += int(not ok)
                if msg is not None:
                    nodes[b].consume(msg)
                nodes[a].on_result(ok)
            st["exchanges"] += 1
        if verbose:
            print(f"  round {t+1}/{rounds}: {len(edges)} exchanges "
                  f"({2*len(edges)} PHY hops), lost={st['lost']}")
    return st


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


# ══════════════════════════════════════════════════════════════════════════════
#  One decentralised node as its OWN PROCESS (its own terminal, or its own computer)
# ══════════════════════════════════════════════════════════════════════════════
class PeerLink:
    """A node of a decentralised network that is BOTH TX AND RX — at different steps.

    NO COORDINATOR. Every node derives the same edge list from (n_nodes, topology), so
    all of them walk the same schedule and the two ends of each edge already agree on
    who transmits first: the node named first in the edge sends, the other receives,
    then they swap. Nodes not on the current edge sit that exchange out. One pass over
    the edge list = one round; the node produces once per round and sends that same
    payload to each of its neighbours.

        node 0 ──── node 1          ./run.sh --algo dl --node 0 --agents 3   (terminal 1)
           \\        /               ./run.sh --algo dl --node 1 --agents 3   (terminal 2)
            node 2                   ./run.sh --algo dl --node 2 --agents 3   (terminal 3)

    link="tcp"       peers talk over TCP/IP — same machine (different terminals) or
                     different computers on a LAN. Node k listens on base_port + k.
    link="wireless"  each exchange is carried by the USRP radio (source_arq / sink_arq),
                     the same ARQ byte-pipe the two-host round-trip uses.
    link="lora"      each exchange is carried by the LoRa radio (drivers/lora),
                     addressed node-to-node — LoRa is a broadcast medium, so every peer
                     hears every exchange and the frame header says who it is for.
    """

    def __init__(self, node_id, n_nodes, topology="ring", peers=None, base_port=5800,
                 link="tcp", connect_timeout=120.0, tx_args="", rx_args="", scheme="DQPSK",
                 waveform="sc", tx_gain=70, rx_gain=30, rx_subdev="A:0", tx_subdev="A:A",
                 rx_ant="RX2", tx_ant="TX/RX", ack_port=5599, chunk=125,
                 lora_backend="sim", lora_port=None, lora_sf=9, lora_cr=5,
                 lora_bw=125000, lora_power=14, lora_snr_db=0.0, lora_medium=None,
                 lora_timeout=120.0):
        import socket, struct as _st
        self.socket, self._st = socket, _st
        self.id, self.n = int(node_id), int(n_nodes)
        if not (0 <= self.id < self.n):
            raise ValueError(f"--node {self.id} is outside 0..{self.n - 1} "
                             f"(use --agents to say how many nodes there are)")
        self.edges = gossip_edges(self.n, topology)
        self.neighbours = sorted({(b if a == self.id else a)
                                  for (a, b) in self.edges if self.id in (a, b)})
        self.hosts = list(peers) if peers else ["127.0.0.1"] * self.n
        if peers is None and link == "wireless":
            # the ARQ acknowledgement path is TCP even when the data goes over the air,
            # so every node still has to know where its neighbours actually live
            print("[peer] WARNING: --peer-link wireless without --peers — ARQ acks will be "
                  "sent to 127.0.0.1. Give --peers <host per node> for a real multi-host run.")
        if len(self.hosts) < self.n:
            raise ValueError(f"--peers lists {len(self.hosts)} hosts but there are {self.n} nodes")
        self.base_port, self.link = int(base_port), link
        self.connect_timeout = float(connect_timeout)
        self.lora_timeout = float(lora_timeout)
        self._inbox = {}                       # frames that arrived out of schedule order
        self._srv = None
        if link == "tcp":
            self._srv = socket.socket()
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind(("0.0.0.0", self.base_port + self.id))
            self._srv.listen(max(8, self.n))
        elif link == "lora":
            # the LoRa PHY, addressed node-to-node over the shared medium
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "drivers", "lora", "python"))
            import framing as _framing
            from lora_radio import make_radio as _make_radio
            self._framing = _framing
            kw = dict(sf=lora_sf, cr=lora_cr, bw_hz=lora_bw, power_dbm=lora_power)
            if lora_backend == "sim":
                kw.update(medium=lora_medium, snr_db=lora_snr_db)
            elif lora_port:
                kw["port"] = lora_port
            self.radio = _make_radio(lora_backend, **kw)
            self._msg_id = 0
            if lora_backend == "sim" and lora_medium is None:
                # The simulated medium lives inside ONE process, so peers started in
                # separate terminals each get their own private air and never hear each
                # other. Real radios share real air; the sim needs one process.
                print("[peer] WARNING: --peer-link lora with the sim backend — the "
                      "simulated medium is per-process, so separate peer processes "
                      "CANNOT hear each other. Use --lora-backend serial|spi for real "
                      "radios, or run the whole network in one process with "
                      "--role gossip --channel lora.")
        else:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "drivers", "usrp", "python"))
            import sdr, tempfile
            self.sdr, self.tmp = sdr, tempfile.gettempdir()
            self.tx_args, self.rx_args = tx_args, rx_args
            self.tx_gain, self.rx_gain, self.ack_port = tx_gain, rx_gain, ack_port
            self.cfg = dict(scheme=scheme, waveform=waveform, fec=True, rx_freq=915e6,
                            tx_freq=915e6, tx_rate=2e6, rx_rate=2e6, symbol_rate=1e6,
                            rx_ant=rx_ant, tx_ant=tx_ant, rx_subdev=rx_subdev,
                            tx_subdev=tx_subdev, det_mult=3, ack_transport="tcp",
                            ack_port=ack_port, bytes_length=chunk, viz=False)
        where = (f" | listening on :{self.base_port + self.id}" if link == "tcp"
                 else f" | radio tx[{tx_args or 'default'}] rx[{rx_args or 'default'}]")
        print(f"[peer] node {self.id}/{self.n} | neighbours {self.neighbours} | "
              f"{len(self.edges)} edges in the round | link={link}{where}")

    # ── TCP transport ────────────────────────────────────────────────────────
    def _recvn(self, sock, n):
        b = b""
        while len(b) < n:
            c = sock.recv(n - len(b))
            if not c:
                raise ConnectionError("peer closed")
            b += c
        return b

    def _send_tcp(self, pid, buf):
        host, port = self.hosts[pid], self.base_port + pid
        import time
        deadline = time.time() + self.connect_timeout
        while True:                                  # the peer's terminal may start later
            try:
                s = self.socket.create_connection((host, port), timeout=5.0)
                break
            except OSError:
                if time.time() > deadline:
                    raise ConnectionError(f"node {pid} at {host}:{port} never came up")
                time.sleep(0.25)
        try:
            s.sendall(self._st.pack(">IH", len(buf), self.id) + buf)
        finally:
            s.close()

    def _recv_tcp(self, pid):
        """Read the next frame FROM pid. A neighbour that runs ahead of the schedule is
        buffered by sender id rather than being mistaken for the one we are waiting on."""
        if self._inbox.get(pid):
            return self._inbox[pid].pop(0)
        while True:
            conn, _ = self._srv.accept()
            try:
                n, src = self._st.unpack(">IH", self._recvn(conn, 6))
                buf = self._recvn(conn, n)
            finally:
                conn.close()
            if src == pid:
                return buf
            self._inbox.setdefault(src, []).append(buf)

    # ── wireless transport (the ARQ byte-pipe, one exchange at a time) ───────
    def _send_wl(self, pid, buf):
        path = os.path.join(self.tmp, f"peer{self.id}_tx.bin")
        open(path, "wb").write(buf)
        self.sdr.source_arq(tx_args=self.tx_args, rx_args=self.tx_args, tx_gain=self.tx_gain,
                            ack_host=self.hosts[pid], max_attempts=50,
                            payload_file=path, **self.cfg).run()

    def _recv_wl(self, pid):
        path = os.path.join(self.tmp, f"peer{self.id}_rx.bin")
        if os.path.exists(path):
            os.remove(path)
        self.sdr.sink_arq(rx_args=self.rx_args, tx_args=self.rx_args, rx_gain=self.rx_gain,
                          out_file=path, **self.cfg).run()
        return open(path, "rb").read()

    # ── LoRa transport: the driver's fragmentation + ARQ, addressed node-to-node ──
    def _send_lora(self, pid, buf):
        self._msg_id = (self._msg_id + 1) & 0xFF
        self._framing.send_message(self.radio, buf, msg_id=self._msg_id,
                                   src=self.id, dst=pid)

    def _recv_lora(self, pid):
        data, _ = self._framing.recv_message(self.radio, timeout=self.lora_timeout,
                                             me=self.id)
        if data is None:
            raise ConnectionError(f"no LoRa message from node {pid} within "
                                  f"{self.lora_timeout:.0f}s")
        return data

    def _send(self, pid, buf):
        {"tcp": self._send_tcp, "lora": self._send_lora}.get(
            self.link, self._send_wl)(pid, buf)

    def _recv(self, pid):
        return {"tcp": self._recv_tcp, "lora": self._recv_lora}.get(
            self.link, self._recv_wl)(pid)

    # ── one round: produce once, then walk the shared schedule ───────────────
    def step(self, app):
        payload = app.produce()                  # mix in last round's arrivals, train, emit
        if payload is None:
            return False
        buf = Codec.pack(app.spec.check(payload))
        for (a, b) in self.edges:
            if self.id not in (a, b):
                continue                          # not my edge — sit this exchange out
            peer = b if a == self.id else a
            if self.id == a:                      # the node named first speaks first
                self._send(peer, buf)
                got = self._recv(peer)
            else:
                got = self._recv(peer)
                self._send(peer, buf)
            msg = _safe_unpack(got, app.spec)
            if msg is not None:
                app.consume(msg)
            app.on_result(msg is not None)
        return True

    def close(self):
        if self._srv is not None:
            self._srv.close()


# ── two-host radio round-trip: request over the USRP link, reply over TCP ──────
#   (mirrors fl.py's proven --uplink wireless --downlink tcp; needs UHD + two hosts)
class RadioRoundTrip:
    """Initiator: send request over source_arq (wireless), receive reply over TCP.
    Responder: receive request over sink_arq (wireless), send reply over TCP.
    Relay:     BOTH — receive over the air from upstream, re-transmit over the air to
               downstream, then carry the reply back upstream over TCP.
    Wireless is the B210->N210 uplink; the reply goes over TCP so the RX-only N210
    never has to transmit — the exact split fl.py uses.

    A relay node therefore uses rx_args for the radio it listens on and tx_args for the
    radio it forwards with (two radios, or one that does both), serves net_host:net_port
    to the node upstream of it, and reads its own reply from down_host:down_port."""

    def __init__(self, role, tx_args="", rx_args="", ack_host="127.0.0.1", ack_port=5599,
                 net_host="127.0.0.1", net_port=5700, scheme="DQPSK", waveform="sc",
                 tx_gain=70, rx_gain=30, rx_subdev="A:0", tx_subdev="A:A",
                 rx_ant="RX2", tx_ant="TX/RX", chunk=125,
                 down_host=None, down_port=None, freq_hz=915e6, samp_rate=2e6,
                 symbol_rate=1e6, fec="conv", ack_transport="tcp", ack_timeout_ms=3000,
                 max_attempts=50, arq="stop-and-wait"):
        # sdr.py lives in the USRP driver (union/ -> repo -> drivers/usrp/python)
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "drivers", "usrp", "python"))
        import sdr, socket, struct as _st
        self.sdr, self.socket, self._st = sdr, socket, _st
        self.role = role
        self.net_host, self.net_port = net_host, net_port
        # The USRP PHY is assembled from parts WE choose — carrier, sample/symbol rate,
        # modulation, FEC, gains — so every one of them is a parameter here rather than
        # a constant. (They used to be hardcoded, which silently ignored the CLI.)
        #
        # sdr.py splits FEC in two: `fec` turns coding on/off and `fec-type` picks the
        # family. So "" means off, and conv|ldpc|turbo means on with that code. ldpc and
        # turbo are soft-native, so they get soft decision as sdr.py recommends.
        fec_type = (fec or "").strip() or None
        self.cfg = dict(scheme=scheme, waveform=waveform, fec=bool(fec_type),
                        rx_freq=float(freq_hz), tx_freq=float(freq_hz),
                        tx_rate=float(samp_rate), rx_rate=float(samp_rate),
                        symbol_rate=float(symbol_rate), rx_ant=rx_ant, tx_ant=tx_ant,
                        rx_subdev=rx_subdev, tx_subdev=tx_subdev, det_mult=3,
                        ack_transport=ack_transport, ack_port=ack_port,
                        timeout=int(ack_timeout_ms), bytes_length=chunk, viz=False)
        # The C++ PHY implements stop-and-wait (ACQ_stop_and_wait.hpp) and nothing else
        # yet, so anything else is refused rather than quietly downgraded. What IS
        # pluggable today is where the acknowledgement travels: tcp (a socket, no reverse
        # RF) or rf (a second RF path, RF B, needs full duplex).
        self.arq = check_arq(arq, "usrp")
        self.max_attempts = int(max_attempts)
        if fec_type:
            self.cfg["fec_type"] = fec_type
            if fec_type in ("ldpc", "turbo"):
                self.cfg["fec_soft"] = True
        self.tx_args, self.rx_args = tx_args, rx_args
        self.ack_host, self.tx_gain, self.rx_gain = ack_host, tx_gain, rx_gain
        # the next hop downstream (relay only): where this node reads its reply from
        self.down_host = down_host or net_host
        self.down_port = int(down_port) if down_port else net_port + 1
        import tempfile
        self.tmp = tempfile.gettempdir()

    def _wl_send(self, buf):                    # wireless uplink (source_arq)
        path = os.path.join(self.tmp, "phylink_tx.bin"); open(path, "wb").write(buf)
        self.sdr.source_arq(tx_args=self.tx_args, rx_args=self.tx_args, tx_gain=self.tx_gain,
                            ack_host=self.ack_host, max_attempts=self.max_attempts,
                            payload_file=path, **self.cfg).run()

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

    def _tcp_connect(self, host, port):
        """The downstream node binds its port only AFTER it decodes our burst, so retry."""
        import time
        for _ in range(50):
            try:
                return self.socket.create_connection((host, port), timeout=2.0)
            except OSError:
                time.sleep(0.2)
        raise ConnectionError(f"reply server {host}:{port} never came up")

    def _tcp_serve_once(self, buf):
        """Hand one framed payload to whoever connects upstream, then close."""
        srv = self.socket.socket()
        srv.setsockopt(self.socket.SOL_SOCKET, self.socket.SO_REUSEADDR, 1)
        srv.bind((self.net_host, self.net_port)); srv.listen(1)
        conn, _ = srv.accept()
        try:
            self._tcp_frame(conn, buf)
        finally:
            conn.close(); srv.close()

    def _pack(self, app, y):
        return Codec.pack(app.spec.check(y if y is not None else np.zeros(1, np.float32)))

    # request/response per step. `app` supplies produce/consume; role decides order.
    def step(self, app):
        if self.role == "tx":                    # client: send req, TCP-recv reply
            x = app.produce()
            if x is None: return False
            self._wl_send(Codec.pack(app.spec.check(x)))
            s = self._tcp_connect(self.net_host, self.net_port)
            try:
                reply = _safe_unpack(self._tcp_read(s), app.spec)
                if reply is not None:
                    app.consume(reply)
                app.on_result(reply is not None)
            finally:
                s.close()
            return True

        if self.role == "relay":
            # ── RECEIVE from upstream over the air, then TRANSMIT downstream over the
            #    air; carry the downstream reply back upstream over TCP. consume/produce
            #    are called once per direction, so a pass-through relay re-emits what it
            #    got and a processing relay can transform each leg. ──
            msg = _safe_unpack(self._wl_recv(), app.spec)          # <- upstream, wireless
            if msg is not None:
                app.consume(msg)
            self._wl_send(self._pack(app, app.produce()))          # -> downstream, wireless
            s = self._tcp_connect(self.down_host, self.down_port)  # <- downstream reply, TCP
            try:
                back = _safe_unpack(self._tcp_read(s), app.spec)
            finally:
                s.close()
            if back is not None:
                app.consume(back)
            self._tcp_serve_once(self._pack(app, app.produce()))   # -> upstream reply, TCP
            app.on_result(back is not None)
            return True

        # server: wl-recv req, TCP-send reply
        msg = _safe_unpack(self._wl_recv(), app.spec)
        if msg is not None: app.consume(msg)
        self._tcp_serve_once(self._pack(app, app.produce()))
        return True
