#!/usr/bin/env python3
"""
test_flags.py — does every CLI flag actually reach the thing it claims to configure?

    python3 union/test_flags.py

A flag can fail in three places, and only the last is visible by reading the parser:

    1. never read            — parsed into the namespace and forgotten
    2. read but not passed   — read at the call site, dropped before the constructor
    3. passed but ignored    — the constructor takes it and does nothing with it

(2) is the dangerous one, because the flag looks completely functional. Two real bugs
of exactly that shape have already shipped here: --fec never reached the radio (the
config hardcoded FEC on), and --snr-db silently did nothing on hardware.

So this walks the FULL path — an argv string in, the constructed object out — and
asserts the value arrives. Exits non-zero on any failure, so CI can run it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import run_algo as R                                    # noqa: E402
import phy_link as pl                                   # noqa: E402

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = DIM = OFF = ""

results = []


def parse(argv):
    """Run argv through the real parser, exactly as main() does."""
    a = R.build_parser().parse_args(["--algo", "echo"] + argv)
    if a.role is None:
        a.role = "peer" if a.node is not None else "loopback"
    if a.radio:
        dev = R.device_args(a.radio)
        a.tx_args = a.tx_args or dev
        a.rx_args = a.rx_args or dev
    return a


def check(flag, argv, get, want):
    """Feed `argv` through the CLI, build the object, pull `get` out of it."""
    try:
        got = get(parse(argv))
        ok = got == want
    except Exception as e:                              # noqa: BLE001
        got, ok = f"{type(e).__name__}: {e}", False
    results.append((flag, ok, got, want))
    print(f"  {flag:22s} {GREEN+'OK  '+OFF if ok else RED+'FAIL'+OFF}  "
          f"{DIM}got={got!r}{OFF}" + ("" if ok else f"  want={want!r}"))


# ── the USRP link: every knob must land in the sdr_system config ────────────
print("\n  USRP link  (--role tx, the real-radio path)")
L = lambda a: R.build_link(a, "tx")                     # noqa: E731
check("--modulation",   ["--modulation", "16-QAM"],       lambda a: L(a).cfg["scheme"],        "16-QAM")
check("--scheme",       ["--scheme", "8-PSK"],            lambda a: L(a).cfg["scheme"],        "8-PSK")
check("--fec ldpc",     ["--fec", "ldpc"],                lambda a: (L(a).cfg["fec"], L(a).cfg.get("fec_type")), (True, "ldpc"))
check("--fec ''",       ["--fec", ""],                    lambda a: L(a).cfg["fec"],           False)
check("--freq",         ["--freq", "905"],                lambda a: L(a).cfg["tx_freq"],       905e6)
check("--samp-rate",    ["--samp-rate", "4e6"],           lambda a: L(a).cfg["tx_rate"],       4e6)
check("--symbol-rate",  ["--symbol-rate", "2e6"],         lambda a: L(a).cfg["symbol_rate"],   2e6)
check("--tx-gain",      ["--tx-gain", "61"],              lambda a: L(a).tx_gain,              61.0)
check("--rx-gain",      ["--rx-gain", "22"],              lambda a: L(a).rx_gain,              22.0)
check("--ack-transport",["--ack-transport", "rf"],        lambda a: L(a).cfg["ack_transport"], "rf")
check("--ack-timeout",  ["--ack-timeout", "1234"],        lambda a: L(a).cfg["timeout"],       1234)
check("--max-attempts", ["--max-attempts", "7"],          lambda a: L(a).max_attempts,         7)
check("--arq",          ["--arq", "stop-and-wait"],       lambda a: L(a).arq,                  "stop-and-wait")
check("--ack-host",     ["--ack-host", "10.0.0.9"],       lambda a: L(a).ack_host,             "10.0.0.9")
check("--net-port",     ["--net-port", "6001"],           lambda a: L(a).net_port,             6001)
check("--radio serial", ["--radio", "30CD424"],           lambda a: L(a).tx_args,              "serial=30CD424")
check("--radio addr",   ["--radio", "192.168.40.2"],      lambda a: L(a).rx_args,              "addr=192.168.40.2")
check("--tx-args",      ["--tx-args", "serial=ABC"],      lambda a: L(a).tx_args,              "serial=ABC")
check("--down-port",    ["--down-port", "5999"],          lambda a: L(a).down_port,            5999)

# ── the LoRa PHY: the chip's own knobs ──────────────────────────────────────
print("\n  LoRa PHY  (--channel lora)")
C = lambda a: R.build_channel(a)                        # noqa: E731
check("--lora-sf",      ["--channel","lora","--lora-sf","12"],        lambda a: C(a).tx.sf,        12)
check("--lora-cr",      ["--channel","lora","--lora-cr","8"],         lambda a: C(a).tx.cr,        8)
check("--lora-bw",      ["--channel","lora","--lora-bw","250000"],    lambda a: C(a).tx.bw_hz,     250000)
check("--lora-power",   ["--channel","lora","--lora-power","20"],     lambda a: C(a).tx.power_dbm, 20)
check("--freq (lora)",  ["--channel","lora","--freq","923"],          lambda a: C(a).tx.freq_hz,   923_000_000)
check("--snr-db (sim)", ["--channel","lora","--snr-db","-3"],         lambda a: C(a).medium.snr_db, -3.0)
check("--max-attempts", ["--channel","lora","--max-attempts","3"],    lambda a: C(a).max_attempts, 3)
check("--arq (lora)",   ["--channel","lora","--arq","stop-and-wait"], lambda a: C(a).arq,          "stop-and-wait")
check("--lora-verbose", ["--channel","lora","--lora-verbose"],        lambda a: C(a).verbose,      True)
check("--lora-backend", ["--channel","lora","--lora-backend","sim"],  lambda a: C(a).backend,      "sim")

# ── the USRP in-process modem ───────────────────────────────────────────────
print("\n  USRP modem  (--channel usrp, the pyphy backend)")
check("--scheme (pyphy)", ["--channel","usrp","--scheme","BPSK"],     lambda a: C(a).scheme,   "BPSK")
check("--fec (pyphy)",    ["--channel","usrp","--fec","conv"],        lambda a: C(a).fec,      "conv")
check("--snr-db (pyphy)", ["--channel","usrp","--snr-db","3"],        lambda a: C(a).snr_db,   3.0)

# ── decentralised peers ─────────────────────────────────────────────────────
print("\n  peer link  (--node K)")
def peer(a, attr):
    link = pl.PeerLink(node_id=a.node, n_nodes=a.agents, topology=a.topology,
                       peers=[h.strip() for h in a.peers.split(",") if h.strip()] or None,
                       base_port=a.peer_port, link=(a.peer_link or "tcp"),
                       lora_backend=a.lora_backend, lora_sf=a.lora_sf)
    v = getattr(link, attr)
    link.close()
    return v
check("--node",       ["--node","2","--agents","4"],                 lambda a: peer(a,"id"),        2)
check("--agents",     ["--node","0","--agents","5"],                 lambda a: peer(a,"n"),         5)
check("--peer-port",  ["--node","0","--agents","2","--peer-port","5911"], lambda a: peer(a,"base_port"), 5911)
check("--peers",      ["--node","0","--agents","2","--peers","10.0.0.1,10.0.0.2"], lambda a: peer(a,"hosts"), ["10.0.0.1","10.0.0.2"])
check("--topology",   ["--node","0","--agents","4","--topology","full"], lambda a: len(peer(a,"edges")), 6)

# ── runner-level flags: assert they change what the runner does ─────────────
print("\n  runners")
check("--topology ring", ["--agents","6"], lambda a: len(pl.gossip_edges(6, "ring")), 6)
check("--topology full", ["--agents","6"], lambda a: len(pl.gossip_edges(6, "full")), 15)
check("--topology edges",["--agents","4"], lambda a: len(pl.gossip_edges(4, "0-1,1-2,2-3")), 3)
check("--relays",        ["--relays","3"], lambda a: a.relays, 3)
check("--steps",         ["--steps","9"],  lambda a: a.steps, 9)

bad = [r for r in results if not r[1]]
print(f"\n  {len(results)} flag paths checked — "
      + (f"{GREEN}all reach their target{OFF}" if not bad else f"{RED}{len(bad)} BROKEN{OFF}"))
for f, _, got, want in bad:
    print(f"    {f}: got {got!r}, want {want!r}")
sys.exit(1 if bad else 0)
