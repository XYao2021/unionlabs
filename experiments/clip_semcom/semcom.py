#!/usr/bin/env python3
"""
semcom.py — CLIP semantic communication over the SDR PHY (arXiv:2507.08873).

The base station encodes an image with a pretrained CLIP model into a float32
embedding, transmits ONLY that embedding over the channel, and the user runs its
follow-up task (zero-shot classification) on the received embedding — no return
link, no joint training.

MODES
  demo   (default)  radio-free: encode -> channel -> classify, report accuracy
  tx                real radio: encode images, send each embedding (B210 TX)
  rx                real radio: receive embeddings, classify (N210 RX)

CHANNELS (demo)   --channel ideal | pyphy      (pyphy = our real modem + AWGN)
EXAMPLES
  # 1) end-to-end sanity (mock CLIP, lossless channel):
  python3 semcom.py demo --mock

  # 2) reproduce the paper's accuracy-vs-noise (Fig. 3) through OUR modem:
  PYTHONPATH=../../phy/bindings arch -x86_64 python3 semcom.py demo --mock \
      --channel pyphy --scheme QPSK --fec turbo --snr-sweep 0,2,4,6,8,12

  # 3) the 3-CLIP-model accuracy / payload / delay / energy tradeoff (Table I):
  python3 semcom.py demo --mock --model-sweep

  # 4) over the radio (two hosts):  RX first, then TX
  python3 semcom.py rx --rx-args addr=192.168.20.2
  python3 semcom.py tx --tx-args serial=30CD424 --ack-host <RX_HOST_IP>
"""
import argparse, sys, os, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp", "python"))
import semcom_core as core
import phy_port as port


def _clip_and_data(a):
    imgs, y, labels = core.synth_dataset(per_class=a.per_class, texture=a.texture, seed=a.seed)
    clip = core.load_clip(a.model, force_mock=a.mock)
    f_texts = clip.encode_texts(labels)
    return imgs, y, labels, clip, f_texts


def _encode_all(clip, imgs):
    return np.stack([clip.encode_image(im) for im in imgs])


# ── demo: radio-free encode -> channel -> classify ───────────────────────────
def run_demo(a):
    imgs, y, labels, clip, f_texts = _clip_and_data(a)
    codec = port.SemComCodec(a.model, quant=a.quant)
    print(f"[semcom] CLIP={clip.name}  spec={codec.spec}  quant={a.quant}  "
          f"N={len(imgs)} images / {len(labels)} classes  payload={core.embedding_nbytes(codec.spec.shape[0], a.quant)} B")
    f_img_clean = _encode_all(clip, imgs)
    acc0, _ = core.accuracy(f_img_clean, y, f_texts)
    print(f"[semcom] noise-free (channel-perfect) top-1 accuracy = {acc0:.3f}")

    snrs = [float(s) for s in a.snr_sweep.split(",")] if a.snr_sweep else [a.snr_db]
    print(f"\n  {'SNR dB':>7} | {'BER':>8} | {'accuracy':>8} | channel")
    for snr in snrs:
        if a.channel == "ideal":
            ch = port.IdealChannel()
        else:
            ch = port.PyphyChannel(scheme=a.scheme, fec=(a.fec or None), k=a.k,
                                   snr_db=snr, soft=not a.hard, seed=a.seed)
        preds, bers = [], []
        for i, f in enumerate(f_img_clean):
            buf = codec.pack(f, sample_id=i)
            out, info = ch.transfer(buf)
            bers.append(info["ber"])
            try:
                vec, _, _ = codec.unpack(out)
                if not np.all(np.isfinite(vec)) or np.max(np.abs(vec)) > 1e30:
                    raise ValueError("wrecked embedding")   # bit errors destroyed the floats
                preds.append(core.classify(vec, f_texts)[0])
            except Exception:
                preds.append(-1)                    # frame destroyed -> miss
        acc = float(np.mean(np.array(preds) == y))
        print(f"  {snr:7.1f} | {np.mean(bers):8.4f} | {acc:8.3f} | {ch.name}/{a.scheme}"
              + (f"+{a.fec}" if a.fec else ""))
    if a.channel == "pyphy":
        print("\n  (accuracy falls as SNR drops — the paper's Fig. 3, through our real modem)")


# ── the 3-CLIP-model tradeoff (accuracy / payload / delay / energy) ──────────
def run_model_sweep(a):
    imgs, y, labels, _, _ = _clip_and_data(a)      # dataset only (clip per model below)
    print(f"  {'model':>9} | {'dim':>4} | {'payload B':>9} | {'t_tx ms':>7} | "
          f"{'energy':>7} | {'clean acc':>9}")
    for key in core.MODEL_TABLE:
        clip = core.load_clip(key, force_mock=a.mock)
        f_texts = clip.encode_texts(labels)
        f_img = _encode_all(clip, imgs)
        acc, _ = core.accuracy(f_img, y, f_texts)
        m = core.link_metrics(key, n_rb=a.n_rb, quant=a.quant)
        print(f"  {key:>9} | {m['dim']:>4} | {core.embedding_nbytes(m['dim'], a.quant):>9} | "
              f"{m['t_tx']*1e3:7.3f} | {m['energy']:7.4f} | {acc:9.3f}")
    print("\n  bigger model -> higher accuracy/robustness but larger payload, delay, energy")
    print("  (the accuracy<->delay<->energy tradeoff the paper's PPO agent optimises)")


# ── real radio roles ─────────────────────────────────────────────────────────
def run_tx(a):
    imgs, y, labels, clip, _ = _clip_and_data(a)
    codec = port.SemComCodec(a.model, quant=a.quant)
    link = port.RadioPhyLink(tx_args=a.tx_args, ack_host=a.ack_host, ack_port=a.ack_port,
                             scheme=a.scheme, waveform=a.waveform, tx_gain=a.tx_gain,
                             tx_subdev=a.tx_subdev, chunk=a.chunk)
    app = port.SemComTxApp(imgs, clip, codec)
    print(f"[tx] CLIP={clip.name} sending {len(imgs)} embeddings ({codec.spec}) over the radio")
    n = 0
    while True:
        buf = app.next_payload()
        if buf is None:
            break
        link.send(buf, tag=f"emb{n}")
        print(f"[tx] sent embedding {n} (label={labels[y[n]]}, {len(buf)} B)")
        n += 1
    print(f"[tx] done — {n} embeddings sent")


def run_rx(a):
    imgs, y, labels, clip, f_texts = _clip_and_data(a)   # RX builds text features locally
    codec = port.SemComCodec(a.model, quant=a.quant)
    link = port.RadioPhyLink(rx_args=a.rx_args, ack_port=a.ack_port, scheme=a.scheme,
                             waveform=a.waveform, rx_gain=a.rx_gain, rx_subdev=a.rx_subdev,
                             chunk=a.chunk)
    app = port.SemComRxApp(codec, f_texts)
    print(f"[rx] CLIP={clip.name} waiting for embeddings; classifying into {len(labels)} classes")
    n = 0
    while n < a.num:
        buf = link.recv(tag=f"emb{n}")
        yhat, _ = app.on_payload(buf)
        truth = labels[y[n]] if n < len(y) else "?"
        print(f"[rx] embedding {n}: predicted={labels[yhat]}  (truth={truth})")
        n += 1
    print(f"[rx] done — {n} classified")


def build_argparser():
    p = argparse.ArgumentParser(description="CLIP semantic communication over the SDR PHY")
    p.add_argument("mode", nargs="?", default="demo", choices=["demo", "tx", "rx"])
    p.add_argument("--model", default="vit-b32", choices=list(core.MODEL_TABLE))
    p.add_argument("--mock", action="store_true", help="force the deterministic mock CLIP (no torch weights)")
    p.add_argument("--quant", default="f32", choices=["f32", "f16", "int8"])
    p.add_argument("--per-class", type=int, default=8)
    p.add_argument("--texture", type=float, default=0.15, help="synthetic-image noise (mock data)")
    p.add_argument("--seed", type=int, default=0)
    # demo channel
    p.add_argument("--channel", default="ideal", choices=["ideal", "pyphy"])
    p.add_argument("--scheme", default="QPSK")
    p.add_argument("--fec", default="", choices=["", "conv", "ldpc", "turbo"])
    p.add_argument("--k", type=int, default=256)
    p.add_argument("--hard", action="store_true", help="hard-decision FEC (default soft)")
    p.add_argument("--snr-db", type=float, default=8.0)
    p.add_argument("--snr-sweep", default="", help="comma list, e.g. 0,2,4,6,8,12")
    p.add_argument("--model-sweep", action="store_true", help="compare the 3 CLIP models")
    p.add_argument("--n-rb", type=int, default=1, help="resource blocks (delay/energy model)")
    # radio
    p.add_argument("--tx-args", default="")
    p.add_argument("--rx-args", default="")
    p.add_argument("--ack-host", default="127.0.0.1")
    p.add_argument("--ack-port", type=int, default=5599)
    p.add_argument("--tx-gain", type=float, default=70)
    p.add_argument("--rx-gain", type=float, default=30)
    p.add_argument("--tx-subdev", default="A:A")
    p.add_argument("--rx-subdev", default="A:0")
    p.add_argument("--waveform", default="sc", choices=["sc", "ofdm"])
    p.add_argument("--chunk", type=int, default=125)
    p.add_argument("--num", type=int, default=8, help="rx: how many embeddings to receive")
    return p


def main():
    a = build_argparser().parse_args()
    if a.mode == "demo":
        run_model_sweep(a) if a.model_sweep else run_demo(a)
    elif a.mode == "tx":
        run_tx(a)
    elif a.mode == "rx":
        run_rx(a)


if __name__ == "__main__":
    main()
