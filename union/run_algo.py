#!/usr/bin/env python3
"""
run_algo.py — load an uploaded algorithm from experiments/<name>/app.py and run it
over the PHY through the uniform phy_link contract.

  # radio-free round-trip (lossless):
  python3 union/run_algo.py --algo echo --role loopback

  # radio-free through the REAL modem + AWGN:
  PYTHONPATH=drivers/usrp/bindings arch -x86_64 python3 union/run_algo.py \
      --algo echo --role loopback --channel pyphy --snr-db 6

  # over the radio (two hosts): rx first, then tx
  python3 union/run_algo.py --algo echo --role rx --rx-args addr=192.168.20.2
  python3 union/run_algo.py --algo echo --role tx --tx-args serial=30CD424 \
      --ack-host <RX_IP> --net-host <RX_IP>

ROLE NAMES
    An algorithm may name its own roles instead of using the PHY's tx/rx, by declaring
    a module-level map from its name to the end of the link that role drives:

        ROLES = {"client": "tx", "server": "rx"}      # in experiments/<name>/app.py

    The experimenter then types the algorithm's own name and the algorithm's make(role)
    receives that same name, so the two always match:

        python3 union/run_algo.py --algo fl --role server ...     # == --role rx
        python3 union/run_algo.py --algo fl --role client ...     # == --role tx

    tx/rx stay valid for every algorithm; an algorithm that declares no ROLES behaves
    exactly as before.
"""
import argparse, importlib.util, inspect, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                            # union/ -> repo
sys.path.insert(0, HERE)
import phy_link as pl


def _has_io(c):
    return any(hasattr(c, m) for m in ("transmit", "produce"))


# ── role names: the algorithm's own vocabulary <-> the node types of the PHY ──
#   tx    = initiator, transmits only          rx    = responder, receives only
#   relay = BOTH: receives from upstream and transmits downstream (a middle node)
#   peer  = BOTH, at different steps: one node of a decentralised network, running as
#           its own process and exchanging with its graph neighbours (needs --node)
TRANSPORT_ROLES = ("tx", "rx", "relay", "peer")
GROUP_ROLES = ("loopback", "chain", "gossip", "multi", "aircomp")  # runners building every node


def role_map(mod):
    """Return (alias -> transport, transport -> alias) for one algorithm.

    An algorithm names its own roles with  ROLES = {"client": "tx", "server": "rx"}.
    Without it the algorithm just speaks the PHY's own tx/rx/relay."""
    declared = getattr(mod, "ROLES", None) or {}
    alias2t = {t: t for t in TRANSPORT_ROLES}
    for alias, transport in declared.items():
        t = str(transport).lower()
        if t not in TRANSPORT_ROLES:
            sys.exit(f"ROLES[{alias!r}] = {transport!r} — must be one of "
                     f"{', '.join(TRANSPORT_ROLES)}")
        alias2t[str(alias).lower()] = t
    # the algorithm-facing name for each node type: its declared alias, else tx/rx/relay
    t2alias = {t: t for t in TRANSPORT_ROLES}
    for alias, t in declared.items():
        t2alias[str(t).lower()] = str(alias).lower()
    return alias2t, t2alias


#   --channel says WHICH PHY (which driver) carries the payloads;
#   --<phy>-backend says HOW that PHY is attached. The two are separate questions and
#   every PHY answers them the same way:
#
#       --channel ideal                                     radio-free, lossless
#       --channel usrp   --usrp-backend pyphy | radio       drivers/usrp
#       --channel lora   --lora-backend sim | serial | spi  drivers/lora
#
#   'sim' is accepted for ideal and 'pyphy' for usrp, so older commands keep working.
CHANNEL_ALIASES = {"sim": "ideal", "pyphy": "usrp"}


def build_channel(a):
    """Turn the CLI into a PHY. One place, so every runner offers the same choices."""
    kind = CHANNEL_ALIASES.get(a.channel, a.channel)
    if kind == "usrp":
        if a.usrp_backend == "radio":
            sys.exit("--channel usrp --usrp-backend radio drives REAL USRPs, and a "
                     "single process has no peer to answer it. Run the two nodes as "
                     "separate processes (--role tx / --role rx), or use the default "
                     "--usrp-backend pyphy to run the same modem in one process.")
        return pl.make_channel("pyphy", scheme=a.scheme, fec=(a.fec or None),
                               snr_db=a.snr_db)
    if kind == "lora":
        return pl.make_channel("lora", backend=a.lora_backend, sf=a.lora_sf,
                               cr=a.lora_cr, bw_hz=a.lora_bw, power_dbm=a.lora_power,
                               freq_hz=int(a.freq * 1e6), snr_db=a.snr_db,
                               port=a.lora_port, verbose=a.lora_verbose,
                               max_attempts=(8 if a.max_attempts is None
                                             else a.max_attempts), arq=a.arq)
    return pl.make_channel("ideal")


# Knobs that belong to ONE PHY and mean nothing to the others. Different physical
# layers have genuinely different logic — the USRP's waveform, FEC and acknowledgement
# path are ours to choose, while a LoRa chip embeds its modulation and CRC and offers
# its own spreading factor instead. Passing one PHY's knob to another is always a
# mistake, and silently ignoring it would hide a wrong experiment.
PHY_ONLY_FLAGS = {
    "usrp": {"scheme": "--scheme", "fec": "--fec",
             "ack_transport": "--ack-transport", "ack_timeout": "--ack-timeout",
             "samp_rate": "--samp-rate", "symbol_rate": "--symbol-rate",
             "tx_gain": "--tx-gain", "rx_gain": "--rx-gain"},
    "lora": {"lora_sf": "--lora-sf", "lora_cr": "--lora-cr", "lora_bw": "--lora-bw",
             "lora_power": "--lora-power", "lora_backend": "--lora-backend",
             "lora_port": "--lora-port"},
}


US_ISM_MHZ = (902.0, 928.0)         # the band both radios are licensed to use here


def check_band(freq_mhz, kind):
    """The carrier is the experimenter's to choose, but not every choice is legal where
    the hardware lives. Warn rather than refuse: EU868 boards and other-region setups are
    real, and this is a research testbed, not a certification tool."""
    lo, hi = US_ISM_MHZ
    if lo <= freq_mhz <= hi:
        return
    where = {868.0: "EU 863-870", 433.0: "EU/Asia 433"}.get(round(freq_mhz), None)
    print(f"[run_algo] WARNING: --freq {freq_mhz:g} MHz is OUTSIDE the US ISM band "
          f"({lo:g}-{hi:g} MHz)"
          + (f" — that is the {where} band" if where else "")
          + f". Legal only with the right hardware and region; a US {kind} radio should "
            f"stay inside {lo:g}-{hi:g} MHz.")


def warn_foreign_flags(a, ap, kind):
    """Say so when a knob was given for a PHY that is not the one selected."""
    for phy, flags in PHY_ONLY_FLAGS.items():
        if phy == kind:
            continue
        used = [name for attr, name in flags.items()
                if getattr(a, attr, None) != ap.get_default(attr)]
        if used:
            verb = "configures" if len(used) == 1 else "configure"
            it = "is" if len(used) == 1 else "are"
            print(f"[run_algo] NOTE: {', '.join(used)} {verb} the {phy} PHY and {it} "
                  f"ignored with --channel {a.channel}. Each PHY has its own knobs: the "
                  f"USRP's waveform/FEC are chosen, LoRa's are embedded in the chip.")


def build_link(a, transport):
    """One END of a point-to-point link (--role tx / rx / relay), on the PHY --channel
    selects. Roles belong to the middleware, so every PHY offers all of them:

        --channel usrp --role tx     the USRP link (wireless request + TCP reply)
        --channel lora --role tx     the LoRa link (a transceiver: reply over the radio)

    The radio-free channels have no second host to talk to, so they are not links; a
    one-process run of the same experiment is --role loopback."""
    kind = CHANNEL_ALIASES.get(a.channel, a.channel)
    if kind == "lora":
        sys.path.insert(0, os.path.join(REPO, "drivers", "lora", "python"))
        import lora_driver
        return lora_driver.LoRaLink(role=transport, node=(a.node if a.node is not None else 0),
                                    backend=a.lora_backend, port=a.lora_port,
                                    sf=a.lora_sf, cr=a.lora_cr, bw_hz=a.lora_bw,
                                    power_dbm=a.lora_power, freq_hz=int(a.freq * 1e6),
                                    snr_db=a.snr_db, verbose=a.lora_verbose,
                                    max_attempts=(8 if a.max_attempts is None
                                                  else a.max_attempts), arq=a.arq)
    if kind == "ideal":
        # --channel has never applied to the point-to-point roles, which always meant
        # the USRP link. Keep every existing command working, and say which PHY it got.
        print(f"[run_algo] --role {transport} with no --channel: using the USRP link "
              f"(drivers/usrp). Add --channel lora for the LoRa link.")
    return pl.RadioRoundTrip(role=transport, tx_args=a.tx_args, rx_args=a.rx_args,
                             ack_host=a.ack_host, net_host=a.net_host,
                             net_port=a.net_port, scheme=a.scheme,
                             down_host=a.down_host, down_port=a.down_port,
                             freq_hz=a.freq * 1e6, samp_rate=a.samp_rate,
                             symbol_rate=a.symbol_rate, fec=a.fec,
                             tx_gain=a.tx_gain, rx_gain=a.rx_gain,
                             ack_transport=a.ack_transport, ack_timeout_ms=a.ack_timeout,
                             max_attempts=(50 if a.max_attempts is None else a.max_attempts),
                             arq=a.arq)


def device_args(spec):
    """Normalise one node's USRP into UHD device args.

    A B210/B200 is addressed by SERIAL, an N210/X310 by IP ADDRESS, so both spellings
    have to be accepted:  serial=30CD424 · addr=192.168.40.2 · or the bare value, which
    is read as an address when it looks like one and as a serial otherwise."""
    s = (spec or "").strip()
    if not s or "=" in s:
        return s
    return f"addr={s}" if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s) else f"serial={s}"


def resolve_role(requested, alias2t, t2alias, algo):
    """Map what the experimenter typed onto (transport end, name the algorithm sees).

    Typing the PHY's own tx/rx still builds the algorithm under ITS name, so
    --role tx and --role client are the same run for an algorithm that declares
    ROLES = {"client": "tx", ...}."""
    r = requested.lower()
    if r in GROUP_ROLES:
        return r, None
    if r in alias2t:
        transport = alias2t[r]
        return transport, t2alias[transport]
    sys.exit(f"--role {requested!r} is not a role of experiments/{algo}\n"
             f"  valid roles: {', '.join(GROUP_ROLES)}, {', '.join(sorted(alias2t))}")


def load_app_factory(name):
    """Return (factory(role, index=None, total=None) -> SdrApp, how, module).

    The algorithm only has to declare what to transmit and what to receive — provided as
    a make(role) binding, a plain class with transmit()/receive(msg), an SdrApp subclass,
    or module-level functions.

    A runner that builds MANY nodes (multi / chain / gossip) also offers each node its
    position in the group. An algorithm that wants it widens its binding to
    make(role, index=..., total=...) — e.g. to take its own data shard — and one that
    does not is called as make(role), exactly as before."""
    path = os.path.join(REPO, "experiments", name, "app.py")
    if not os.path.exists(path):
        sys.exit(f"no algorithm at {path}\n"
                 f"create experiments/{name}/app.py (copy experiments/_template/app.py)")
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location(f"algo_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1) a make(role) binding -> read ANY (untouched) algorithm
    if callable(getattr(mod, "make", None)):
        wants = set(inspect.signature(mod.make).parameters)      # does it want index/total?
        def f(role, index=None, total=None):
            kw = {k: v for k, v in (("index", index), ("total", total))
                  if k in wants and v is not None}
            return pl.adapt(mod.make(role, **kw), role)
        return f, "make(role) binding", mod
    own = [c for _, c in inspect.getmembers(mod, inspect.isclass) if c.__module__ == mod.__name__]
    # 2) an SdrApp subclass (advanced/stateful)
    subs = [c for c in own if issubclass(c, pl.SdrApp) and c is not pl.SdrApp]
    if subs:
        return (lambda role, index=None, total=None: subs[0](role)), \
               f"SdrApp subclass {subs[0].__name__}", mod
    # 3) a plain class exposing transmit()/receive()
    plains = [c for c in own if _has_io(c)]
    if plains:
        cls = plains[0]
        def f(role, index=None, total=None):
            try:
                obj = cls(role)
            except TypeError:
                obj = cls()
            return pl.adapt(obj, role)
        return f, f"plain class {cls.__name__}", mod
    # 4) module-level transmit()/receive() (single instance)
    if _has_io(mod):
        return (lambda role, index=None, total=None: pl.adapt(mod, role)), \
               "module-level transmit/receive", mod
    sys.exit(f"{path} exposes no algorithm interface "
             f"(need make(role), a class/SdrApp with transmit()/receive(), or module functions)")


def main():
    ap = argparse.ArgumentParser(description="run an uploaded algorithm over the PHY")
    ap.add_argument("--algo", required=True, help="folder name under experiments/")
    ap.add_argument("--role", default=None,
                    help="loopback | chain | gossip | multi | aircomp | tx | rx | relay | peer, "
                         "or any role the algorithm declares in ROLES (e.g. client / server). "
                         "Case-insensitive. Defaults to loopback, or to peer when --node is given.")
    ap.add_argument("--node", type=int, default=None,
                    help="run ONE node of a decentralised network as this process: which "
                         "node am I (0-based). Implies --role peer; --agents says how many "
                         "nodes there are and --topology which of them I exchange with.")
    ap.add_argument("--peers", default="",
                    help="comma-separated host per node, indexed by node id "
                         "(default: all 127.0.0.1, i.e. several terminals on this machine)")
    ap.add_argument("--peer-port", type=int, default=5800,
                    help="base TCP port for peer exchange; node k listens on peer-port + k")
    ap.add_argument("--peer-link", default=None, choices=["tcp", "wireless", "lora"],
                    help="how decentralised peers exchange: tcp (LAN / same machine), "
                         "wireless (the USRP radio), lora (the LoRa radio). Defaults to "
                         "the PHY --channel names — lora for --channel lora, else tcp.")
    ap.add_argument("--channel", default="ideal",
                    choices=["ideal", "sim", "usrp", "pyphy", "lora"],
                    help="WHICH PHY carries the payloads: ideal (radio-free, lossless), "
                         "usrp (drivers/usrp), lora (drivers/lora). HOW that "
                         "PHY is attached is the matching --<phy>-backend flag. Aliases: "
                         "sim=ideal, pyphy=usrp.")
    ap.add_argument("--usrp-backend", default="pyphy", choices=["pyphy", "radio"],
                    help="how the USRP PHY is attached: pyphy (the repo's real C++ modem "
                         "in-process, no radio) or radio (real USRPs — needs the two-host "
                         "role split, --role tx / --role rx)")
    ap.add_argument("--steps", type=int, default=5)
    ap.add_argument("--agents", type=int, default=4, help="number of agents (--role multi)")
    ap.add_argument("--relays", type=int, default=1,
                    help="number of relay nodes between the two ends (--role chain)")
    ap.add_argument("--topology", default="ring",
                    help="peer graph for --role gossip: ring (default) | full | an explicit "
                         "edge list such as 0-1,1-2,2-0 (any graph, e.g. a line or a star)")
    # ── the one RF knob both radios genuinely share: where in the band we sit ──
    ap.add_argument("--freq", type=float, default=915.0, metavar="MHz",
                    help="centre frequency in MHz (default 915). Both PHYs use it. The "
                         "US ISM band is 902-928 MHz and a value outside it is flagged.")
    # ── USRP PHY knobs (--channel usrp; drivers/usrp). We assemble this PHY, so
    #    the waveform, the rates, the coding and the gains are all ours to choose. ──
    ap.add_argument("--tx-gain", type=float, default=70.0, help="USRP transmit gain, dB")
    ap.add_argument("--rx-gain", type=float, default=30.0, help="USRP receive gain, dB")
    ap.add_argument("--samp-rate", type=float, default=2e6,
                    help="USRP sample rate in Hz (default 2e6)")
    # ARQ. Both PHYs run STOP-AND-WAIT — it is the only scheme the C++ PHY implements
    # (ACQ_stop_and_wait.hpp) and what the LoRa driver's framing does. What differs is
    # where the acknowledgement travels, and that is the USRP's choice to make: a LoRa
    # module acknowledges over the same radio, so it has no such knob.
    ap.add_argument("--ack-transport", default="tcp", choices=["tcp", "rf"],
                    help="USRP ACK path: tcp (a socket — no reverse RF needed) or rf (a "
                         "second RF path, RF B, which needs the reverse link and a "
                         "full-duplex box). The DATA always goes over RF either way.")
    ap.add_argument("--ack-timeout", type=int, default=3000, metavar="MS",
                    help="USRP: how long the source waits for an ACK before resending "
                         "(default 3000 ms). LoRa derives its own from the airtime of a "
                         "frame, which scales correctly across SF7..SF12.")
    ap.add_argument("--arq", default="stop-and-wait", choices=list(pl.ARQ_SCHEMES),
                    help="retransmission policy. Only stop-and-wait is implemented so "
                         "far (the C++ ACQ_stop_and_wait.hpp, and the LoRa driver's "
                         "framing); naming it means a run records the policy it used, "
                         "and a new scheme is an entry in ARQ_SCHEMES rather than an "
                         "edit everywhere. A PHY that lacks the one you ask for says so.")
    ap.add_argument("--max-attempts", type=int, default=None,
                    help="stop-and-wait: give up on a chunk after this many un-ACKed "
                         "sends. Applies to whichever PHY is selected; the default is "
                         "each PHY's own (USRP 50, LoRa 8 — LoRa retries are expensive "
                         "because each one costs a full frame of airtime).")
    ap.add_argument("--symbol-rate", type=float, default=1e6,
                    help="USRP symbol rate in Hz (default 1e6; sample rate / this is the "
                         "oversampling factor the receiver expects)")
    # LoRa channel knobs (--channel lora; drivers/lora)
    ap.add_argument("--lora-backend", default="sim", choices=["sim", "serial", "spi"],
                    help="how the SX1276 is attached: sim (no hardware), serial "
                         "(Arduino/Teensy on USB), spi (Pi with the radio on its SPI bus)")
    ap.add_argument("--lora-sf", type=int, default=9, choices=range(7, 13), metavar="7..12",
                    help="spreading factor: higher reaches further and costs "
                         "exponentially more airtime")
    ap.add_argument("--lora-cr", type=int, default=5, choices=[5, 6, 7, 8], metavar="5..8",
                    help="coding rate denominator (4/5 .. 4/8)")
    ap.add_argument("--lora-bw", type=int, default=125000,
                    choices=[125000, 250000, 500000],
                    help="bandwidth in Hz. The SX1276 supports more, but these are the "
                         "ones used in US 902-928 operation: 125 kHz (the normal "
                         "channels), 250 kHz, and 500 kHz (the wide channel). Doubling "
                         "the bandwidth halves the airtime and costs ~3 dB of "
                         "sensitivity.")
    ap.add_argument("--lora-power", type=int, default=14, help="TX power in dBm")
    ap.add_argument("--lora-port", default=None,
                    help="serial backend: the port the radio is on, e.g. /dev/ttyUSB0")
    ap.add_argument("--lora-verbose", action="store_true",
                    help="print fragments / retransmissions / airtime for every message")
    # pyphy channel knobs
    ap.add_argument("--scheme", "--modulation", dest="scheme", default="QPSK",
                    help="USRP modulation (BPSK/QPSK/8-PSK/16-QAM/DBPSK/DQPSK). "
                         "--modulation is the same flag.")
    ap.add_argument("--fec", default="turbo", choices=["", "conv", "ldpc", "turbo"],
                    help="USRP forward error correction. LoRa has its coding rate "
                         "(--lora-cr) and its CRC in the chip instead.")
    ap.add_argument("--snr-db", type=float, default=8.0)
    # radio knobs
    ap.add_argument("--radio", default="",
                    help="THIS node's USRP: serial=30CD424 (B210) or addr=192.168.40.2 "
                         "(X310/N210); a bare serial or IP also works. Sets both --tx-args "
                         "and --rx-args, so one flag names the radio this process owns. "
                         "Give --tx-args/--rx-args instead when a node has two radios.")
    ap.add_argument("--tx-args", default="", help="UHD device args of the transmit radio")
    ap.add_argument("--rx-args", default="", help="UHD device args of the receive radio")
    ap.add_argument("--ack-host", default="127.0.0.1")
    ap.add_argument("--net-host", default="127.0.0.1")
    ap.add_argument("--net-port", type=int, default=5700,
                    help="TCP reply port this node SERVES to the node upstream of it")
    ap.add_argument("--down-host", default=None,
                    help="relay only: host of the next hop downstream")
    ap.add_argument("--down-port", type=int, default=None,
                    help="relay only: that node's --net-port (default: --net-port + 1)")
    a = ap.parse_args()

    if a.role is None:                      # --node 3 alone means "I am one peer of the network"
        a.role = "peer" if a.node is not None else "loopback"

    if a.radio:                             # one flag names the radio this node process owns
        dev = device_args(a.radio)
        a.tx_args = a.tx_args or dev        # explicit --tx-args/--rx-args still win
        a.rx_args = a.rx_args or dev
        print(f"[run_algo] radio: tx_args={a.tx_args!r} rx_args={a.rx_args!r}")

    kind_sel = CHANNEL_ALIASES.get(a.channel, a.channel)
    warn_foreign_flags(a, ap, kind_sel)
    if kind_sel != "ideal" or a.node is not None or a.role in ("tx", "rx", "relay"):
        check_band(a.freq, kind_sel if kind_sel != "ideal" else "usrp")

    factory, how, mod = load_app_factory(a.algo)
    alias2t, t2alias = role_map(mod)
    transport, algo_role = resolve_role(a.role, alias2t, t2alias, a.algo)
    print(f"[run_algo] loaded experiments/{a.algo} via {how}")
    if algo_role is not None and algo_role != transport:
        print(f"[run_algo] role {algo_role!r} -> PHY end {transport!r}")

    ch = None
    if transport == "loopback":
        ch = build_channel(a)
        # both ends in one process, each built under its OWN role name
        ini = factory(t2alias["tx"], index=0, total=2)
        res = factory(t2alias["rx"], index=1, total=2)
        st = pl.run_loopback(ini, res, ch, steps=a.steps)
        print(f"[run_algo] loopback done: {st['delivered']}/{st['steps']} round-trips "
              f"delivered over channel={ch.name}")
    elif transport == "chain":
        # MULTI-HOP archetype: initiator -> N relays -> responder, every hop over the PHY
        ch = build_channel(a)
        n_chain = a.relays + 2
        nodes = ([factory(t2alias["tx"], index=0, total=n_chain)]
                 + [factory(t2alias["relay"], index=1 + i, total=n_chain)
                    for i in range(a.relays)]
                 + [factory(t2alias["rx"], index=n_chain - 1, total=n_chain)])
        st = pl.run_chain(nodes, ch, steps=a.steps)
        print(f"[run_algo] chain done ({a.relays} relay(s), {2*(a.relays+1)} hops/round-trip, "
              f"channel={ch.name}): {st['delivered']}/{st['steps']} round-trips delivered "
              f"over {st['hops']} hops")
    elif transport == "gossip":
        # DECENTRALISED archetype: N peers over a graph, no server and no access point
        ch = build_channel(a)
        peer = t2alias["tx"]                      # every node runs the same program
        try:
            pl.gossip_edges(a.agents, a.topology)          # validate before building nodes
        except ValueError as e:
            sys.exit(f"--topology: {e}")
        nodes = [factory(peer, index=i, total=a.agents) for i in range(a.agents)]
        st = pl.run_gossip(nodes, ch, rounds=a.steps, topology=a.topology)
        print(f"[run_algo] gossip done ({st['nodes']} peers, {a.topology} graph, "
              f"{st['edges']} edges, channel={ch.name}): {st['rounds']} rounds, "
              f"{st['exchanges']} exchanges, {st['hops']} PHY hops, {st['lost']} lost")
        if callable(getattr(mod, "report", None)):        # optional end-of-run summary
            mod.report([getattr(nd, "_src", nd) for nd in nodes])
    elif transport == "multi":
        ch = build_channel(a)
        agents = [factory("agent", index=i, total=a.agents)       # N independent agents
                  for i in range(a.agents)]
        st = pl.run_slotted(agents, ch, slots=a.steps)
        ptx = [getattr(g._src, "p_transmit", lambda: float("nan"))() for g in agents]
        n = a.agents
        opt = (1 - 1.0 / n) ** (n - 1)                        # slotted-ALOHA optimal throughput
        print(f"[run_algo] multi done ({n} agents, channel={ch.name}, {st['slots']} slots): "
              f"throughput={st['delivered']/max(1,st['slots']):.2f}/slot "
              f"(slotted-ALOHA optimum = {opt:.2f})  "
              f"collision-rate={st['collisions']/max(1,st['slots']):.2f}  "
              f"per-agent P(transmit)=[{', '.join(f'{p:.2f}' for p in ptx)}]")
    elif transport == "aircomp":
        # COMPUTE archetype: N sensors superpose -> AP recovers Σ v_i (the app owns the driver)
        if not callable(getattr(mod, "run", None)) or not callable(getattr(mod, "make", None)):
            sys.exit(f"experiments/{a.algo} needs make(role) + run(sensors, ...) for --role aircomp")
        sensors = [mod.make("sensor") for _ in range(a.agents)]
        mod.run(sensors, snr_db=a.snr_db, steps=a.steps)
    elif transport == "peer":
        # ONE node of a decentralised network, in its own process (its own terminal or
        # its own computer). It is both TX and RX, at different steps of the round.
        if a.node is None:
            sys.exit("--role peer needs --node K (which node am I, 0-based). "
                     "Use --agents for how many nodes there are, --topology for the graph.")
        hosts = [h.strip() for h in a.peers.split(",") if h.strip()] or None
        os.environ["UNION_ROLE"] = "peer"        # tell the algorithm it is running standalone
        # the PHY the experimenter picked decides how peers exchange, unless overridden
        kind = CHANNEL_ALIASES.get(a.channel, a.channel)
        peer_link = a.peer_link or ("lora" if kind == "lora" else "tcp")
        try:
            link = pl.PeerLink(node_id=a.node, n_nodes=a.agents, topology=a.topology,
                               peers=hosts, base_port=a.peer_port, link=peer_link,
                               tx_args=a.tx_args, rx_args=a.rx_args, scheme=a.scheme,
                               lora_backend=a.lora_backend, lora_port=a.lora_port,
                               lora_sf=a.lora_sf, lora_cr=a.lora_cr, lora_bw=a.lora_bw,
                               lora_power=a.lora_power, lora_snr_db=a.snr_db)
        except ValueError as e:
            sys.exit(str(e))
        app = factory(algo_role, index=a.node, total=a.agents)
        n = 0
        try:
            while n < a.steps and link.step(app):
                n += 1
        finally:
            link.close()
        print(f"[run_algo] node {a.node}/{a.agents} done: {n} rounds over the "
              f"{a.topology} graph ({peer_link})")
        if callable(getattr(mod, "report", None)):
            mod.report([getattr(app, "_src", app)])
    else:
        # ONE end of the link, in its own process (the other end runs on the peer host).
        # The link is driven by the PHY end (tx/rx/relay); the app is built under its own
        # name. WHICH PHY carries it is --channel, exactly as for the group runners: the
        # roles are the middleware's, not any one driver's.
        link = build_link(a, transport)
        app = factory(algo_role)
        n = 0
        while link.step(app):
            n += 1
            if transport == "tx" and n >= a.steps:
                break
        print(f"[run_algo] radio {algo_role} ({transport}) done: {n} steps")

    # What did the PHY actually cost? For LoRa this is the headline number — the
    # airtime an experiment would really have spent on the air.
    if ch is not None and hasattr(ch, "stats"):
        print(f"[run_algo] {ch.name} PHY: {ch.stats()}")


if __name__ == "__main__":
    main()
