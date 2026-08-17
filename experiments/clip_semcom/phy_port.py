#!/usr/bin/env python3
"""
phy_port.py — the API port that connects CLIP semantic communication to this
repo's SDR PHY. This is the "data-transfer archetype" on the (proposed) SdrApp
contract: the app hands the PHY a float32 embedding and gets one back.

Layers:
  PayloadSpec(dtype, shape)      — the app DECLARES its output shape+type
  SemComCodec.pack / unpack      — float32 embedding <-> self-describing bytes
  PhyLink(backend=...)           — one transport, three interchangeable backends:
       "ideal"  : lossless in-memory (mock / logic check)
       "pyphy"  : our REAL modem + AWGN, radio-free (reproduces noise->accuracy)
       "radio"  : the USRP link via sdr.source_arq / sink_arq (like fl.py)
  SemComTxApp / SemComRxApp      — the SdrApp pair (next_payload / on_payload)

Radio-free backends run on any python3. The "pyphy" backend needs the built
extension (PYTHONPATH=bindings, arch -x86_64 on macOS). The "radio" backend needs
UHD + the sdr_system binary and imports ../../phy/python/sdr.py.
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp", "python"))
import semcom_core as core


# ── the contract's schema object ─────────────────────────────────────────────
class PayloadSpec:
    def __init__(self, dtype="float32", shape=(512,)):
        self.dtype, self.shape = dtype, tuple(shape)

    def __repr__(self):
        return f"PayloadSpec(dtype={self.dtype!r}, shape={self.shape})"


class SemComCodec:
    """One serializer for the embedding vector — thin wrapper over semcom_core."""
    def __init__(self, model_key="vit-b32", quant="f32"):
        self.model_key, self.quant = model_key, quant
        self.model_id = core.MODEL_TABLE[model_key]["id"]
        self.spec = PayloadSpec("float32", (core.MODEL_TABLE[model_key]["dim"],))

    def pack(self, vec, sample_id=0):
        return core.pack_embedding(vec, self.model_id, sample_id, self.quant)

    def unpack(self, buf):
        return core.unpack_embedding(buf)          # -> (vec, model_id, sample_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Radio-free channels (transfer bytes -> bytes) for offline / mock evaluation
# ══════════════════════════════════════════════════════════════════════════════
class IdealChannel:
    name = "ideal"
    def transfer(self, buf):
        return buf, dict(ber=0.0, crc_ok=True)


class PyphyChannel:
    """Push the payload BYTES through the repo's real modem (pyphy) with AWGN at a
    target Es/N0. No CRC gate — residual bit errors pass through and corrupt the
    embedding floats, exactly the semantic-comm regime the paper studies."""
    name = "pyphy"

    def __init__(self, scheme="QPSK", fec=None, k=256, snr_db=8.0, soft=True, seed=0):
        import pyphy                                # raises if not built
        self.pyphy = pyphy
        self.scheme, self.fec, self.k = scheme, fec, k
        self.snr_db, self.soft = snr_db, soft
        self.rng = np.random.RandomState(seed)
        self.bps = {"BPSK": 1, "QPSK": 2, "8-PSK": 3, "16-QAM": 4,
                    "DBPSK": 1, "DQPSK": 2}.get(scheme, 2)

    def transfer(self, buf):
        p = self.pyphy
        bits = np.unpackbits(np.frombuffer(buf, np.uint8))
        nbits = bits.size
        tx = p.fec_encode(bits.astype(np.uint8), self.fec, self.k) if self.fec else bits.astype(np.uint8)
        pad = (-tx.size) % self.bps
        if pad:
            tx = np.concatenate([tx, np.zeros(pad, np.uint8)])
        syms = p.modulate(tx, self.scheme).astype(np.complex64)
        es = float(np.mean(np.abs(syms) ** 2)) or 1.0
        sigma = np.sqrt(es / (2.0 * 10 ** (self.snr_db / 10.0)))
        noise = sigma * (self.rng.randn(syms.size) + 1j * self.rng.randn(syms.size))
        rx = (syms + noise).astype(np.complex64)
        if self.fec and self.soft:
            llr = p.soft_llr(rx, self.scheme, float(2 * sigma ** 2))
            rbits = p.fec_decode_soft(llr, self.fec, self.k, info_len=nbits)
        else:
            hard = p.demodulate(rx, self.scheme)
            rbits = p.fec_decode(hard.astype(np.uint8), self.fec, self.k, info_len=nbits) if self.fec \
                else hard[:nbits]
        rbits = np.asarray(rbits, np.uint8)[:nbits]
        if rbits.size < nbits:
            rbits = np.concatenate([rbits, np.zeros(nbits - rbits.size, np.uint8)])
        ber = float(np.mean(rbits != bits))
        out = np.packbits(rbits).tobytes()[:len(buf)]
        return out, dict(ber=ber, crc_ok=(ber == 0), snr_db=self.snr_db)


def make_channel(backend="ideal", **kw):
    if backend == "ideal":
        return IdealChannel()
    if backend == "pyphy":
        return PyphyChannel(**kw)
    raise ValueError(f"radio-free backend must be ideal|pyphy (got {backend!r}); "
                     f"use RadioPhyLink for the USRP link")


# ══════════════════════════════════════════════════════════════════════════════
#  Radio transport (two-host USRP link) — mirrors fl.py's proven byte-pipe
# ══════════════════════════════════════════════════════════════════════════════
class RadioPhyLink:
    """send(buf) on the TX host, recv() on the RX host — reliable ARQ over the
    B210->N210 link, same path fl.py uses. Requires UHD + ../../phy/python/sdr.py."""
    name = "radio"

    def __init__(self, tx_args="", rx_args="", ack_host="127.0.0.1", ack_port=5599,
                 scheme="DQPSK", waveform="sc", tx_gain=70, rx_gain=30,
                 rx_subdev="A:0", tx_subdev="A:A", chunk=125, timeout=8.0, max_attempts=50):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", "drivers", "usrp", "python"))
        import sdr, tempfile, atexit
        self.sdr, self.tempfile, self.atexit = sdr, tempfile, atexit
        self.cfg = dict(scheme=scheme, waveform=waveform, fec=True, rx_freq=915e6, tx_freq=915e6,
                        tx_rate=2e6, rx_rate=2e6, symbol_rate=1e6, rx_ant="RX2", tx_ant="TX/RX",
                        rx_subdev=rx_subdev, tx_subdev=tx_subdev, det_mult=3, ack_transport="tcp",
                        ack_port=ack_port, bytes_length=chunk, viz=False)
        self.tx_args, self.rx_args = tx_args, rx_args
        self.ack_host, self.tx_gain, self.rx_gain = ack_host, tx_gain, rx_gain
        self.timeout, self.max_attempts = timeout, max_attempts

    def _scratch(self, tag):
        p = os.path.join(self.tempfile.gettempdir(), f"semcom_{tag}.bin")
        self.atexit.register(lambda: os.path.exists(p) and os.remove(p))
        return p

    def send(self, buf, tag="emb"):
        path = self._scratch(tag)
        with open(path, "wb") as f:
            f.write(buf)
        self.sdr.source_arq(tx_args=self.tx_args, rx_args=self.tx_args, tx_gain=self.tx_gain,
                            ack_host=self.ack_host, timeout=self.timeout,
                            max_attempts=self.max_attempts, payload_file=path, **self.cfg).run()

    def recv(self, tag="emb"):
        path = self._scratch(tag)
        if os.path.exists(path):
            os.remove(path)
        self.sdr.sink_arq(rx_args=self.rx_args, tx_args=self.rx_args, rx_gain=self.rx_gain,
                          out_file=path, **self.cfg).run()
        with open(path, "rb") as f:
            return f.read()


# ══════════════════════════════════════════════════════════════════════════════
#  The SdrApp pair (data-transfer archetype)
# ══════════════════════════════════════════════════════════════════════════════
class SemComTxApp:
    """Base station: emits one CLIP embedding per image via next_payload()."""
    def __init__(self, images, clip, codec):
        self.images, self.clip, self.codec = images, clip, codec
        self.i = 0

    def next_payload(self):
        if self.i >= len(self.images):
            return None
        f = self.clip.encode_image(self.images[self.i])
        buf = self.codec.pack(f, sample_id=self.i)
        self.i += 1
        return buf                                  # bytes for the PHY to transmit


class SemComRxApp:
    """User: on each received embedding, run the follow-up task (classify)."""
    def __init__(self, codec, text_features):
        self.codec, self.f_texts = codec, text_features
        self.preds = []

    def on_payload(self, buf):
        vec, _mid, sid = self.codec.unpack(buf)
        yhat, sims = core.classify(vec, self.f_texts)
        self.preds.append((sid, yhat))
        return yhat, sims
