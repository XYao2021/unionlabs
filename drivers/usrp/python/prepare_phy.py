#!/usr/bin/env python3
"""
prepare_phy.py — measure a testbed's RF environment ONCE and derive the PHY
parameters an experiment there should use. The goal is hands-free: a user on a
new testbed runs this, and the platform knows the usable band, the carrier,
the noise floor, and the detector thresholds — instead of the user tuning
--det-mult and --sync-threshold by folklore.

    python3 prepare_phy.py --device x310 --args addr=192.168.40.2 --band vert2450-5g
    python3 prepare_phy.py --device n210 --args addr=192.168.10.2   # ism915
    python3 prepare_phy.py ... --node siteB --write                 # publish profile
    python3 prepare_phy.py --dry-run

Four measurements, all receive-only:

  1. SURVEY the band the antenna serves (freq_survey's sweep) and find the
     widest contiguous quiet region -> the USABLE BAND, and its center ->
     the CARRIER (plateau center beats the literal minimum: the minimum is
     measurement noise, the center has margin on both sides).
  2. DWELL at that carrier (many sense windows) -> the NOISE FLOOR and its
     burstiness -> a DET-MULT with the measured headroom, clamped to the
     modem's own guidance (5..30).
  3. LISTEN with the detector forced open for a few seconds -> the noise's
     ACQ correlation distribution -> a SYNC-THRESHOLD between the noise peaks
     and a real preamble's peak (~the preamble length, 31).
  4. The quiet region's WIDTH -> the usable bandwidth -> whether the default
     2 MS/s link fits (it needs ~2 MHz + guard).

Between 2 and 3 it also reads the RECEIVER's own calibrated floor and reports
it against the survey's. Both measure 10*log10(mean|x|^2), but the detector
prints its floor linear while channel_sense prints dB, so the two have looked
incomparable -- a survey floor of -31 dB next to an apparent detect threshold
near -50 dB reads like a contradiction when it is mostly a difference in RX
gain. Measured here at one --gain, the remainder is a number in the profile
(sense_minus_detector_db). det-mult is a RATIO on the detector's own floor and
so is immune to the offset; an absolute --energy_threshold is not, and must be
quoted on the detector's scale.

--write publishes the profile to
searching/phy-<key>-<band>-<subdev>-<ant>-<local-time>.json (the same shared
folder the node records live in) -- the local wall-clock time is in the name so
`ls` shows when each radio was surveyed; a re-survey of the same path supersedes
the prior file. Every run prints the ready-to-paste run.sh / radio.sh flags and
the topology "defaults" snippet.
"""
import argparse
import glob
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

from channel_sense import _run_sense
from freq_survey import BANDS, survey as band_survey

PREAMBLE_PEAK = 31.0            # a real preamble's ACQ correlation after AGC
DEFAULT_LINK_BW_MHZ = 2.4      # 2 MS/s + RRC roll-off + CFO guard


# ── 1 · the usable bands: EVERY contiguous quiet region of the sweep ─────────
def quiet_regions(rows, margin_db=6.0):
    """-> (sweep_floor_db, [region, ...]) ranked best-first.

    Quiet = within margin of the sweep's lower-quartile floor. Every contiguous
    run is kept, not just the winner: a testbed usually has several usable
    stretches, and which one is *best* depends on things this measurement cannot
    see -- a neighbouring experiment, a regulatory limit, an antenna that is
    only nominally in band. Recording all of them lets a topology choose a
    different one per node without re-measuring the site.

    Ranked by width first (bandwidth is the scarce resource), then by how quiet
    the region actually is."""
    if not rows:
        sys.exit("[prepare] survey produced nothing — is the radio reachable?")
    p = sorted(r["power_db"] for r in rows)
    floor = p[len(p) // 4]
    quiet = [r["power_db"] <= floor + margin_db for r in rows]

    runs, cur = [], None
    for i, q in enumerate(quiet + [False]):
        if q and cur is None:
            cur = i
        elif not q and cur is not None:
            runs.append((cur, i - 1))
            cur = None

    regions = []
    for a, b in runs:
        lo, hi = rows[a]["freq_mhz"], rows[b]["freq_mhz"]
        band = [r["power_db"] for r in rows[a:b + 1]]
        regions.append({
            "usable_mhz": [lo, hi],
            "width_mhz": round(hi - lo, 3),
            "carrier_mhz": round((lo + hi) / 2.0, 3),   # plateau centre, not the
                                                        # literal minimum: the
                                                        # minimum is measurement
                                                        # noise, the centre has
                                                        # margin on both sides
            "region_floor_db": round(sum(band) / len(band), 1),
        })
    regions.sort(key=lambda r: (-r["width_mhz"], r["region_floor_db"]))
    return floor, regions


def quiet_region(rows, margin_db=6.0):
    """The single best region, as (lo, hi, sweep_floor_db). Kept so existing
    callers and the older profile schema keep working."""
    floor, regions = quiet_regions(rows, margin_db)
    if not regions:
        sys.exit("[prepare] no quiet region found — the whole band is occupied?")
    lo, hi = regions[0]["usable_mhz"]
    return lo, hi, floor


# ── 2 · floor + det-mult from a dwell at the carrier ─────────────────────────
def dwell(freq_mhz, windows, window_ms, radio):
    rows = _run_sense(window_ms, windows, -999.0, rx_freq=freq_mhz * 1e6, **radio)
    powers = sorted(r["power_db"] for r in rows)
    med = powers[len(powers) // 2]
    p99 = powers[min(len(powers) - 1, int(len(powers) * 0.99))]
    # threshold must clear the p99 burstiness with 3 dB to spare; the modem's
    # own guidance bounds it (5 = default, 10-30 = over-the-air)
    mult = 10 ** ((p99 - med + 3.0) / 10.0)
    det_mult = round(min(30.0, max(5.0, mult)), 1)
    return med, p99, det_mult


# ── 3 · sync threshold from the noise's own ACQ correlations ─────────────────
_ACQ = re.compile(r"\[ACQ\]\s+Peak correlation = ([0-9.]+)")
# The detector reports its own floor in LINEAR units (only its threshold gets a
# dB line), so it has to be converted before it can be compared with anything.
_DET_FLOOR = re.compile(r"\[DETECTOR CALIBRATION\][^\n]*\n\s*Noise floor:\s*([0-9.eE+-]+)")
_DET_THR_DB = re.compile(r"Threshold \(dB\):\s*(-?[0-9.]+)")

def detector_floor(freq_mhz, seconds, radio, binary=None):
    """The floor the RECEIVER measures for itself, in dB.

    The survey and the dwell both use channel_sense, which reports
    10*log10(mean|x|^2). The detector computes exactly the same quantity
    (calculate_window_energy averages |x|^2 over its window) but prints the
    result LINEAR, so the two were never directly comparable by eye -- which is
    how a survey floor of -31 dB and an apparent RX detect threshold near
    -50 dB came to look like a contradiction.

    They are the same scale, but only at the same RX GAIN: gain shifts the
    floor by a constant number of dB. Measuring both here at --gain makes the
    remaining difference a real, reportable number instead of a suspicion.

    Returns (floor_db, threshold_db) or None if the receiver printed no
    calibration block.
    """
    import sdr
    cmd = sdr.SDR(role="rx", rx_freq=freq_mhz * 1e6, viz=False,
                  binary=binary, skip_rate_check=1, **radio).command()
    try:
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                           timeout=seconds)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) \
              else (e.stdout or "")
    m = _DET_FLOOR.search(out)
    if not m:
        return None
    lin = float(m.group(1))
    floor_db = 10.0 * math.log10(lin + 1e-20)
    t = _DET_THR_DB.search(out)
    return floor_db, (float(t.group(1)) if t else None)


def noise_acq(freq_mhz, seconds, radio, binary=None):
    """Force the energy detector open (det-mult ~1) so noise triggers ACQ, and
    read the correlation peaks noise achieves. A threshold halfway (in dB
    terms) between that and a real preamble's ~31 rejects noise without
    risking real bursts. Returns (noise_p95, sync_threshold) or None if the
    RX printed no ACQ lines (a very quiet band may never trigger)."""
    import sdr
    cmd = sdr.SDR(role="rx", rx_freq=freq_mhz * 1e6, det_mult=1.05,
                  viz=False, binary=binary, skip_rate_check=1, **radio).command()
    try:
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                           timeout=seconds)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) \
              else (e.stdout or "")
    peaks = sorted(float(m.group(1)) for m in _ACQ.finditer(out))
    if not peaks:
        return None
    p95 = peaks[min(len(peaks) - 1, int(len(peaks) * 0.95))]
    thr = round(min(PREAMBLE_PEAK * 0.8, (p95 * PREAMBLE_PEAK) ** 0.5), 1)
    thr = max(thr, round(p95 * 1.3, 1))          # never inside the noise cloud
    return p95, thr


# ── device discovery: the devices are automatic, the antenna is not ─────────
def find_devices():
    """Every USRP visible now, as [{type, product?, serial?, addr?}] — the same
    text-parse discover-node.py uses (uhd_find_devices has no machine mode)."""
    try:
        out = subprocess.run(["uhd_find_devices"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return []
    return parse_find_devices(out)


def parse_find_devices(out):
    radios, cur = [], None
    for line in out.splitlines():
        if line.startswith("-- UHD Device"):
            cur = {}
            radios.append(cur)
            continue
        m = re.match(r"\s+(serial|addr|type|name|product|resource):\s*(.*)$", line)
        if m and cur is not None and m.group(2).strip():
            cur[m.group(1)] = m.group(2).strip()
    return [r for r in radios if r]


def classify(dev):
    """-> (device_class, uhd_args, identity) for one discovered radio."""
    t = (dev.get("type") or "").lower()
    prod = (dev.get("product") or "").lower()
    if t == "b200" or "b21" in prod or "b20" in prod:
        ident = dev.get("serial", "")
        return "b210", f"serial={ident}", ident
    if t == "x300" or "x31" in prod or "x30" in prod:
        ident = dev.get("addr", "")
        return "x310", f"addr={ident}", ident
    # usrp2 family (N200/N210) and anything else network-addressed
    ident = dev.get("addr") or dev.get("serial", "")
    return "n210", (f"addr={ident}" if dev.get("addr") else f"serial={ident}"), ident


def band_for(ident, band_map, default_band):
    """The antenna is the one fact discovery cannot give: no radio can report
    what is screwed onto its connector. --band-map carries that knowledge."""
    for key, band in band_map.items():
        if key and key in ident:
            return band, True
    return default_band, False


def survey_timestamps(epoch=None):
    """(measured_utc, measured_local, filename_stamp) for a survey, all from one
    instant so they never disagree.

    The filename and the local string are LOCAL time (honouring $TZ), so the name
    reads as the wall clock the operator sees -- inside a container that means
    `export TZ=America/New_York` (or their zone) once, else it shows the
    container's clock, which is UTC. measured_utc stays ISO-UTC, because the
    resolver sorts surveys by it and a machine must not depend on a local zone.

        filename:  2026-09-02_23-45-00     (dashes: no colon/slash/space)
        local:     2026-09-02 23:45:00 EDT
        utc:       2026-09-03T03:45:00Z
    """
    epoch = time.time() if epoch is None else epoch
    utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    local = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(epoch)).strip()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(epoch))
    return utc, local, stamp


def publish_profile(profile, d, node, band, subdev, ant, stamp):
    """Write a survey to searching/ and supersede earlier surveys of the SAME
    signal path. Returns (path_written, [paths_removed]).

    Factored out of main() precisely so it can be exercised without a radio: the
    write path runs only on real hardware, so a bug here (a missing import, say)
    slips past every hardware-free test and first appears in a session at the end
    of a several-minute survey. test_prepare_publish.py now walks it.

    The filename carries the survey time so `ls` shows when each radio was last
    measured; the folder still keeps ONE profile per path (the current one), so
    the timestamped write removes prior surveys of this radio/band/subdev/ant,
    the old un-stamped name included.
    """
    os.makedirs(d, exist_ok=True)

    def _tag(x):
        return re.sub(r"[^A-Za-z0-9]+", "", str(x)) or "x"

    base = f"phy-{node}-{band}-{_tag(subdev)}-{_tag(ant)}"
    path = os.path.join(d, f"{base}-{stamp}.json")
    superseded = [q for q in
                  glob.glob(os.path.join(d, f"{base}-*.json"))
                  + [os.path.join(d, f"{base}.json")]
                  if os.path.exists(q) and q != path]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(profile, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())         # a network share can lose a buffered write
    os.replace(tmp, path)
    removed = []
    for q in superseded:
        try:
            os.remove(q)
            removed.append(q)
        except OSError:
            pass
    return path, removed


# Per-device TRANSMIT defaults, matching radio.sh's own table. The survey measures a
# receiver and so knows nothing about transmitting; these are the wrapper's defaults for
# the device, not measurements, and the draft says so.
# A B210 transmits over ~0-89 dB, and 78 is near the top of it: on one bench, with
# the two radios a short cable or a metre apart, that arrives far above what the
# receiver can take and the burst is lost to front-end compression rather than to
# distance. It looks exactly like a link that is too WEAK -- the source transmits
# "complete" and nothing is ever acknowledged -- so it sends you looking for more
# power. 50 is what this rig's working pair actually used. Raise it for range; the
# note beside the field says which way and why.
# 75 for the B210, measured on this rig: 85 saturated the receiver and 50 did not
# reach it, which is a narrow window and worth recording rather than rediscovering.
_TX_DEFAULTS = {"b210": ("A:A", 75), "n210": ("A:0", 25), "x310": ("A:0", 25)}


def topology_dir():
    """Where topology files live, in the same order union/topology.py searches.

    Resolved here rather than by importing union/topology.py: drivers/ is the layer
    below union/ and must not depend upward on it. The cost of the duplication is this
    list going stale, so it is deliberately the whole list and in the same order --
    env override, the shared workspace, then this checkout.
    """
    # python/ -> usrp/ -> drivers/ -> the repo: FOUR levels. Three lands on drivers/,
    # which exists nowhere, so the checkout fallback silently never applied and the
    # makedirs below took over -- a bug the OSError fallback hid rather than surfaced.
    repo = os.path.abspath(__file__)
    for _ in range(4):
        repo = os.path.dirname(repo)
    cands = [os.environ.get("UNION_TOPOLOGY_DIR"),
             "/workspace/experiments/topologies",
             os.path.join(repo, "deploy", "workspace", "topologies")]
    cands = [c for c in cands if c]
    for c in cands:
        if os.path.isdir(c):
            return c
    os.makedirs(cands[0], exist_ok=True)        # nothing exists yet: make the first
    return cands[0]


def newest_profile(d, args=None):
    """The most recent saved survey in d, optionally for one radio. -> (profile, stamp)

    A survey costs minutes and a radio, and the draft written from it is only a
    convenience -- so regenerating the draft must never require sweeping the band
    again. This reads what is already on disk.
    """
    best = None
    for path in sorted(glob.glob(os.path.join(d, "phy-*.json"))):
        try:
            with open(path) as fh:
                prof = json.load(fh)
        except Exception:
            continue
        if args and (prof.get("radio") or {}).get("args") != args:
            continue
        key = prof.get("measured_utc") or os.path.getmtime(path)
        if best is None or str(key) > str(best[0]):
            best = (key, prof, path)
    if best is None:
        raise FileNotFoundError(
            f"no saved survey in {d}" + (f" for {args}" if args else "") +
            " — run ./prepare.sh --band <band> first")
    _, prof, path = best
    # the stamp the survey was filed under, so the draft is traceable to it rather
    # than to the moment someone happened to regenerate it
    m = re.search(r"-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.json$", path)
    if m:
        return prof, m.group(1)
    local = (prof.get("measured_local") or "").strip()
    return prof, (local.replace(" ", "_").replace(":", "-") or
                  time.strftime("%Y-%m-%d_%H-%M-%S"))


def resolve_serial(args, timeout=20):
    """-> the serial of the radio reachable at `args`, or None.

    A topology is read on every box that shares /workspace, and an ADDRESS only
    identifies a radio within one host: 192.168.40.2 is UHD's default for an X310, so
    two machines each with one answer to it and both containers claim the same node.
    run_topology already warns about exactly that. A serial is burned into the hardware
    and means the same thing everywhere, so a draft should carry one even when the
    survey was addressed by IP -- and UHD will tell us, since the radio is attached to
    the box running the survey.

    None when nothing answers, which is the ordinary case for --topology-only run on a
    box that no longer has the radio: the draft then keeps the address it was surveyed
    with rather than inventing anything.
    """
    try:
        p = subprocess.run(["uhd_find_devices", "--args", args],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    for ln in (p.stdout + p.stderr).splitlines():
        if ln.strip().startswith("serial:"):
            got = ln.split(":", 1)[1].strip()
            if got:
                return got
    return None


def publish_topology_draft(profile, d, stamp, force=False):
    """Write a ready-to-edit TOPOLOGY for the pair this radio is the RECEIVER of.

    A survey ends holding every number a topology needs but one. It knows the device,
    the radio's identity, the signal path it listened on, the gain it listened at, the
    carriers it measured as usable, and the detector values it derived -- and because
    prepare_phy is receive-only, it knows this radio is the RECEIVING end of whatever
    link it joins. The single thing it cannot know is the far end's identity, which is
    on another box.

    So the draft is written with the receiver complete and the transmitter a
    placeholder, which is the smallest thing a person still has to supply. Authoring
    the rest by hand meant copying carriers out of a survey into a file -- the exact
    transcription this project removed from the carrier itself by making freq_mhz a
    candidate list, and which was still being done for every other field.

    Written into topologies/, where every other topology lives, so it is found by name
    (`./run.sh --topology draft-<serial>`) and by `./run.sh topologies` rather than
    having to be known about. A draft sitting among runnable files would be a trap, so
    two things carry it: the lister marks it DRAFT and names the fields still to fill,
    and a run REFUSES it until they are filled. `d` is kept in the signature because
    the caller passes the survey directory for every other output; it is used only as
    the fallback when no topologies directory can be resolved.
    """
    os.makedirs(d, exist_ok=True)
    radio = profile.get("radio") or {}
    args = radio.get("args") or ""
    device = (radio.get("device") or "").lower()
    # serial= if there is one: an address identifies a radio only within one host, and
    # a shared /workspace means the file is read on boxes where it means someone else's
    m = re.search(r"serial=([^,\s]+)", args) or re.search(r"addr=([^,\s]+)", args)
    id_key = "serial" if "serial=" in args else "addr"
    id_val = m.group(1) if m else "REPLACE_ME_SINK_ID"
    id_note = ""
    if id_key == "addr":
        # ask the radio what it is called, so the file does not depend on an address
        # that means something different on the next box
        got = resolve_serial(args)
        if got:
            id_key, id_note = "serial", f" (resolved from {id_val})"
            id_val = got

    # Preference order: the recommended carrier first, then the rest. The resolver
    # takes the first candidate it finds inside a measured window, so this makes the
    # survey's own pick the default without pinning it.
    opts = profile.get("options") or []
    use = int(profile.get("use", 0) or 0)
    order = ([opts[use]] + [o for i, o in enumerate(opts) if i != use]) \
        if 0 <= use < len(opts) else list(opts)
    cands = [o["carrier_mhz"] for o in order if o.get("carrier_mhz") is not None]
    if not cands:
        raise ValueError("the survey saved no usable carrier, so there is nothing "
                         "for a topology to choose between")

    # The DATA carrier and the ACK carrier are chosen by a person, from the windows
    # the survey measured. A candidate list would let the survey choose, and that is
    # offered in the header -- but a named blank is the default here because the two
    # carriers must DIFFER for a wireless ACK, and nothing in a survey knows which of
    # its windows someone intends for which direction.
    opt_list = " | ".join(f"{c:g}" for c in cands)
    # THE SURVEY ALREADY CHOSE. It swept the band, found the usable regions and ranked
    # them, so leaving its own recommendation as a blank asked someone to retype a
    # number the file was holding -- which is the transcription this whole mechanism
    # exists to remove. Filled in, with the alternatives beside it so changing it is
    # one edit.
    data_freq = cands[0]
    # the reply needs a DIFFERENT carrier, so the runner-up. With only one usable
    # region there is no second to offer, and a blank is honest: nothing measured says
    # where else to put it.
    ack_freq = cands[1] if len(cands) > 1 else "REPLACE_ME_WITH_ACK_FREQ_OPTION"
    data_hint = (f"// the survey's pick. Others measured usable: {opt_list}"
                 f"   (or [{', '.join(f'{c:g}' for c in cands)}] to let it re-choose)")
    ack_hint = (f"// must DIFFER from the data carrier. Others: {opt_list}"
                if len(cands) > 1 else
                f"// only one usable region was found ({opt_list}), so there is no "
                f"second carrier to offer — survey a wider band, or use a TCP ACK")

    tx_subdev, tx_gain = _TX_DEFAULTS.get(device, ("A:0", 25))
    # THE SECOND RF PATH IS THE SECOND ANTENNA PORT, not a second daughterboard.
    # One X310 daughterboard is full duplex and brings out two connectors -- TX/RX to
    # transmit, RX2 to receive -- which is the arrangement the working commands on this
    # rig already used. Defaulting to B:0 asked for a board that is usually not fitted
    # and, on a radio with antennas only on the first port, cannot work at all.
    # A B210 is different: its two channels A:A and A:B are the natural split.
    rx_ant, rx_subdev = radio.get("ant"), radio.get("subdev")
    rx_gain = radio.get("gain_db")
    # The reply path should use the channel the DATA is not on -- and which that is
    # differs per node, because the data leaves the source on its tx channel and
    # arrives at the sink on its rx channel. One constant got the sink wrong whenever
    # the survey used A:B: it put the ACK transmitter on the same channel AND the same
    # connector the data arrives on, which cannot work and reads as "the ACK never
    # fires". An X310 has one board, so there its two CONNECTORS are the two paths and
    # the subdev stays the same.
    def _other_channel(sub):
        if device != "b210":
            return sub or "A:0"
        return "A:A" if str(sub or "A:A").strip().upper().endswith(":B") else "A:B"

    src_ack_subdev = _other_channel(tx_subdev)      # the source LISTENS for the ACK
    snk_ack_subdev = _other_channel(rx_subdev)      # the sink SENDS it

    # ── the header: what the survey found, above the file rather than inside it ──
    # The explanation used to live in `note` fields holding paragraphs, which is what
    # made the file read as generated output rather than as a topology. Comments keep
    # the body clean and put the carriers where someone opening the file looks first.
    W = 74
    H = ["// " + "-" * (W - 3),
         f"//  draft-{id_val}-{stamp}",
         f"//  Written by prepare.sh from the survey of {device} "
         f"{id_key}={id_val}{id_note}",
         f"//  on {rx_subdev} / {rx_ant}, band {radio.get('band')}, "
         f"measured {stamp}.",
         "//",
         "//  CARRIERS THE SURVEY MEASURED AS USABLE   (best first)"]
    for o in order:
        c = o.get("carrier_mhz")
        if c is None:
            continue
        bw = o.get("band_mhz") or []
        where = f"clear {bw[0]:g}-{bw[1]:g} MHz" if len(bw) == 2 else "clear"
        wide = o.get("width_mhz")
        H.append(f"//      {c:>9.5g} MHz   {where}"
                 + (f"   ({wide:g} MHz wide)" if wide else ""))
    H += ["//",
          "//  ONE THING TO FILL IN: the far radio's serial, on the src node. Everything",
          "//  else is from the survey -- this radio's identity, its connector, its",
          "//  subdev, the gain it listened at, and the carriers it measured. A run",
          "//  refuses the file until that serial is real, and names it.",
          "//",
          "//    ack_wireless   false -> the reply comes back over TCP, and snk.host",
          "//                            is the address the source dials.",
          "//                   true  -> the ARQ ACK comes back over the air on the",
          "//                            node's OTHER antenna port: it transmits on",
          "//                            TX/RX and listens on RX2, same subdev, which",
          "//                            is what one full-duplex daughterboard gives",
          "//                            you. The two carriers must differ.",
          "//    freq_mhz       already set to what the survey picked, the same on",
          "//                   both nodes. Change it to any carrier listed above, or",
          "//                   to a LIST of them to let the survey re-choose on every",
          "//                   run, which keeps the file from going stale.",
          "//    the reply       carrier is the runner-up, so it differs from the data",
          "//                   one as a wireless ACK requires. Only read when",
          "//                   ack_wireless is true.",
          "//    serial         THE ONE BLANK: the far radio, from uhd_find_devices on",
          "//                   its box. Radios are named by SERIAL throughout, not by",
          "//                   address -- 192.168.40.2 is UHD's default for an X310,",
          "//                   so two machines answer to it and both containers would",
          "//                   claim the same node. A serial means the same thing on",
          "//                   every box that reads this file.",
          "//",
          "//  THE ACK ADDRESS IS snk.host, and it is written out below rather than",
          "//  left to a default -- a field you cannot see is a field you do not know",
          "//  you can change. 127.0.0.1 is right while both radios are on one machine.",
          "//  On two machines put each node's OWN address in its host: snk.host is",
          "//  what src dials, and src needs one too or the reply has no route home.",
          "//  For a single run without editing anything:",
          "//    ./run.sh radio --topology <this> --node src --ack-host 10.0.0.40",
          "//  Anything typed replaces what this file says, for any field, not just",
          "//  this one.",
          "//",
          "//  THE APPLICATION REPLY IS ALWAYS TCP. ack_wireless moves the ARQ ACK, not",
          "//  the algorithm's answer: the request goes over the air and the reply comes",
          "//  back on a socket (port 5700 unless ports.net says otherwise). So two",
          "//  machines need an IP route between them even with ack_wireless true.",
          "//",
          "//  YOU ONLY HAVE TO FILL YOUR OWN END. Each node is checked against what",
          "//  THAT node reads, so --node snk never asks for the source's serial: that",
          "//  radio is on another machine and its own container fills it in. Start the",
          "//  receiver, watch it listen, then go and start the transmitter.",
          "//",
          "//  GAINS are per node and per direction: tx.gain drives the transmitter,",
          "//  rx.gain the receiver. Only rx.gain came from the survey; every tx.gain",
          "//  is radio.sh's default for this device, because a receive-only survey",
          "//  sees nothing about transmitting. Check them against the rig.",
          "//",
          "//  scheme, waveform, fec, bytes_length, det_mult and sync_threshold sit",
          "//  in defaults, so none of them has to be repeated on the command line.",
          "//  Each one carries a note saying what it does and which way to move it.",
          "//",
          "//  THE TWO THAT ACTUALLY NEED ITERATING, in this order:",
          "//    det_mult        the energy gate that decides a burst is present at",
          "//                    all. Too low and ambient RF keeps waking the",
          "//                    receiver; too high and it sleeps through real",
          "//                    bursts. Symptom of too high: nothing at all.",
          "//    sync_threshold  the correlation gate that decides a preamble was",
          "//                    found. Too low and noise passes as a preamble, which",
          "//                    decodes to garbage rather than to silence; too high",
          "//                    and it never locks. Symptom of too low: output that",
          "//                    looks like random bytes.",
          "//  Tune det_mult first: until the detector fires, the correlator never",
          "//  sees a burst to judge, so sync_threshold cannot be read.",
          "//  quiet_phy is true so the per-block chatter does not bury them: [ACQ]",
          "//  and every other diagnosis line still prints, which is what tuning",
          "//  reads. Set it false when you want the whole pipeline trace.",
          "// " + "-" * (W - 3)]
    if profile.get("sync_threshold_measured") is False:
        # The one number in defaults that is NOT a measurement. Worth saying where it
        # is written, because a threshold below the noise correlation makes the
        # receiver lock onto noise and decode garbage -- which looks like a broken
        # link, not like a setting.
        H[-1:-1] = [
            "//  sync_threshold is a PLACEHOLDER, not a measurement: nothing",
            "//  triggered the detector during a receive-only survey. Watch the",
            "//  [ACQ] Peak correlation lines on a real run and set it below the",
            "//  true peak but above the noise. Too low and the receiver locks",
            "//  onto noise and decodes garbage.",
            "//"]

    body = {
        "schema": 1,
        "name": f"draft-{id_val}-{stamp}",
        "algo": "echo",
        "description": (f"{device} pair from the {stamp} survey of {id_val}. "
                        f"Set ack_wireless to choose how the reply comes back."),
        "defaults": {
            "channel": "usrp",
            "scheme": "QPSK",
            "waveform": "sc",
            "fec": "turbo",
            "steps": 10,
            "max_attempts": 50,
            "bytes_length": 1000,
            "quiet_phy": True,
            "det_mult": profile.get("det_mult", 30),
            "sync_threshold": profile.get("sync_threshold", 15),
            "ack_wireless": False,
        },
        "nodes": [
            {"id": "src", "role": "tx", "host": "127.0.0.1",
             "radio": {
                 "device": device, "serial": "REPLACE_ME_SOURCE_ID",
                 "tx": {"ant": "TX/RX", "subdev": tx_subdev, "gain": tx_gain,
                        "freq_mhz": data_freq},
                 "rx": {"ant": "RX2", "subdev": src_ack_subdev, "gain": rx_gain,
                        "freq_mhz": ack_freq}}},
            {"id": "snk", "role": "rx", "host": "127.0.0.1",
             "ports": {"ack": 5599},
             "radio": {
                 "device": device, id_key: id_val,
                 "rx": {"ant": rx_ant, "subdev": rx_subdev, "gain": rx_gain,
                        "freq_mhz": data_freq},
                 "tx": {"ant": "TX/RX", "subdev": snk_ack_subdev, "gain": tx_gain,
                        "freq_mhz": ack_freq}}},
        ],
        "links": [
            # only the DATA direction is stated; ack_wireless above decides the reply,
            # so flipping that switch is not a contradiction with anything written here
            {"from": "src", "to": "snk", "medium": {"up": "wireless"}},
        ],
    }
    try:
        out = topology_dir()
    except OSError:
        out = d                                 # unwritable: beside the survey is fine
    # .jsonc because the file carries a // header: an editor decides whether comments
    # are legal by extension, and VS Code marks every one of them in a .json file as an
    # error. topology.py searches both, so --topology draft-<id>-<stamp> still resolves.
    path = os.path.join(out, f"draft-{id_val}-{stamp}.jsonc")
    # NEVER CLOBBER. This file exists to be edited -- carriers chosen, ack_wireless
    # flipped, det_mult and sync_threshold tuned against the rig -- and --topology-only
    # names it by the SURVEY's stamp, so regenerating lands on exactly the file someone
    # has been working in. A survey can be repeated in minutes; a tuned link cannot.
    if os.path.exists(path) and not force:
        raise FileExistsError(
            f"{path} already exists and is probably the file you have been editing "
            f"(carriers, ack_wireless, det_mult, sync_threshold). Regenerating would "
            f"put every default back. Pass --force to overwrite it deliberately, or "
            f"move it aside first.")
    # Hints go BESIDE the blank, not only in the header: the field is where someone
    # is looking when they are about to type, and topologies take // comments now.
    hints = [
        (f'"freq_mhz": {json.dumps(ack_freq)}', ack_hint),
        (f'"freq_mhz": {json.dumps(data_freq)}', data_hint),
        # ── what each knob is, and which way to move it ──────────────────────
        # Wording taken from the modem's own option registry (docs/PARAMETERS.md,
        # generated from sdr_system --help) rather than restated, so the file cannot
        # drift from what the binary actually does.
        ('"det_mult"', "// TUNE. Detector gate = measured noise_floor x this. RAISE so "
                       "only real bursts fire (10-30 over the air); too HIGH misses "
                       "weak bursts, too LOW triggers on ambient RF"),
        ('"sync_threshold"', "// TUNE. ACQ correlation gate. A real preamble peaks near "
                             "31 after AGC, noise far lower: set BELOW the true peak "
                             "and ABOVE the noise. Too LOW locks onto noise and decodes "
                             "garbage; too HIGH never acquires. Watch [ACQ] Peak "
                             "correlation"),
        ('"bytes_length"', "// payload bytes per chunk. BIGGER = fewer bursts and more "
                           "throughput, but one bad chunk costs more. MUST match both "
                           "ends. Longest message = 64 x this"),
        ('"max_attempts"', "// source only: give up on a chunk after this many un-ACKed "
                           "sends. 0 = never give up, which keeps the pair in lockstep "
                           "but waits forever if the link dies"),
        ('"scheme"', "// bits per symbol: QPSK=2, 8-PSK=3, 16-QAM=4. HIGHER carries more "
                     "per symbol and needs more SNR. MUST match both ends"),
        ('"fec"', "// error correction: conv | ldpc | turbo, or \"\" for none. STRONGER "
                  "tolerates more noise and costs payload rate. MUST match both ends"),
        ('"waveform"', "// sc = single carrier, ofdm = multicarrier"),
        ('"quiet_phy"', "// true silences the modem's per-block chatter ([FILTER], "
                        "[MODULATION], [DEMODULATION], [AGC], [DETECTOR] -- one or more "
                        "lines per block per stage). DIAGNOSIS still prints either way: "
                        "[ACQ] peaks, CRC, ARQ progress, clip guard, RX timeouts, "
                        "[BER], [ERROR]. Set false to see everything"),
        ('"steps"', "// --algo runs only: how many algorithm iterations. A bare modem "
                    "run (run.sh radio) ignores it"),

        ('"ack_wireless"', "// true = ACK over the air, using the second RF block on "
                           "each node; false = over TCP"),
        ('"REPLACE_ME_SOURCE_ID"', "// uhd_find_devices on the transmitting box"),
        ('"host"', "// the ACK address. 127.0.0.1 = both radios on this machine. Two "
                   "machines: each node's own address, and both are needed. Ignored "
                   "entirely when ack is rf or none"),
    ]
    # gain means a different thing per direction, so the note has to know which block
    # it is in. A single note covering both makes the reader pick, which is the job the
    # note was supposed to do.
    GAIN = {
        "tx": "// dB, transmit power. RAISE for range; too high clips and distorts, "
              "which looks like a bad link rather than a loud one",
        "rx": "// dB, receive gain. RAISE for weak signals; too high saturates the "
              "front end and the burst arrives distorted",
    }
    out_lines, side = [], None
    for line in json.dumps(body, indent=2).splitlines():
        t = line.strip()
        if t in ('"tx": {', '"rx": {'):
            side = t[1:3]
        elif t.startswith('"gain"') and side:
            line = f"{line}   {GAIN[side]}"
        for needle, hint in hints:
            if needle in line:
                line = f"{line}   {hint}"
                break
        out_lines.append(line)

    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(H) + "\n")
        fh.write("\n".join(out_lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

    # ── one live draft per radio ─────────────────────────────────────────────
    # A draft is named by the survey that produced it, so every re-survey left
    # another one behind and the folder filled with near-identical files describing
    # the same pair at different moments. Worse than clutter: the old ones still
    # resolve, so a name that looks current can be a measurement from two bands ago,
    # and an edit goes into one file while a run reads another.
    #
    # Moved aside, not deleted. The one being replaced may be the file someone tuned
    # -- this project has already been careful not to clobber that -- and a survey is
    # cheap to repeat while a tuned link is not. `.superseded` ends in neither .json
    # nor .jsonc, so the resolver and the lister stop seeing it while it stays on disk
    # for anyone who wants a value back out of it.
    retired = []
    for q in sorted(glob.glob(os.path.join(out, f"draft-{id_val}-*.json"))
                    + glob.glob(os.path.join(out, f"draft-{id_val}-*.jsonc"))):
        if os.path.abspath(q) == os.path.abspath(path):
            continue
        try:
            os.replace(q, q + ".superseded")
            retired.append(os.path.basename(q))
        except OSError:
            pass
    if retired:
        print(f"[prepare] earlier draft(s) for {id_val} moved aside: "
              + ", ".join(f"{r} -> {r}.superseded" for r in retired))
    return path


def publish_frequencies(profile, d, stamp):
    """Write the survey's AVAILABLE FREQUENCIES as a small flat JSON named by the
    time they were measured. Returns the path written.

    The profile beside it keeps exactly ONE file per signal path -- each survey
    supersedes the last, so yesterday's picture of the band is deliberately gone.
    This file is the opposite: nothing is superseded, so the folder accumulates
    what the band looked like at each survey. That history is the point. A carrier
    that was clear last week may not be clear today, and when a link that used to
    work stops working, the first question is what changed in the band -- which is
    unanswerable if every survey overwrote the one before it.

    It is also deliberately FLAT: a plain list of carriers, then the numbers that
    justify them. It can be read at a glance, or by a two-line script, without
    knowing the profile schema or which of its keys is authoritative.
    """
    os.makedirs(d, exist_ok=True)
    opts = profile.get("options") or []
    use = profile.get("use", 0)
    radio = profile.get("radio") or {}
    rec = {
        "measured_utc":   profile.get("measured_utc"),
        "measured_local": profile.get("measured_local"),
        "node":           profile.get("node"),
        "radio": {"args":   radio.get("args"),   "device": radio.get("device"),
                  "band":   radio.get("band"),   "ant":    radio.get("ant"),
                  "subdev": radio.get("subdev")},
        # the recommended carrier, then the plain list of every usable one
        "recommended_mhz": (opts[use].get("carrier_mhz")
                            if isinstance(use, int) and 0 <= use < len(opts) else None),
        "available_mhz": [o.get("carrier_mhz") for o in opts],
        # and what justifies each: how wide it is, how quiet, whether a default
        # link fits inside it
        "frequencies": [{"carrier_mhz":       o.get("carrier_mhz"),
                         "band_mhz":          o.get("band_mhz"),
                         "width_mhz":         o.get("width_mhz"),
                         "floor_db":          o.get("floor_db"),
                         "fits_default_link": o.get("fits_default_link")}
                        for o in opts],
        "noise_floor_db": (profile.get("noise") or {}).get("floor_db"),
    }
    path = os.path.join(d, f"freqs-{stamp}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())         # a network share can lose a buffered write
    os.replace(tmp, path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--band", choices=sorted(BANDS), default="ism915")
    ap.add_argument("--step-mhz", type=float, default=None,
                    help="survey step (default: 1 for <200 MHz bands, else 10)")
    ap.add_argument("--device", choices=["b210", "n210", "x310"], default="b210")
    ap.add_argument("--args", default="", help="UHD device args")
    ap.add_argument("--rx-ant", default="RX2")
    ap.add_argument("--subdev", default=None)
    ap.add_argument("--gain", type=float, default=25.0,
                    help="RX gain — use the gain the EXPERIMENT will use")
    ap.add_argument("--max-options", type=int, default=3, metavar="N",
                    help="how many usable carriers to save (default 3). Each is a "
                         "complete parameter combination; the widest is recommended.")
    ap.add_argument("--dwell-windows", type=int, default=100)
    ap.add_argument("--rx-rate", type=float, default=1.5625e6,
                    help="receive sample rate (Hz) for the survey. Default 1.5625e6 is "
                         "exact on N210 (100/64) and X310 (200/128); a fixed-clock radio "
                         "only does master_clock/N. If yours coerces, pick from the modem's "
                         "'nearest usable rates' line.")
    ap.add_argument("--acq-seconds", type=float, default=8.0)
    ap.add_argument("--node", default=None,
                    help="name for the profile. Default: this node's stable key "
                         "-- $UNION_SITE, else the radio's serial, else "
                         "host-<hostname>. NOT the bare hostname: inside a "
                         "session that is the pod id and changes every session, "
                         "so a profile filed under it is lost on the next pod.")
    ap.add_argument("--write", action="store_true",
                    help="publish the profile to /workspace/experiments/searching/ "
                         "(override the directory with $UNION_SETTINGS_DIR)")
    ap.add_argument("--binary", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing topology draft. Without it an existing "
                         "one is kept, because it is the file you edit.")
    ap.add_argument("--topology-only", action="store_true",
                    help="skip the sweep: write the topology draft from the newest "
                         "survey already saved for this radio. No radio is touched.")
    ap.add_argument("--all", action="store_true",
                    help="discover every connected USRP and prepare each in "
                         "turn (devices are auto-detected; give each its "
                         "antenna with --band-map)")
    ap.add_argument("--band-map", default="",
                    help="--all: which antenna hangs on which radio, e.g. "
                         "'30CD424:vert900,192.168.40.2:vert2450-5g'. A device "
                         "not named here falls back to --band, with a warning "
                         "— the antenna cannot be probed.")
    a = ap.parse_args()

    # Regenerate the topology draft from a survey already on disk, touching no radio.
    # The draft is written at the end of a sweep, so anyone whose code predated it --
    # or whose write failed -- would otherwise have to spend the band again for a file
    # built entirely from numbers that are already saved.
    if getattr(a, "topology_only", False):
        d = os.environ.get("UNION_SETTINGS_DIR") or "/workspace/experiments/searching"
        try:
            prof, stamp = newest_profile(d, a.args or None)
        except FileNotFoundError as e:
            sys.exit(f"[prepare] {e}")
        radio = prof.get("radio") or {}
        print(f"[prepare] from the survey of {radio.get('device')} "
              f"{radio.get('args')} on {radio.get('subdev')}/{radio.get('ant')} "
              f"({radio.get('band')}), measured {stamp}")
        try:
            path = publish_topology_draft(prof, d, stamp, force=a.force)
        except FileExistsError as e:
            sys.exit(f"[prepare] {e}")
        print(f"[prepare] topology draft: {path}")
        print(f"[prepare]   replace REPLACE_ME_SOURCE_ID (uhd_find_devices on the "
              f"transmitting box), then:")
        print(f"[prepare]   ./run.sh --algo echo --topology "
              f"{os.path.splitext(os.path.basename(path))[0]} --node snk")
        return
    if a.node is None:
        # Same identity rule as discover-node.py and union/phy_profile.py, so what
        # prepare_phy writes is what radio.sh and run.sh later look for.
        #
        # This file is drivers/usrp/python/prepare_phy.py, so the repo root is FOUR
        # levels up, not three. Three landed on drivers/, the import always failed,
        # and the except below quietly filed every profile under the hostname --
        # which inside a session is the pod id and changes with the pod. The
        # profile was written, reported as published, and then orphaned on the next
        # session: precisely the failure the stable key exists to prevent, hidden
        # by the fallback that was supposed to be the safety net.
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        sys.path.insert(0, repo)
        try:
            from union.phy_profile import node_key
            a.node = node_key()
        except Exception as e:
            a.node = "host-" + os.uname().nodename
            print(f"[prepare] could not read this node's stable key "
                  f"({e.__class__.__name__}: {e}) — filing under {a.node!r}, which "
                  f"is this session's pod name and will not be found by the next "
                  f"session. Pass --node, or set $UNION_SITE.", file=sys.stderr)

    if a.all:
        band_map = {}
        for pair in a.band_map.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                if v.strip() not in BANDS:
                    sys.exit(f"[prepare] --band-map: unknown band {v.strip()!r} "
                             f"(use {', '.join(sorted(BANDS))})")
                band_map[k.strip()] = v.strip()
        devs = find_devices()
        if not devs:
            sys.exit("[prepare] --all: no USRPs visible (uhd_find_devices "
                     "found nothing)")
        print(f"[prepare] {len(devs)} device(s) visible:")
        plans = []
        for d in devs:
            cls, args_str, ident = classify(d)
            band, mapped = band_for(ident, band_map, a.band)
            note = "" if mapped else "  (no --band-map entry: assuming " + band + ")"
            print(f"  {cls:>5}  {args_str:<28} -> band {band}{note}")
            plans.append((cls, args_str, ident, band))
        if a.dry_run:
            return
        for cls, args_str, ident, band in plans:
            print(f"\n════ preparing {cls} {args_str} ({band}) ════")
            # Key each radio by ITS OWN identity, not by the host's. Prefixing
            # with the host key (which is the FIRST radio's serial) filed the
            # second radio's survey under a name the second radio would never
            # look itself up by, so nothing could find it.
            argv = ["--device", cls, "--args", args_str, "--band", band,
                    "--gain", str(a.gain), "--rx-ant", a.rx_ant,
                    "--node", ident.replace(".", "-") or a.node]
            if a.subdev:
                argv += ["--subdev", a.subdev]
            if a.write:
                argv += ["--write"]
            if a.binary:
                argv += ["--binary", a.binary]
            r = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv)
            if r.returncode != 0:
                print(f"[prepare] {args_str} FAILED (exit {r.returncode}) — "
                      f"continuing with the rest", file=sys.stderr)
        return

    lo0, hi0, why = BANDS[a.band]
    step = a.step_mhz or (1.0 if hi0 - lo0 <= 200 else 10.0)
    subdev = a.subdev or {"b210": "A:A", "n210": "A:0", "x310": "A:0"}[a.device]
    radio = dict(rx_args=a.args, rx_gain=a.gain, rx_ant=a.rx_ant, rx_subdev=subdev,
                 rx_rate=a.rx_rate)
    if a.binary:
        radio["binary"] = a.binary

    n_pts = int((hi0 - lo0) / step) + 1
    plan = (f"[prepare] {a.band} ({lo0:g}-{hi0:g} MHz @ {step:g}) -> dwell "
            f"{a.dwell_windows} windows -> detector cross-check 6s -> "
            f"ACQ listen {a.acq_seconds:g}s   "
            f"(~{n_pts * 2 + a.acq_seconds + 16:.0f}s total, receive-only)")
    print(plan)
    if a.dry_run:
        return

    # 1 · survey
    freqs = [round(lo0 + i * step, 6) for i in range(n_pts)]
    rows = band_survey(freqs, 10.0, a.gain, a.args, a.rx_ant, subdev, a.binary,
                       rx_rate=a.rx_rate)
    sweep_floor, regions = quiet_regions(rows)
    if not regions:
        sys.exit("[prepare] no quiet region found — the whole band is occupied?")
    lo, hi = regions[0]["usable_mhz"]
    floor = sweep_floor
    if len(regions) > 1:
        print(f"[prepare] {len(regions)} quiet regions found; all are saved, the "
              f"widest is the recommendation:")
        for i, r in enumerate(regions[:6]):
            mark = "  <- recommended" if i == 0 else ""
            print(f"           {i + 1}. {r['usable_mhz'][0]:g}-{r['usable_mhz'][1]:g} MHz "
                  f"({r['width_mhz']:g} MHz, {r['region_floor_db']:.1f} dB) "
                  f"carrier {r['carrier_mhz']:g}{mark}")
    carrier = round((lo + hi) / 2.0, 3)
    width = hi - lo
    print(f"\n[prepare] usable band: {lo:g}-{hi:g} MHz ({width:g} MHz wide), "
          f"floor ~{floor:.1f} dB -> carrier {carrier:g} MHz (plateau center)")

    # 2 · dwell
    med, p99, det_mult = dwell(carrier, a.dwell_windows, 10.0, radio)
    print(f"[prepare] dwell at {carrier:g}: floor {med:.1f} dB, p99 {p99:.1f} dB "
          f"-> det-mult {det_mult:g}")

    # 2b · the receiver's OWN floor, on the same gain, so the two scales can be
    #      compared instead of guessed at
    det = detector_floor(carrier, 6.0, radio, a.binary)
    if det:
        det_floor_db, det_thr_db = det
        offset = med - det_floor_db
        print(f"[prepare] receiver's own floor {det_floor_db:.1f} dB"
              + (f" (its threshold {det_thr_db:.1f} dB)" if det_thr_db is not None else "")
              + f" -> sense is {offset:+.1f} dB from the detector at gain {a.gain:g}")
        if abs(offset) > 3.0:
            print(f"           the two disagree by {abs(offset):.1f} dB. det-mult is a "
                  f"RATIO on the detector's own floor, so it is unaffected; but an "
                  f"absolute --energy_threshold must be quoted on the detector's scale, "
                  f"not the survey's.")
    else:
        det_floor_db = det_thr_db = offset = None
        print("[prepare] receiver printed no calibration block — cannot cross-check "
              "the survey's floor against the detector's")

    # 3 · ACQ noise distribution
    acq = noise_acq(carrier, a.acq_seconds, radio, a.binary)
    if acq:
        noise_p95, sync_thr = acq
        print(f"[prepare] noise ACQ p95 {noise_p95:.1f} (real preamble ~{PREAMBLE_PEAK:g}) "
              f"-> sync-threshold {sync_thr:g}")
    else:
        sync_thr = 15.0
        print("[prepare] band too quiet to trigger noise ACQ — sync-threshold 15 "
              "is a PLACEHOLDER, not a measurement. It is only a floor: noise "
              "here never scored high enough to say where the real one is. What "
              "a genuine preamble scores can only be seen once a link runs — "
              "calibration_rx.sh + calibration_tx.sh measure exactly that and "
              "write the result back here.")

    # 4 · does the default link fit?
    fits = width >= DEFAULT_LINK_BW_MHZ
    if not fits:
        print(f"[prepare] WARNING: quiet region ({width:g} MHz) is narrower than "
              f"the default 2 MS/s link needs (~{DEFAULT_LINK_BW_MHZ} MHz) — "
              f"lower the rates or find another band")

    # ONE place per value. The previous layout stored `recommended` as a full
    # copy of candidates[0] and then repeated the whole lot again as flat keys
    # for an older reader, so a four-region survey wrote 102 lines in which every
    # number appeared five or six times -- long enough that nobody read it, and
    # ambiguous about which copy was authoritative.
    #
    # Here: what was measured once (the noise, the thresholds derived from it)
    # sits once at the top. `options` holds the usable carriers, each a complete
    # parameter combination on its own, and `use` says which one is recommended.
    def option(r, measured):
        o = {"carrier_mhz": r["carrier_mhz"],
             "band_mhz":    r["usable_mhz"],
             "width_mhz":   r["width_mhz"],
             "floor_db":    r["region_floor_db"],
             "fits_default_link": r["width_mhz"] >= DEFAULT_LINK_BW_MHZ}
        if measured:
            # only the recommended carrier is dwelt on; the rest inherit the
            # thresholds, and this says so rather than implying every option was
            # measured with equal care
            o["measured_here"] = True
        return o

    keep = regions[:a.max_options]
    dropped = len(regions) - len(keep)
    options = [option(r, i == 0) for i, r in enumerate(keep)]
    if dropped:
        print(f"[prepare] saving the top {len(keep)} of {len(regions)} regions "
              f"(--max-options to keep more); dropped: "
              + ", ".join(f"{r['carrier_mhz']:g} MHz" for r in regions[len(keep):]))

    # One instant for the record and its filename, so they never disagree. The
    # filename carries LOCAL wall-clock time (see survey_timestamps); UTC stays
    # in the record for the resolver to sort on.
    measured_utc, measured_local, stamp = survey_timestamps()
    profile = {
        "schema": 3,
        "node": a.node,
        "measured_utc": measured_utc,
        "measured_local": measured_local,
        # This survey is receive-only, so the radio it characterises is the
        # RECEIVER of any link it takes part in. --args named that radio; recording
        # the role here lets calibration read a searching/ file and know, without a
        # separate reservation, which end this is. (A transmitter is never surveyed:
        # its own noise says nothing about the link it will drive.)
        "role": "rx",
        # BOTH names for the radio. `args` is how this survey addressed it, which is
        # the measurement's own record; `serial` is what the hardware calls itself, so
        # a later run that names the radio by serial -- which is what a shared topology
        # must do, since an address means something different on each box -- still
        # matches this measurement. Resolved here because the survey runs ON the box
        # holding the radio; None when UHD says nothing, and then nothing is claimed.
        "radio": {"device": a.device, "args": a.args, "ant": a.rx_ant,
                  "subdev": subdev, "gain_db": a.gain, "band": a.band,
                  "serial": (re.search(r"serial=([^,\s]+)", a.args or "").group(1)
                             if "serial=" in (a.args or "")
                             else resolve_serial(a.args or ""))},
        # measured once, at the recommended carrier, and shared by every option
        "noise": {"sweep_floor_db": round(sweep_floor, 1),
                  "floor_db": round(med, 1),
                  "p99_db": round(p99, 1),
                  "detector_floor_db": (round(det_floor_db, 1)
                                        if det_floor_db is not None else None),
                  "detector_threshold_db": (round(det_thr_db, 1)
                                            if det_thr_db is not None else None),
                  "sense_minus_detector_db": (round(offset, 1)
                                              if offset is not None else None),
                  "acq_p95": (round(acq[0], 1) if acq else None)},
        "det_mult": det_mult,
        "sync_threshold": sync_thr,
        # False when noise never triggered ACQ: then the threshold is a default,
        # not something this survey measured, and only a real link can settle it.
        "sync_threshold_measured": bool(acq),
        "use": 0,
        "options": options,
    }

    print("\n── the hands-free settings ──────────────────────────────────")
    print(f"  radio.sh:  --freq {carrier:g}e6 --gain {a.gain:g} "
          f"--det-mult {det_mult:g} --sync-threshold {sync_thr:g}")
    print(f"  run.sh:    --freq {carrier:g} --usrp-set det_mult={det_mult:g} "
          f"--usrp-set sync_threshold={sync_thr:g}")
    print(f'  topology:  "defaults": {{ "freq_mhz": {carrier:g} }}')

    if a.write:
        # searching/, not settings/: settings holds per-session records that are
        # rewritten every session start and reaped when the pod goes. A band
        # survey costs minutes and a radio and stays true for as long as the
        # antenna and the room do, so it does not belong in a folder whose
        # contents are expected to churn.
        d = os.environ.get("UNION_SETTINGS_DIR") or "/workspace/experiments/searching"
        path, superseded = publish_profile(profile, d, a.node, a.band, subdev,
                                           a.rx_ant, stamp)
        for q in superseded:
            print(f"[prepare] superseded earlier survey: {os.path.basename(q)}")

        # The frequency list is a keepsake, not the authority — so a failure to
        # write it must never cost the profile, which is the artifact everything
        # downstream actually resolves against.
        try:
            fpath = publish_frequencies(profile, d, stamp)
            avail = ", ".join(f"{o['carrier_mhz']:g}" for o in profile["options"])
            print(f"[prepare] available frequencies ({avail} MHz) saved: {fpath}")
        except Exception as e:
            print(f"[prepare] WARNING: could not save the frequency list "
                  f"({e.__class__.__name__}: {e}) — the profile above is unaffected",
                  file=sys.stderr)

        # A topology, prefilled with everything this survey just established. Last and
        # non-fatal on purpose: it is a convenience built FROM the measurement, so it
        # must never be able to cost the measurement, which took minutes on a radio.
        try:
            tpath = publish_topology_draft(profile, d, stamp,
                                           force=getattr(a, "force", False))
            print(f"[prepare] topology draft: {tpath}")
            print(f"[prepare]   replace REPLACE_ME_SOURCE_ID (uhd_find_devices on the "
                  f"transmitting box), then:")
            print(f"[prepare]   ./run.sh --algo echo --topology {tpath} --node snk")
        except Exception as e:
            print(f"[prepare] WARNING: could not write the topology draft "
                  f"({e.__class__.__name__}: {e}) — the survey above is unaffected",
                  file=sys.stderr)

        # Prove it landed. os.replace returning is not evidence the bytes are on
        # the share: report what is actually readable at the path, and where that
        # path really is, because "published" against a directory that is not the
        # mount everyone else sees looks identical to success.
        try:
            size = os.path.getsize(path)
            import subprocess as _sp
            mnt = _sp.run(["df", "-h", d], capture_output=True, text=True).stdout
            mnt = (mnt.strip().splitlines() or [""])[-1]
            print(f"[prepare] on disk: {size} bytes at {path}")
            print(f"[prepare] that path lives on: {mnt}")
        except Exception as e:
            print(f"[prepare] WARNING: wrote {path} but cannot stat it back "
                  f"({e.__class__.__name__}: {e}) — it may not have persisted",
                  file=sys.stderr)
        print(f"\n[prepare] profile published: {path} — visible to every "
              f"session of the account, on every testbed")
        print(f"[prepare] surveyed at {measured_local} ({measured_utc})")

        # Read it back the way run.sh and radio.sh will. Writing a file and
        # announcing success proves only that a write succeeded; it does not
        # prove the thing that matters, which is that the resolver finds it under
        # the key it was filed with. Saying "published" without checking is how a
        # profile came to be written, reported, and then never picked up.
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))))
            from union import phy_profile as pp
            # Resolve it the way radio.sh and run.sh will: by the radio's own
            # args and connector and the recommended carrier -- NOT bare, which
            # cannot tell two of this radio's bands apart and cannot find a
            # serial-named file when the radio is addressed by addr.
            vals, found, why = pp.load(args=a.args, band=a.band, subdev=subdev,
                                       ant=a.rx_ant, near_mhz=carrier)
            if found and os.path.samefile(found, path):
                print(f"[prepare] verified: run.sh and radio.sh resolve this "
                      f"profile ({why}) — "
                      + ", ".join(f"{k}={v}" for k, v in sorted(vals.items())))
            elif found:
                print(f"[prepare] WARNING: written, but the resolver picks a "
                      f"DIFFERENT profile first: {found} ({why}). This one will be "
                      f"ignored until that is removed or --phy-profile-node names "
                      f"it.", file=sys.stderr)
            else:
                print(f"[prepare] WARNING: written, but nothing resolves it back "
                      f"({why}). It will not be used. Check that $UNION_SETTINGS_DIR "
                      f"or /workspace/experiments/settings is the same path both "
                      f"sides see.", file=sys.stderr)
        except Exception as e:
            print(f"[prepare] could not verify the profile is readable "
                  f"({e.__class__.__name__}: {e})", file=sys.stderr)
    else:
        print("\n[prepare] add --write to publish this profile to the shared workspace")


if __name__ == "__main__":
    main()
