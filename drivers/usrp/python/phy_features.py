#!/usr/bin/env python3
"""
phy_features.py — extract every PHY feature we can honestly measure from one IQ
capture, and say which estimator produced each one.

    # capture from a radio and write both the features and the raw IQ
    python3 phy_features.py --args serial=30CD3F7 --freq 915e6 --n 1000000 \
        --save-iq capture.npy --json features.json

    # re-analyse a capture later, with no radio present
    python3 phy_features.py --load-iq capture.npy --rate 2e6

    # from Python
    from phy_features import extract, capture_and_extract
    f = capture_and_extract(args="serial=30CD3F7", freq_hz=915e6, rate_hz=2e6, n=1 << 20)
    print(f["power"]["mean_dbfs"], f["spectrum"]["snr_db"])

WHY THIS EXISTS. The numbers a reviewer asks for -- received power, SNR, occupied
bandwidth, EVM, CFO -- were each obtainable before this file, but from five different
places: a printed `[SENSE]` line, a `[ACQ]` line, a `--viz` figure, a post-processing
plotter. None of them wrote down the raw samples, so a figure could be rendered and
then never re-checked. This takes ONE capture, derives everything from it, and saves
the IQ beside the numbers so any of them can be recomputed or disputed later.

HONEST UNITS, which matter more than the numbers. A USRP reports no absolute
reference, so everything here is **dBFS** -- decibels relative to the ADC's full
scale -- and NOT dBm. The same antenna and gain on two radios can read differently.
Pass --cal-offset-db if you have calibrated this chain against a known source, and
the output will carry a dbm field alongside; without it, no dbm is reported rather
than a fabricated one. Every value names the estimator that produced it, because
"SNR" in particular means at least three different things in this codebase (see the
`method` field, and the note on `[ACQ] Correlation SNR` in the README of this folder).

WHAT NEEDS WHAT. Power, spectrum, SNR, occupied bandwidth and IQ quality come from
any capture of anything. Sync features (detection, correlation peak, timing offset,
CFO) only mean something when the capture contains OUR OWN preamble, so they are
computed only when --preamble-m is given, and are reported as null otherwise rather
than as zeros that look like measurements.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

EPS = 1e-30


# ── power, straight from the samples ─────────────────────────────────────────
def power_features(x, cal_offset_db=None):
    """Mean/peak power in dBFS, and PAPR. No assumptions about what x contains."""
    p = np.abs(x).astype(np.float64) ** 2
    mean_p, peak_p = float(p.mean()), float(p.max())
    out = {
        "mean_dbfs": 10.0 * np.log10(mean_p + EPS),
        "peak_dbfs": 10.0 * np.log10(peak_p + EPS),
        "papr_db": 10.0 * np.log10((peak_p + EPS) / (mean_p + EPS)),
        "rms_amplitude": float(np.sqrt(mean_p)),
        # "RSSI" is conventionally absolute; ours is not, so it is named for what it
        # is. Reporting a dBFS figure as RSSI is how a relative number ends up in a
        # table that implies calibration.
        "note": "dBFS — relative to ADC full scale, NOT dBm",
    }
    if cal_offset_db is not None:
        out["mean_dbm"] = out["mean_dbfs"] + float(cal_offset_db)
        out["peak_dbm"] = out["peak_dbfs"] + float(cal_offset_db)
        out["cal_offset_db"] = float(cal_offset_db)
    return out


# ── spectrum: noise floor, occupied bandwidth, and an in-band SNR ────────────
def _welch(x, rate_hz, nfft=4096):
    """Welch PSD (density, W/Hz-ish in dBFS terms), returned as (freqs_hz, psd_lin)."""
    nfft = int(min(nfft, len(x)))
    if nfft < 64:
        return None, None
    win = np.hanning(nfft)
    step = nfft // 2
    segs = [x[i:i + nfft] * win for i in range(0, len(x) - nfft + 1, step)]
    if not segs:
        return None, None
    acc = np.zeros(nfft, dtype=np.float64)
    for s in segs:
        acc += np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2
    # normalise by window energy and segment count so the level is comparable
    psd = acc / (len(segs) * (win ** 2).sum() * rate_hz)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / rate_hz))
    return freqs, psd


def spectrum_features(x, rate_hz, nfft=4096, occupancy_db=10.0, min_occupancy=0.005):
    """Noise floor, occupied bandwidth, and an in-band SNR estimate.

    METHOD (recorded in the output as `method`): take the Welch PSD; estimate the
    noise density as the MEDIAN of the lowest-quartile bins, which is robust to a
    signal filling part of the band; call a bin occupied when it exceeds that noise
    density by `occupancy_db`; then

        in-band SNR = 10 log10( (P_occupied - N_occupied) / N_occupied )

    where N_occupied is the noise density times the occupied bin count.

    TWO SNRs ARE REPORTED, because the single word is ambiguous and the difference is
    not small -- a signal filling a quarter of the capture differs by 6 dB between
    them:
      snr_db             in-band: noise counted only inside the occupied bandwidth.
                         This is what a receiver experiences, and the one to quote.
      snr_total_band_db  over the whole captured bandwidth. Depends on the rate you
                         happened to capture at, so it is informational only.

    WHY occupancy_db DEFAULTS TO 10. A noise-only PSD has a long tail: for an
    exponentially distributed bin, ~6% of bins sit more than 6 dB above the median,
    so a 6 dB threshold reads the tail of pure noise as a weak signal and returns a
    confident negative SNR for an idle channel. At 10 dB that tail is ~0.1%, which
    the min_occupancy guard then rejects outright. A real in-band signal sits far
    above 10 dB, so nothing useful is lost.

    IT REFUSES RATHER THAN GUESSES in three knowable cases, each reported as a null
    `snr_db` with a `why`: too little occupancy to be a signal (idle), occupancy
    above 80% (the noise estimate is then itself mostly signal), and occupied power
    not exceeding the noise estimate. For an SNR that goes in a report, prefer the
    two-capture method (`snr_from_noise_reference`) with the transmitter keyed off --
    it assumes nothing about where the signal sits.
    """
    freqs, psd = _welch(x, rate_hz, nfft)
    if psd is None:
        return {"method": "welch-quartile", "snr_db": None,
                "why": f"capture too short for an FFT ({len(x)} samples)"}
    srt = np.sort(psd)
    noise_density = float(np.median(srt[:max(1, len(srt) // 4)]))
    thresh = noise_density * (10.0 ** (occupancy_db / 10.0))
    occ = psd > thresh
    n_occ = int(occ.sum())
    bin_hz = float(freqs[1] - freqs[0]) if len(freqs) > 1 else rate_hz

    out = {
        "method": "welch-quartile",
        "noise_density_dbfs_per_hz": 10.0 * np.log10(noise_density + EPS),
        "noise_floor_dbfs": 10.0 * np.log10(noise_density * rate_hz + EPS),
        "occupied_bandwidth_hz": n_occ * bin_hz,
        "occupied_fraction": n_occ / len(psd),
        "nfft": int(nfft),
        "occupancy_threshold_db": float(occupancy_db),
    }
    out["min_occupancy"] = float(min_occupancy)
    if out["occupied_fraction"] < min_occupancy:
        out["snr_db"] = None
        out["snr_total_band_db"] = None
        out["why"] = ("only %.3f%% of bins rose %.1f dB above the floor — that is "
                      "the tail of the noise distribution, not a signal; the "
                      "channel reads as idle"
                      % (100.0 * out["occupied_fraction"], occupancy_db))
        return out
    # 0.80, not 0.95: the noise density is the median of the LOWEST QUARTILE, so if
    # a quarter of the bins sat at the true floor at most three quarters could read
    # as occupied -- a >0.95 test was unreachable code, which the self-test caught by
    # never being able to trigger it. Above 0.80 the lowest-quartile window is itself
    # mostly signal, so the noise density is contaminated and every number derived
    # from it is wrong by an unknown amount. Refusing is the only honest answer.
    if out["occupied_fraction"] > 0.80:
        out["snr_db"] = None
        out["snr_total_band_db"] = None
        out["why"] = ("%.0f%% of the captured bandwidth is occupied, so the "
                      "noise-floor estimate is itself mostly signal and any SNR "
                      "from it would be meaningless; capture at a higher rate so "
                      "there is a noise-only region, or use a noise reference"
                      % (100.0 * out["occupied_fraction"]))
        return out
    p_occ = float(psd[occ].sum())
    p_all = float(psd.sum())
    n_in_occ = noise_density * n_occ
    n_all = noise_density * len(psd)
    sig = max(p_occ - n_in_occ, 0.0)
    out["snr_db"] = (10.0 * np.log10(sig / (n_in_occ + EPS)) if sig > 0 else None)
    out["snr_total_band_db"] = (10.0 * np.log10(sig / (n_all + EPS))
                                if sig > 0 else None)
    out["snr_definition"] = ("snr_db is IN-BAND (noise counted only within "
                             "occupied_bandwidth_hz); snr_total_band_db spans the "
                             "full captured rate")
    if out["snr_db"] is None:
        out["why"] = "occupied-band power did not exceed the noise estimate"
    _ = p_all
    # where the energy sits, useful for spotting a carrier offset or DC spur
    out["centroid_offset_hz"] = float((freqs[occ] * psd[occ]).sum() / (p_occ + EPS))
    return out


def snr_from_noise_reference(x_signal, x_noise):
    """The defensible SNR: one capture with the transmitter ON, one with it OFF, at
    the SAME gain and frequency. SNR = 10log10((P_on - P_off)/P_off). This is the
    method to use for anything that goes in a report -- it assumes nothing about
    where the signal sits in the band."""
    p_on = float((np.abs(x_signal).astype(np.float64) ** 2).mean())
    p_off = float((np.abs(x_noise).astype(np.float64) ** 2).mean())
    sig = p_on - p_off
    return {
        "method": "two-capture (tx on minus tx off)",
        "signal_plus_noise_dbfs": 10.0 * np.log10(p_on + EPS),
        "noise_only_dbfs": 10.0 * np.log10(p_off + EPS),
        "snr_db": (10.0 * np.log10(sig / (p_off + EPS)) if sig > 0 else None),
        "why": (None if sig > 0 else
                "the tx-on capture was not above the tx-off capture — check the "
                "transmitter actually keyed, and that both gains match"),
    }


# ── front-end quality: DC leakage and IQ imbalance ───────────────────────────
def iq_quality_features(x):
    """DC offset and IQ imbalance — front-end health, independent of any signal.

    A large DC term is LO self-mixing (worst on a zero-IF part like the B210's
    AD9361); gain/quadrature imbalance puts an image in the mirror bin and shows up
    as a constellation that is elliptical rather than round."""
    i, q = x.real.astype(np.float64), x.imag.astype(np.float64)
    dc_i, dc_q = float(i.mean()), float(q.mean())
    si, sq = float(i.std()), float(q.std())
    rms = float(np.sqrt((np.abs(x).astype(np.float64) ** 2).mean()))
    # quadrature error from the residual correlation between the two rails
    corr = float(((i - dc_i) * (q - dc_q)).mean())
    denom = si * sq
    phase_err = float(np.degrees(np.arcsin(np.clip(corr / (denom + EPS), -1, 1))))
    return {
        "dc_offset_i": dc_i,
        "dc_offset_q": dc_q,
        "dc_offset_dbc": 10.0 * np.log10((dc_i ** 2 + dc_q ** 2 + EPS) / (rms ** 2 + EPS)),
        "gain_imbalance_db": 20.0 * np.log10((si + EPS) / (sq + EPS)),
        "quadrature_error_deg": phase_err,
    }


# ── sync features: only meaningful on OUR OWN waveform ───────────────────────
def sync_features(x, preamble_m=5, sps=2, scheme="QPSK"):
    """Detection, correlation peak, timing offset and CFO — via the modem's own DSP
    (pyphy), so these are the same numbers the C++ receiver would compute.

    Returns a dict whose values are all None, plus a `why`, when pyphy is not
    importable — a missing optional extension must not look like a measurement of
    zero."""
    out = {"available": False, "detected": None, "peak_correlation": None,
           "timing_offset_samples": None, "cfo_hz": None, "correlation_snr_db": None}
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "bindings"))
        import pyphy
    except Exception as e:
        out["why"] = f"pyphy not importable ({e.__class__.__name__}: {e})"
        return out
    try:
        pre = np.asarray(pyphy.preamble(preamble_m), dtype=np.complex64)
        syms = np.asarray(pyphy.rrc_rx(np.asarray(x, dtype=np.complex64), sps=sps),
                          dtype=np.complex64)
        ndata = max(0, len(syms) - len(pre))
        aligned, detected, peak, tau = pyphy.acq(syms, pre, ndata)
        out.update(available=True, detected=bool(detected),
                   peak_correlation=float(peak), timing_offset_samples=int(tau))
        # the correlator's own noise floor, the way synchronization.hpp derives it:
        # the median correlation magnitude away from the winning offset
        if len(syms) > len(pre) + 2:
            mags = np.abs(np.correlate(syms, pre, mode="valid"))
            if mags.size:
                floor = float(np.median(mags))
                out["correlation_snr_db"] = 20.0 * np.log10((float(peak) + EPS)
                                                            / (floor + EPS))
        if detected and len(aligned):
            _, cfo = pyphy.cfo_correct(np.asarray(aligned, dtype=np.complex64), pre)
            out["cfo_hz"] = float(cfo)
    except Exception as e:
        out["why"] = f"pyphy call failed ({e.__class__.__name__}: {e})"
    return out


def evm_features(x, scheme="QPSK", preamble_m=5, sps=2):
    """RMS EVM (%) against the nearest ideal constellation point, and the SNR that
    EVM implies (SNR ≈ -20log10(EVM)). Needs OUR waveform and a successful ACQ, so
    it reports null with a `why` when the burst was not found."""
    out = {"evm_pct": None, "snr_from_evm_db": None}
    s = sync_features(x, preamble_m=preamble_m, sps=sps, scheme=scheme)
    if not s.get("available"):
        out["why"] = s.get("why", "pyphy unavailable")
        return out
    if not s.get("detected"):
        out["why"] = "no burst detected, so there are no symbols to score"
        return out
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "bindings"))
        import pyphy
        pre = np.asarray(pyphy.preamble(preamble_m), dtype=np.complex64)
        syms = np.asarray(pyphy.rrc_rx(np.asarray(x, dtype=np.complex64), sps=sps),
                          dtype=np.complex64)
        aligned, det, _, _ = pyphy.acq(syms, pre, max(0, len(syms) - len(pre)))
        blk, _ = pyphy.cfo_correct(np.asarray(aligned, dtype=np.complex64), pre)
        blk = np.asarray(pyphy.phase_correct(np.asarray(blk, dtype=np.complex64), pre),
                         dtype=np.complex64)
        data = np.asarray(blk[len(pre):], dtype=np.complex64)
        if not len(data):
            out["why"] = "burst detected but carried no data symbols"
            return out
        # normalise to unit average power, then score against the ideal QPSK points
        data = data / (np.sqrt((np.abs(data) ** 2).mean()) + EPS)
        ideal = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j], dtype=np.complex64)
        ideal = ideal / np.sqrt(2.0)
        err = np.abs(data[:, None] - ideal[None, :]).min(axis=1)
        evm = float(np.sqrt((err ** 2).mean()))
        out["evm_pct"] = evm * 100.0
        out["snr_from_evm_db"] = -20.0 * np.log10(evm + EPS)
        out["symbols_scored"] = int(len(data))
        out["note"] = "QPSK decision-directed EVM; valid only for a QPSK capture"
    except Exception as e:
        out["why"] = f"EVM computation failed ({e.__class__.__name__}: {e})"
    return out


# ── the one call that does all of it ─────────────────────────────────────────
def extract(samples, rate_hz, freq_hz=None, gain_db=None, radio_args=None,
            preamble_m=None, sps=2, scheme="QPSK", noise_samples=None,
            cal_offset_db=None, nfft=4096):
    """Every feature derivable from one capture, as a nested dict.

    `samples` is a complex array. `preamble_m` enables the sync/EVM features (our
    own waveform only). `noise_samples` enables the two-capture SNR. Nothing here
    needs a radio — pass a recorded capture and it all still works, which is what
    makes the numbers re-checkable."""
    x = np.asarray(samples)
    if x.dtype != np.complex64 and x.dtype != np.complex128:
        x = x.astype(np.complex64)
    out = {
        "schema": 1,
        "capture": {
            "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "measured_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "n_samples": int(x.size),
            "rate_hz": float(rate_hz),
            "duration_s": float(x.size / rate_hz) if rate_hz else None,
            "freq_hz": (float(freq_hz) if freq_hz is not None else None),
            "gain_db": (float(gain_db) if gain_db is not None else None),
            "radio_args": radio_args,
        },
        "power": power_features(x, cal_offset_db=cal_offset_db),
        "spectrum": spectrum_features(x, rate_hz, nfft=nfft),
        "iq_quality": iq_quality_features(x),
    }
    if noise_samples is not None:
        out["snr_two_capture"] = snr_from_noise_reference(x, np.asarray(noise_samples))
    if preamble_m is not None:
        out["sync"] = sync_features(x, preamble_m=preamble_m, sps=sps, scheme=scheme)
        out["evm"] = evm_features(x, scheme=scheme, preamble_m=preamble_m, sps=sps)
    else:
        out["sync"] = {"available": False,
                       "why": "no --preamble-m given; sync/EVM need our own waveform"}
    return out


def capture_and_extract(args, freq_hz, rate_hz, n=1 << 20, gain_db=30.0,
                        subdev="A:0", ant="RX2", symbol_rate=1e6, **kw):
    """Capture from a real USRP via pyphy.Radio, then extract. Requires bindings
    built WITH_UHD=1 (pyphy.HAS_RADIO)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "bindings"))
    import pyphy
    if not getattr(pyphy, "HAS_RADIO", False):
        raise SystemExit(
            "pyphy was built without UHD, so it cannot drive a radio.\n"
            "  Rebuild with:  WITH_UHD=1 drivers/usrp/bindings/build.sh\n"
            "  Or capture elsewhere and analyse with --load-iq.")
    r = pyphy.Radio(role="rx", args=args, freq=float(freq_hz), rate=float(rate_hz),
                    symbol_rate=float(symbol_rate), gain=float(gain_db),
                    subdev=subdev, ant=ant)
    try:
        x = np.asarray(r.capture(int(n)), dtype=np.complex64)
    finally:
        try:
            r.close()
        except Exception:
            pass
    return x, extract(x, rate_hz, freq_hz=freq_hz, gain_db=gain_db,
                      radio_args=args, **kw)


# ── self-test: do the estimators recover a known truth? ──────────────────────
def self_test():
    """Synthesise captures whose answers are known, and check the estimators find
    them. Without this the output is just numbers; with it, the SNR estimate has a
    stated accuracy on a case where the truth is not in doubt."""
    rng = np.random.default_rng(0)
    checked = failures = 0

    def check(label, ok, detail=""):
        nonlocal checked, failures
        checked += 1
        if not ok:
            failures += 1
            print(f"  FAIL {label} {detail}")

    rate = 2e6
    n = 1 << 17

    # 1 · power: a known-amplitude tone must read the right dBFS and PAPR
    amp = 0.25
    tone = (amp * np.exp(2j * np.pi * 1e5 * np.arange(n) / rate)).astype(np.complex64)
    p = power_features(tone)
    check("tone mean power", abs(p["mean_dbfs"] - 20 * np.log10(amp)) < 0.1,
          f'got {p["mean_dbfs"]:.2f} want {20*np.log10(amp):.2f}')
    check("tone PAPR ~0 dB", abs(p["papr_db"]) < 0.1, f'got {p["papr_db"]:.3f}')
    check("no dbm without calibration", "mean_dbm" not in p)
    p2 = power_features(tone, cal_offset_db=-30.0)
    check("dbm appears with calibration",
          abs(p2["mean_dbm"] - (p2["mean_dbfs"] - 30.0)) < 1e-9)

    # 2 · the single-capture estimator against a known truth. The signal fills a
    #     QUARTER of the band, so the two reported SNRs must differ by 10log10(4) =
    #     6.02 dB: total-band is what we constructed, in-band is 6 dB higher because
    #     only a quarter of the noise lies under the signal. Asserting both is what
    #     keeps the two definitions from being quietly conflated again.
    for want_total in (5.0, 10.0, 20.0):
        noise = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)) / np.sqrt(2)
        nf = np.fft.fft(rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n))
        keep = np.zeros(n, dtype=bool)
        keep[: n // 8] = True
        keep[-n // 8:] = True          # a quarter of the band, centred on DC
        sig = np.fft.ifft(nf * keep)
        sig = sig / np.sqrt((np.abs(sig) ** 2).mean())
        noise = noise / np.sqrt((np.abs(noise) ** 2).mean())
        x = (sig * (10 ** (want_total / 20.0)) + noise).astype(np.complex64)
        s = spectrum_features(x, rate)
        check(f"total-band SNR @ {want_total:g} dB",
              s["snr_total_band_db"] is not None
              and abs(s["snr_total_band_db"] - want_total) < 2.0,
              f'got {s["snr_total_band_db"]}')
        check(f"in-band SNR is ~6 dB higher @ {want_total:g} dB",
              s["snr_db"] is not None
              and abs(s["snr_db"] - (want_total + 6.02)) < 2.0,
              f'got {s["snr_db"]} want ~{want_total + 6.02:.2f}')
        check(f"occupied bandwidth ~quarter of rate @ {want_total:g} dB",
              abs(s["occupied_fraction"] - 0.25) < 0.08,
              f'got {s["occupied_fraction"]:.3f}')

    # 3 · the two-capture SNR must be tighter than the single-capture one
    for want_snr in (3.0, 15.0):
        noise = ((rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)) / np.sqrt(2)
                 ).astype(np.complex64)
        sig = ((rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)) / np.sqrt(2)
               ).astype(np.complex64) * (10 ** (want_snr / 20.0))
        r = snr_from_noise_reference(sig + noise, noise)
        check(f"two-capture SNR @ {want_snr:g} dB",
              r["snr_db"] is not None and abs(r["snr_db"] - want_snr) < 0.5,
              f'got {r["snr_db"]}')

    # 4 · an idle capture reports no SNR, with a reason — not a number
    idle = ((rng.normal(0, 1e-3, n) + 1j * rng.normal(0, 1e-3, n))).astype(np.complex64)
    s = spectrum_features(idle, rate)
    check("idle capture yields no SNR", s["snr_db"] is None)
    check("idle capture explains itself", bool(s.get("why")))

    # 5 · THE REGRESSION THAT MATTERS: pure full-scale noise must report NO SNR.
    #     A noise PSD has a long tail, so at the old 6 dB threshold ~6% of bins read
    #     as "occupied" and the estimator returned a confident negative SNR for an
    #     idle channel — a fabricated measurement, the worst kind of wrong.
    full = ((rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n))).astype(np.complex64)
    s = spectrum_features(full, rate)
    check("pure noise reports no SNR", s["snr_db"] is None, f'got {s["snr_db"]}')
    check("pure noise reports no total-band SNR either",
          s["snr_total_band_db"] is None, f'got {s["snr_total_band_db"]}')
    check("pure noise says it reads as idle", "idle" in (s.get("why") or ""),
          f'why={s.get("why")!r}')
    # and a band-filling signal refuses for the OTHER documented reason
    s = spectrum_features(full, rate, occupancy_db=0.0001, min_occupancy=0.0)
    check("band-filling capture refuses an SNR", s["snr_db"] is None,
          f'got {s["snr_db"]}')
    check("band-filling refusal cites bandwidth",
          "bandwidth" in (s.get("why") or ""), f'why={s.get("why")!r}')

    # 6 · IQ quality: a deliberately imbalanced, DC-offset signal is detected
    bad = (tone + (0.1 + 0.05j)).astype(np.complex64)
    q = iq_quality_features(bad)
    check("DC offset found", abs(q["dc_offset_i"] - 0.1) < 0.01, f'got {q["dc_offset_i"]:.4f}')
    skew = (tone.real + 1j * tone.imag * 0.5).astype(np.complex64)
    q = iq_quality_features(skew)
    check("gain imbalance found", abs(q["gain_imbalance_db"] - 6.02) < 0.3,
          f'got {q["gain_imbalance_db"]:.2f}')

    # 7 · extract() composes, and is honest about what it could not do
    f = extract(tone, rate, freq_hz=915e6, gain_db=30.0, radio_args="serial=TEST")
    for k in ("capture", "power", "spectrum", "iq_quality", "sync"):
        check(f"extract has {k}", k in f)
    check("sync absent without a preamble", f["sync"]["available"] is False)
    check("sync says why", bool(f["sync"].get("why")))
    check("capture metadata carried", f["capture"]["freq_hz"] == 915e6
          and f["capture"]["n_samples"] == n)
    check("duration computed", abs(f["capture"]["duration_s"] - n / rate) < 1e-9)
    check("round-trips as JSON", isinstance(json.dumps(f), str))

    if failures:
        print(f"  {failures} of {checked} phy-feature checks FAILED")
        return 1
    print(f"phy_features self-test: {checked} checks passed — estimators recover "
          f"known SNR within 2 dB (single-capture) and 0.5 dB (two-capture)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Extract every measurable PHY feature from one IQ capture.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--args", help="UHD device args, e.g. serial=30CD3F7 or addr=192.168.40.2")
    ap.add_argument("--freq", type=float, help="centre frequency in Hz")
    ap.add_argument("--rate", type=float, default=2e6, help="sample rate in Hz (default 2e6)")
    ap.add_argument("--gain", type=float, default=30.0, help="RX gain in dB (default 30)")
    ap.add_argument("--subdev", default="A:0", help="RF channel (B210: A:A/A:B)")
    ap.add_argument("--ant", default="RX2", help="antenna port (default RX2)")
    ap.add_argument("-n", "--n", type=float, default=float(1 << 20),
                    help="samples to capture (default 1048576)")
    ap.add_argument("--load-iq", metavar="FILE.npy",
                    help="analyse a saved capture instead of using a radio")
    ap.add_argument("--save-iq", metavar="FILE.npy",
                    help="save the raw IQ beside the features, so they can be re-checked")
    ap.add_argument("--noise-iq", metavar="FILE.npy",
                    help="a transmitter-OFF capture at the same gain, for the "
                         "two-capture SNR (the defensible one)")
    ap.add_argument("--preamble-m", type=int, default=None,
                    help="enable sync/EVM features; our m-sequence preamble order "
                         "(the modem's default is 5)")
    ap.add_argument("--sps", type=int, default=2, help="samples per symbol (default 2)")
    ap.add_argument("--cal-offset-db", type=float, default=None,
                    help="dBFS->dBm offset, if THIS chain has been calibrated")
    ap.add_argument("--nfft", type=int, default=4096, help="PSD FFT size (default 4096)")
    ap.add_argument("--json", metavar="FILE", help="write the features as JSON")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    noise = np.load(a.noise_iq) if a.noise_iq else None

    if a.load_iq:
        x = np.load(a.load_iq)
        f = extract(x, a.rate, freq_hz=a.freq, gain_db=a.gain, radio_args=a.args,
                    preamble_m=a.preamble_m, sps=a.sps, noise_samples=noise,
                    cal_offset_db=a.cal_offset_db, nfft=a.nfft)
        f["capture"]["source"] = a.load_iq
    else:
        if not a.args or a.freq is None:
            ap.error("--args and --freq are required to capture "
                     "(or pass --load-iq to analyse a saved file)")
        x, f = capture_and_extract(
            a.args, a.freq, a.rate, n=int(a.n), gain_db=a.gain, subdev=a.subdev,
            ant=a.ant, preamble_m=a.preamble_m, sps=a.sps, noise_samples=noise,
            cal_offset_db=a.cal_offset_db, nfft=a.nfft)

    if a.save_iq:
        np.save(a.save_iq, x)
        f["capture"]["iq_file"] = a.save_iq
        print(f"[phy-features] raw IQ saved: {a.save_iq} ({x.size} samples)")

    print(json.dumps(f, indent=2))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(f, fh, indent=2)
            fh.write("\n")
        print(f"[phy-features] features saved: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
