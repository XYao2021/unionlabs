#!/usr/bin/env python3
"""
run_algo.py — load an uploaded algorithm from algorithms/<name>/app.py and run it
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

        ROLES = {"client": "tx", "server": "rx"}      # in algorithms/<name>/app.py

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
import topology as tp


def _has_io(c):
    return any(hasattr(c, m) for m in ("transmit", "produce"))


# ── where algorithms live. The same rule as topologies: the shared workspace
#    wins over the repo checkout, so a testbed session runs the algorithms the
#    account shares, and a laptop with no /workspace runs the repo's own. ──
def algo_search_path():
    out = []
    env = os.environ.get("UNION_ALGO_DIR", "")
    if env:
        out.append(env)
    out.append("/workspace/experiments/algorithms")
    out.append(os.path.join(REPO, "workspace", "experiments", "algorithms"))
    return out


def algo_path(name):
    """-> the app.py for this algorithm, or None."""
    for d in algo_search_path():
        p = os.path.join(d, name, "app.py")
        if os.path.isfile(p):
            return p
    return None


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
        # Validate the keys even here, so a typo is caught wherever it is typed — then
        # refuse rather than drop them: the in-process pyphy backend is the modem's DSP
        # called directly, not the CLI, so a modem option cannot reach it. Silently
        # ignoring them would produce a run that looks configured and is not.
        if _usrp_extra(a):
            sys.exit("--usrp-set configures the C++ modem process, which the default "
                     "--usrp-backend pyphy does not start (it calls the DSP in-process). "
                     "Use --usrp-backend radio with --role tx/rx, or drop --usrp-set.")
        return pl.make_channel("pyphy", scheme=a.scheme, fec=(a.fec or None),
                               snr_db=a.snr_db)
    if kind == "lora":
        return pl.make_channel("lora", backend=a.lora_backend, sf=a.lora_sf,
                               cr=a.lora_cr, bw_hz=a.lora_bw, power_dbm=a.lora_power,
                               freq_hz=int(a.freq * 1e6), snr_db=a.snr_db,
                               port=a.lora_port, verbose=a.lora_verbose,
                               max_attempts=(8 if a.max_attempts is None
                                             else a.max_attempts), arq=a.arq,
                               **_lora_extra(a))
    return pl.make_channel("ideal")


# Knobs that belong to ONE PHY and mean nothing to the others. Different physical
# layers have genuinely different logic — the USRP's waveform, FEC and acknowledgement
# path are ours to choose, while a LoRa chip embeds its modulation and CRC and offers
# its own spreading factor instead. Passing one PHY's knob to another is always a
# mistake, and silently ignoring it would hide a wrong experiment.
PHY_ONLY_FLAGS = {
    "usrp": {"scheme": "--scheme", "fec": "--fec", "waveform": "--waveform",
             "usrp_set": "--usrp-set",
             "tx_subdev": "--tx-subdev", "rx_subdev": "--rx-subdev",
             "tx_ant": "--tx-ant", "rx_ant": "--rx-ant",
             "ack_transport": "--ack-transport", "ack_timeout": "--ack-timeout",
             "samp_rate": "--samp-rate", "symbol_rate": "--symbol-rate",
             "tx_gain": "--tx-gain", "rx_gain": "--rx-gain"},
    "lora": {"lora_set": "--lora-set", "lora_sf": "--lora-sf", "lora_cr": "--lora-cr", "lora_bw": "--lora-bw",
             "lora_power": "--lora-power", "lora_backend": "--lora-backend",
             "lora_port": "--lora-port"},
}


def _usrp_extra(a):
    """KEY=VALUE for the USRP, checked against sdr.py's OPTIONS — the modem's own
    auto-generated registry of every option it has."""
    pairs = _kv_pairs(getattr(a, "usrp_set", []), "usrp")
    if not pairs:
        return {}
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "drivers", "usrp", "python"))
    import sdr
    known = {k.replace("-", "_") for k in sdr.OPTIONS}
    _check_keys(pairs, known, "usrp", "docs/PARAMETERS.md (or sdr_system --help)")
    return pairs


def _lora_extra(a):
    """KEY=VALUE for LoRa, checked against the driver's own signature."""
    pairs = _kv_pairs(getattr(a, "lora_set", []), "lora")
    if not pairs:
        return {}
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "drivers", "lora", "python"))
    import inspect, lora_driver
    known = set(inspect.signature(lora_driver.LoRaChannel.__init__).parameters) - {"self"}
    _check_keys(pairs, known, "lora", "drivers/lora/python/lora_driver.py")
    return pairs


def _kv_pairs(items, phy):
    """--usrp-set / --lora-set: KEY=VALUE pairs straight onto a PHY's own variables.

    The named flags above cover what most experiments touch. These cover EVERYTHING
    else — the USRP modem alone has ~100 options — without this file having to list
    them, which would rot the moment the PHY gains one. Keys are the PHY's own names
    (either spelling: det-mult or det_mult), and an unknown key is refused rather than
    ignored, because a typo that silently changes nothing is a wrong experiment that
    looks like a right one.
    """
    out = {}
    for item in items or []:
        if "=" not in item:
            sys.exit(f"--{phy}-set expects KEY=VALUE (got {item!r})")
        k, v = item.split("=", 1)
        k = k.strip().replace("-", "_")
        t = v.strip()
        if t.lower() in ("true", "false"):
            val = (t.lower() == "true")
        else:
            try:
                val = int(t)
            except ValueError:
                try:
                    val = float(t)
                except ValueError:
                    val = t
        out[k] = val
    return out


def _check_keys(pairs, known, phy, hint):
    """Refuse a key the PHY does not have, and say what it might have meant."""
    for k in pairs:
        if k in known:
            continue
        near = sorted(n for n in known if k[:4] and n.startswith(k[:4]))
        sys.exit(f"--{phy}-set: {phy} has no variable {k!r}"
                 + (f" — did you mean {', '.join(near[:4])}?" if near else "")
                 + f"\n  see {hint}")


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


def uses_real_radio(a, kind, role):
    """Is this run actually driving hardware? Simulation-only knobs mean nothing if so."""
    if kind == "usrp" and a.usrp_backend == "radio":
        return "the USRP radio backend"
    if kind == "lora" and a.lora_backend in ("serial", "spi"):
        return f"the LoRa {a.lora_backend} backend"
    # the point-to-point roles with no --channel default to the USRP link (see build_link)
    if kind == "ideal" and role.lower() in ("tx", "rx", "relay"):
        return "the USRP link"
    return None


def warn_simulation_only_flags(a, ap, kind, role):
    """--snr-db sets the noise a SIMULATED channel adds. A real link's SNR is an outcome
    of gain, distance and interference — nothing here can dial it in, so saying so beats
    letting the flag look effective."""
    hw = uses_real_radio(a, kind, role)
    if hw and a.snr_db != ap.get_default("snr_db"):
        knob = ("--tx-gain / --rx-gain" if "USRP" in hw else "--lora-power / --lora-sf")
        print(f"[run_algo] NOTE: --snr-db is a simulation knob and does nothing on {hw}. "
              f"A real link's SNR is measured, not set — drive it with {knob}, and read the "
              f"SNR the receiver reports.")


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
    link_kind = getattr(a, "link", "auto")
    if link_kind == "tcp":
        # NO RADIO IN THE LOOP. Both directions over TCP/IP, which is what a session
        # with no antenna attached actually has — and what fl.py calls
        # --uplink tcp --downlink tcp. The hub collects one payload from every client
        # before it answers, so a server that averages really does average N models.
        if transport == "relay":
            sys.exit("--link tcp does not carry a relay yet: a store-and-forward middle "
                     "node exists for the radio path (--link usrp), where it is what "
                     "gets a client out of the server's range. Over TCP/IP the client "
                     "can reach the server directly — link it straight to the hub.")
        n_clients = a.clients if a.clients else 1
        return pl.TcpStar(role=transport, hub_host=a.net_host, hub_port=a.net_port,
                          clients=n_clients, node_id=_hub_index(a))
    if kind == "lora" or link_kind == "lora":
        sys.path.insert(0, os.path.join(REPO, "drivers", "lora", "python"))
        import lora_driver
        return lora_driver.LoRaLink(role=transport, node=(int(a.node) if a.node is not None else 0),
                                    backend=a.lora_backend, port=a.lora_port,
                                    sf=a.lora_sf, cr=a.lora_cr, bw_hz=a.lora_bw,
                                    power_dbm=a.lora_power, freq_hz=int(a.freq * 1e6),
                                    snr_db=a.snr_db, verbose=a.lora_verbose,
                                    max_attempts=(8 if a.max_attempts is None
                                                  else a.max_attempts), arq=a.arq)
    if link_kind == "chain":
        # A RELAY WHOSE TWO HOPS DIFFER. Each leg speaks what its neighbour speaks, so
        # the media are the hops' business, not the node's.
        up = a.up_medium or "wireless"
        down = a.down_medium or "tcp"
        radio = _radio_link(a, "relay") if "wireless" in (up, down) else None
        return pl.ChainRelay(up_medium=up, down_medium=down, radio=radio,
                             serve_host="0.0.0.0", serve_port=a.net_port,
                             down_host=(a.down_host or a.net_host),
                             down_port=(a.down_port or a.net_port + 1),
                             node_id=_hub_index(a))
    if kind == "ideal" and link_kind == "auto":
        # --channel has never applied to the point-to-point roles, which always meant
        # the USRP link. Keep every existing command working, and say which PHY it got.
        print(f"[run_algo] --role {transport} with no --channel: using the USRP link "
              f"(drivers/usrp). Add --channel lora for the LoRa link, or --link tcp to "
              f"run the same roles over TCP/IP with no radio.")
    return _radio_link(a, transport)


def _hub_index(a):
    """This node's index among the clients of the node it dials — what it stamps its
    frames with. Falls back to its place in its own role group, then to --node."""
    for attr in ("hub_index", "role_index"):
        v = getattr(a, attr, None)
        if v is not None:
            return int(v)
    return int(a.node or 0)


def _radio_link(a, transport):
    """The USRP link: request over the air, reply over TCP. Its own function because a
    ChainRelay borrows it for whichever of its two hops is wireless."""
    return pl.RadioRoundTrip(role=transport, tx_args=a.tx_args, rx_args=a.rx_args,
                             ack_host=a.ack_host, ack_port=a.ack_port,
                             net_host=a.net_host,
                             net_port=a.net_port, scheme=a.scheme, waveform=a.waveform,
                             tx_subdev=a.tx_subdev, rx_subdev=a.rx_subdev,
                             tx_ant=a.tx_ant, rx_ant=a.rx_ant,
                             extra_cfg=_usrp_extra(a),
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
    sys.exit(f"--role {requested!r} is not a role of algorithms/{algo}\n"
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
    path = algo_path(name)
    if path is None:
        sys.exit(f"no algorithm {name!r} — looked in {', '.join(algo_search_path())}\n"
                 f"create algorithms/{name}/app.py (copy algorithms/_template/app.py)")
    sys.path.insert(0, os.path.dirname(path))
    # An algorithm's own sys.path escapes ("..", ..., "drivers") are written
    # relative to its folder, and a copy running from /workspace/experiments/
    # algorithms has no drivers/ above it — the escape points at nothing. A
    # nonexistent sys.path entry is harmless, so make the imports resolve by
    # APPENDING the repo's driver paths here (append, not insert: an algorithm
    # that ships its own copy of a module still wins).
    for d in (os.path.join(REPO, "drivers", "usrp", "python"),
              os.path.join(REPO, "drivers", "lora", "python")):
        if d not in sys.path:
            sys.path.append(d)
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


# ══════════════════════════════════════════════════════════════════════════════
#  --topology as a FILE: the wiring of a whole experiment, read by every node
# ══════════════════════════════════════════════════════════════════════════════
#  One file says who the nodes are, what radio each owns, which connector and RF
#  channel it uses, which port it listens on, and how every link is carried. Each node
#  asks it "which node am I" (--node) and gets the rest of its command line.
#
#  PRECEDENCE: anything typed on the command line wins over the file. "Typed" means
#  "differs from the parser's own default" rather than "appears in argv", because
#  run.sh passes --channel/--steps on EVERY invocation — comparing against argv would
#  make the file lose to flags nobody typed.
MEDIUM_TRANSPORT = {"tcp": "tcp", "wireless": "usrp", "lora": "lora"}

# topology "defaults" -> the flag each one sets. Only knobs that mean the same thing to
# every node belong here; anything per-node is a property of the node.
TOPO_DEFAULTS = {"channel": "channel", "steps": "steps", "scheme": "scheme",
                 "fec": "fec", "waveform": "waveform", "samp_rate": "samp_rate",
                 "symbol_rate": "symbol_rate", "snr_db": "snr_db",
                 "sim_snr_db": "snr_db", "freq_mhz": "freq", "peer_port": "peer_port",
                 "arq": "arq", "max_attempts": "max_attempts",
                 "ack_transport": "ack_transport", "ack_timeout": "ack_timeout"}


def _typed_flags(ap, argv=None):
    """Which settings did the experimenter actually TYPE? A flag whose value happens to
    equal the parser default is still a choice ('--steps 5' when 5 is the default), so
    argv is the authority; the value comparison below then catches anything reaching
    run_algo another way."""
    argv = sys.argv[1:] if argv is None else argv
    opt = {s: act.dest for act in ap._actions for s in act.option_strings}
    return {opt[t.split("=", 1)[0]] for t in argv
            if t.startswith("-") and t.split("=", 1)[0] in opt}


def _typed(ap, a, dest):
    """Did the experimenter actually choose this, or is it just the parser's default?"""
    if dest in getattr(a, "_typed", ()):
        return True
    return getattr(a, dest, None) != ap.get_default(dest)


def _set(ap, a, dest, value):
    """Apply a value from the file, unless the command line already said otherwise."""
    if value is None or _typed(ap, a, dest):
        return False
    setattr(a, dest, value)
    return True


def _peer_base_port(topo):
    """PeerLink gives node k the port base+k, so a file that lists per-node peer ports
    has to agree with that or two nodes end up dialling the same socket. Check it here,
    where the message can name both nodes, instead of at connect time."""
    bases = {}
    for nd in topo.nodes:
        if "peer" in nd.ports:
            bases.setdefault(nd.ports["peer"] - nd.index, []).append(nd.id)
    if not bases:
        return None
    if len(bases) > 1:
        got = "; ".join(f"base {b} from {', '.join(ids)}" for b, ids in sorted(bases.items()))
        sys.exit(f"--topology {topo.name}: peer ports must be base+index (node k listens "
                 f"on base+k), but this file implies more than one base — {got}")
    return next(iter(bases))


def _node_media(topo, nd):
    """(what this node TRANSMITS over, what it RECEIVES over) across all of its links.

    A link is ordered from -> to, and its two directions can use different media: the
    RX-only N210 rig is exactly {"up": "wireless", "down": "tcp"} — the client transmits
    over the air, the server answers over TCP."""
    out, inc = set(), set()
    for ln in topo.links_of(nd):
        if ln.a.id == nd.id:
            out.add(ln.up); inc.add(ln.down)
        else:
            out.add(ln.down); inc.add(ln.up)
    return out, inc


def _peer_transport(topo, nd):
    """Which transport carries a DECENTRALISED node's exchanges: tcp | usrp | lora.

    A peer is the one case where both directions may be wireless: PeerLink walks the
    shared edge schedule, so one end transmits while the other listens and then they
    swap — never at the same instant, which is all a half-duplex radio can do. The
    point-to-point roles resolve their media per HOP instead (see apply_topology), since
    a relay's two hops need not agree."""
    out, inc = _node_media(topo, nd)
    media = out | inc
    if media == {"tcp"}:
        return "tcp"
    if media == {"lora"}:
        return "lora"
    if media <= {"wireless", "tcp"} and "wireless" in media:
        return "usrp"
    sys.exit(f"--topology {topo.name}: node {nd.id} has links over "
             f"{', '.join(sorted(media))} — one node is attached one way. Split it into "
             f"two topologies, or give its links a single medium.")


def apply_topology(ap, a):
    """Read the --topology file, if it names one, and fill this run's settings in.

    Returns the Topology (or None when --topology is ring / full / an edge list, which
    keeps every existing command working untouched)."""
    try:
        topo = tp.load_if_file(a.topology)
    except tp.TopologyError as e:
        sys.exit(f"--topology: {e}")
    if topo is None:
        if a.node is not None and not str(a.node).strip().isdigit():
            sys.exit(f"--node {a.node!r} is a name, but --topology {a.topology!r} is not "
                     f"a file that could define it. Give a topology file, or --node K.")
        return None

    print(f"[run_algo] topology {topo.name} ({len(topo.nodes)} nodes, "
          f"{len(topo.links)} links) from {topo.path}")
    if topo.algo and _typed(ap, a, "algo") and a.algo != topo.algo:
        print(f"[run_algo] NOTE: this file was written for --algo {topo.algo}, "
              f"running it with --algo {a.algo}")

    for key, dest in TOPO_DEFAULTS.items():         # experiment-wide knobs
        if key in topo.defaults:
            _set(ap, a, dest, topo.defaults[key])
    _set(ap, a, "agents", len(topo.nodes))
    base = _peer_base_port(topo)
    if base is not None:
        _set(ap, a, "peer_port", base)
    # the graph itself, in the edge-list spelling every runner already understands.
    # FILE ORDER is the schedule: one exchange at a time, so a half-duplex radio is
    # never asked to transmit and receive at once.
    a.topology = topo.edge_spec()

    if a.node is None:                              # a whole-network run in one process
        return topo
    try:
        nd = topo.node(a.node)
    except tp.TopologyError as e:
        sys.exit(f"--node: {e}")
    a.node = nd.index
    _set(ap, a, "role", nd.role)
    role_index, role_count = topo.role_group(nd)
    a.role_index = role_index
    if "ack" in nd.ports:            # what THIS node binds, when it is the sink
        _set(ap, a, "ack_port", nd.ports["ack"])

    # what the algorithm needs to know about its own place in the network. The
    # middleware publishes the facts; each algorithm reads the ones it cares about
    # (fl: which shard am I and how many clients does the server average over).
    # How many nodes ORIGINATE data — the client count a server aggregates over, and the
    # number of shards the data is split into. A node that both receives and sends is a
    # relay carrying somebody else's payload, not a source of its own, so counting every
    # node that dials would make a 3-node chain look like 2 clients. Derived from the
    # graph, so no algorithm vocabulary leaks into the middleware.
    dialers = sum(1 for x in topo.nodes
                  if any(ln.a.id == x.id for ln in topo.links_of(x))
                  and not any(ln.b.id == x.id for ln in topo.links_of(x)))
    os.environ.update({"UNION_NODE": nd.id, "UNION_INDEX": str(nd.index),
                       "UNION_NODES": str(len(topo.nodes)),
                       "UNION_ROLE_INDEX": str(role_index),
                       "UNION_ROLE_COUNT": str(role_count),
                       "UNION_CLIENTS": str(max(1, dialers)),
                       "UNION_TOPOLOGY": topo.name})

    is_peer = (a.role or nd.role) in ("peer", "gossip")
    transport = _peer_transport(topo, nd) if is_peer else None
    _set(ap, a, "peers", ",".join(x.dial_host() or "127.0.0.1" for x in topo.nodes))
    # A peer normally listens on base+k, so everyone can work out everyone else's port.
    # A published port breaks that arithmetic — a NodePort is whatever the cluster gave
    # out — so the dial ports travel as a list whenever they are not base+k.
    base = a.peer_port
    dial = [x.dial_port("peer", base + x.index) for x in topo.nodes]
    if any(p != base + i for i, p in enumerate(dial)):
        _set(ap, a, "peer_ports", ",".join(str(p) for p in dial))
    uses_rf = False

    if is_peer:
        _set(ap, a, "peer_link", {"tcp": "tcp", "usrp": "wireless",
                                  "lora": "lora"}[transport])
        missing = [x.id for x in topo.peers_of(nd) if not x.dial_host()]
        if missing and not _typed(ap, a, "peers"):
            print(f"[run_algo] NOTE: {', '.join(missing)} ha"
                  f"{'s' if len(missing) == 1 else 've'} no host in {topo.name}, so this "
                  f"node will look for them on 127.0.0.1. That is right for several "
                  f"processes on one machine; give --peers <host per node> otherwise.")
    else:
        # ── a point-to-point role: EVERY HOP HAS ITS OWN MEDIUM ──
        # A link is ordered from -> to, so `up` is the medium that carries the DATA and
        # `down` the one that carries the reply. Links where this node is `to` bring
        # data in; links where it is `from` take data out. A relay has both, and the
        # two need not agree — that is the whole point.
        in_links = [ln for ln in topo.links_of(nd) if ln.b.id == nd.id]
        out_links = [ln for ln in topo.links_of(nd) if ln.a.id == nd.id]
        arrives = {ln.up for ln in in_links}
        leaves = {ln.up for ln in out_links}
        replies = {ln.down for ln in in_links} | {ln.down for ln in out_links}
        for what, media in (("receives on", arrives), ("transmits on", leaves)):
            if len(media) > 1:
                sys.exit(f"--topology {topo.name}: node {nd.id} {what} "
                         f"{', '.join(sorted(media))} at once. A node is attached one "
                         f"way per direction; give those links one medium.")
        all_media = arrives | leaves
        if all_media != {"lora"} and replies - {"tcp"}:
            sys.exit(f"--topology {topo.name}: node {nd.id} has a link whose DOWN "
                     f"direction is {', '.join(sorted(replies - {'tcp'}))}. Every "
                     f"transport here carries the reply over TCP — the RX-only N210 "
                     f"never transmits, which is the reason the split exists. Set that "
                     f"link's down medium to tcp.")
        med_in = next(iter(arrives)) if arrives else None
        med_out = next(iter(leaves)) if leaves else None
        uses_rf = "wireless" in all_media
        family = {"tcp": "tcp", "wireless": "usrp", "lora": "lora"}

        # Who am I to the node I dial? Its hub counts its clients 0..N-1 and tracks which
        # of them have finished BY THAT INDEX, so a frame stamped with a node index the
        # hub never assigned is discarded — and the hub then waits for a goodbye that
        # already came. This is that index, and it is not the same number as --node.
        if out_links:
            nxt = out_links[0].b
            sources = [ln.a.id for ln in topo.links_of(nxt) if ln.b.id == nxt.id]
            a.hub_index = sources.index(nd.id) if nd.id in sources else 0

        if in_links and out_links:
            # A RELAY: one hop in, one hop out, each on its own medium. Both wireless is
            # the shape RadioRoundTrip already carries; anything else is ChainRelay.
            a.up_medium, a.down_medium = med_in, med_out
            if "lora" in all_media:
                _set(ap, a, "link", "lora")
            elif med_in == "wireless" and med_out == "wireless":
                _set(ap, a, "link", "usrp")
            else:
                _set(ap, a, "link", "chain")
            _set(ap, a, "net_host", nd.host or "0.0.0.0")   # what UPSTREAM dials
            _set(ap, a, "net_port", nd.port("net", 5700))
            _set(ap, a, "down_host", nxt.dial_host() or "127.0.0.1")
            # the next hop's port: this node may name it explicitly (ports.down), else it
            # is the port that node PUBLISHES (which is the one it serves on, unless a
            # NodePort renumbered it)
            _set(ap, a, "down_port", nd.ports.get("down")
                 or nxt.dial_port("net", nd.port("net", 5700) + 1))
            if med_out == "wireless":            # this relay transmits to the next hop
                _set(ap, a, "ack_host", nxt.dial_host() or "127.0.0.1")
                _set(ap, a, "ack_port", nxt.dial_port("ack", 5599))
        elif out_links:
            # A CLIENT: transmits to the next node and reads its reply
            hub = out_links[0].b
            _set(ap, a, "link", family[med_out])
            if not hub.host and not _typed(ap, a, "net_host"):
                print(f"[run_algo] NOTE: {hub.id} has no host in {topo.name}, so {nd.id} "
                      f"will dial 127.0.0.1. That is right for both on one machine; pass "
                      f"--net-host <address> when {hub.id} is somewhere else (a session "
                      f"pod's address changes every session, which is why a file should "
                      f"not carry it).")
            _set(ap, a, "net_host", hub.dial_host() or "127.0.0.1")
            _set(ap, a, "net_port", hub.dial_port("net", 5700))
            # THE ARQ ACK IS THE SINK'S SOCKET. main.cpp: sink_arq calls accept_one(),
            # source_arq calls connect_to() — so the receiver listens and the transmitter
            # dials. A transmitter therefore takes the port of the node it is dialling,
            # not its own; taking its own is how two nodes end up on different ports and
            # the ACK never arrives.
            _set(ap, a, "ack_host", hub.dial_host() or "127.0.0.1")
            if med_out == "wireless":
                _set(ap, a, "ack_port", hub.dial_port("ack", 5599))
        else:
            # A SERVER: receives, aggregates, answers. Bind the address this node is
            # reachable at — a client on another machine cannot reach 127.0.0.1.
            _set(ap, a, "link", family[med_in])
            _set(ap, a, "net_host", nd.host or "127.0.0.1")
            _set(ap, a, "net_port", nd.port("net", 5700))
            _set(ap, a, "clients", max(1, len(in_links)))

    # the radio this node owns, and the CONNECTOR each direction uses
    if nd.radio:
        args = nd.radio["args"]
        if nd.can_tx():
            _set(ap, a, "tx_args", args)
            _set(ap, a, "tx_ant", nd.side("tx", "ant"))
            _set(ap, a, "tx_subdev", nd.side("tx", "subdev"))
            _set(ap, a, "tx_gain", nd.side("tx", "gain"))
        if nd.can_rx():
            _set(ap, a, "rx_args", args)
            _set(ap, a, "rx_ant", nd.side("rx", "ant"))
            _set(ap, a, "rx_subdev", nd.side("rx", "subdev"))
            _set(ap, a, "rx_gain", nd.side("rx", "gain"))
        freq = nd.side("tx", "freq_mhz") or nd.side("rx", "freq_mhz")
        _set(ap, a, "freq", freq)
        # a run that really drives a USRP should say so, so the band check and the
        # simulation-only warnings apply to it
        if transport == "usrp" or uses_rf:
            _set(ap, a, "channel", "usrp")
            _set(ap, a, "usrp_backend", "radio")
    if nd.lora:
        for key, dest in (("backend", "lora_backend"), ("port", "lora_port"),
                          ("sf", "lora_sf"), ("cr", "lora_cr"), ("bw", "lora_bw"),
                          ("power", "lora_power")):
            if key in nd.lora:
                _set(ap, a, dest, nd.lora[key])
    return topo


def print_plan(a, topo):
    """What this node resolved to — the answer to 'is the file wired the way I think?'
    without spending a run to find out."""
    if topo is not None:
        print(topo.summary())
    print("\n  this node")
    peer = (a.role or "").lower() in ("peer", "gossip")
    radio = bool(a.tx_args or a.rx_args)
    fields = [("algo", a.algo), ("node", a.node), ("role", a.role),
              ("agents", a.agents), ("steps", a.steps), ("graph", a.topology),
              ("channel", a.channel), ("link", None if peer else a.link),
              ("peer-link", a.peer_link if peer else None),
              ("peers", a.peers if peer else None),
              ("peer-port", a.peer_port if peer else None),
              ("peer-dial", a.peer_ports if peer and a.peer_ports else None),
              ("net", None if peer else f"{a.net_host}:{a.net_port}"),
              ("hop in", getattr(a, "up_medium", None)),
              ("hop out", getattr(a, "down_medium", None)),
              ("ack", f"{a.ack_host}:{a.ack_port}" if radio else None),
              ("down", f"{a.down_host}:{a.down_port}" if a.down_host else None),
              ("clients", a.clients), ("freq-MHz", a.freq if radio else None),
              ("tx", f"{a.tx_args} ant={a.tx_ant} subdev={a.tx_subdev} "
                     f"gain={a.tx_gain}" if a.tx_args else None),
              ("rx", f"{a.rx_args} ant={a.rx_ant} subdev={a.rx_subdev} "
                     f"gain={a.rx_gain}" if a.rx_args else None)]
    for k, v in fields:
        if v is not None:
            print(f"    {k:<10} {v}")
    env = {k: v for k, v in os.environ.items() if k.startswith("UNION_")}
    if env:
        print("    env        " + "  ".join(f"{k}={v}" for k, v in sorted(env.items())))


def build_parser():
    """The CLI, as its own function so it can be inspected and tested without
    running an experiment. main() is a thin wrapper over it."""
    ap = argparse.ArgumentParser(description="run an uploaded algorithm over the PHY")
    ap.add_argument("--algo", required=True,
                help="folder name under workspace/experiments/algorithms/")
    ap.add_argument("--role", default=None,
                    help="loopback | chain | gossip | multi | aircomp | tx | rx | relay | peer, "
                         "or any role the algorithm declares in ROLES (e.g. client / server). "
                         "Case-insensitive. Defaults to loopback, or to peer when --node is given.")
    ap.add_argument("--node", default=None, metavar="K|NAME",
                    help="which node of the experiment am I: a 0-based index, or the id "
                         "of a node in the --topology file. Implies --role peer unless "
                         "the file gives this node a role; --agents says how many nodes "
                         "there are and --topology which of them I exchange with.")
    ap.add_argument("--link", default="auto",
                    choices=["auto", "tcp", "usrp", "lora", "chain"],
                    help="how the two ends of a point-to-point role (--role tx/rx/relay, "
                         "or an algorithm's client/server) are attached: tcp (plain "
                         "TCP/IP — no radio at all), usrp (over the air, reply over TCP), "
                         "lora, or chain (a relay whose two hops use DIFFERENT media — "
                         "see --up-medium/--down-medium). 'auto' follows --channel, which "
                         "is the older behaviour. A --topology file sets this per link.")
    ap.add_argument("--up-medium", default=None, choices=["tcp", "wireless"],
                    help="--link chain: what carries data INTO this relay (default "
                         "wireless). From a topology file this is the `up` medium of the "
                         "link that ends here.")
    ap.add_argument("--down-medium", default=None, choices=["tcp", "wireless"],
                    help="--link chain: what carries data OUT of this relay towards the "
                         "next hop (default tcp). From a topology file this is the `up` "
                         "medium of the link that starts here.")
    ap.add_argument("--clients", type=int, default=None,
                    help="--link tcp, server end: how many clients the hub collects from "
                         "before it answers (one FedAvg round). Taken from the topology "
                         "file when there is one.")
    ap.add_argument("--print-plan", action="store_true",
                    help="print the topology and the settings this node resolves to, "
                         "then exit without running anything")
    ap.add_argument("--peers", default="",
                    help="comma-separated host per node, indexed by node id "
                         "(default: all 127.0.0.1, i.e. several terminals on this machine)")
    ap.add_argument("--peer-port", type=int, default=5800,
                    help="base TCP port for peer exchange; node k listens on peer-port + k")
    ap.add_argument("--peer-ports", default="",
                    help="comma-separated port per node to DIAL, indexed by node id, when "
                         "they are not peer-port + k — which is what a NodePort does to "
                         "them. Each node still LISTENS on peer-port + its own id.")
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
                    help="THE WIRING. A built-in graph (ring | full), an explicit edge "
                         "list such as 0-1,1-2,2-0, or the name of a topology file in "
                         "/workspace/experiments/topologies (e.g. fl-star-tcp) — which "
                         "additionally says what radio each node owns, which connector "
                         "it uses, which port it listens on and how each link is "
                         "carried. Anything typed on the command line wins over it.")
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
    # Named exactly as the modem names them (drivers/usrp/src/main.cpp), so a flag found
    # in PARAMETERS.md or a radio.sh command is typed identically here.
    ap.add_argument("--usrp-set", action="append", metavar="KEY=VALUE", default=[],
                    help="any other USRP modem variable, by its own name — repeatable. "
                         "e.g. --usrp-set det_mult=5 --usrp-set viz=true. "
                         "Full list: docs/PARAMETERS.md or sdr_system --help")
    ap.add_argument("--lora-set", action="append", metavar="KEY=VALUE", default=[],
                    help="any other LoRa driver variable, by its own name — repeatable. "
                         "e.g. --lora-set seed=7 --lora-set reply_timeout=60")
    ap.add_argument("--waveform", default="sc", choices=["sc", "ofdm"],
                    help="USRP waveform: single-carrier, or OFDM (CFO-robust — worth "
                         "trying on a marginal link). Both ends must agree.")
    ap.add_argument("--tx-subdev", default="A:A", metavar="SPEC",
                    help="USRP transmit RF channel (B210: A:A = RF A, A:B = RF B)")
    ap.add_argument("--rx-subdev", default="A:0", metavar="SPEC",
                    help="USRP receive RF channel (N210/X310: A:0)")
    ap.add_argument("--tx-ant", default="TX/RX", metavar="PORT",
                    help="USRP transmit connector (default TX/RX)")
    ap.add_argument("--rx-ant", default="RX2", metavar="PORT",
                    help="USRP receive connector (default RX2)")
    ap.add_argument("--scheme", "--modulation", dest="scheme", default="QPSK",
                    help="USRP modulation (BPSK/QPSK/8-PSK/16-QAM/DBPSK/DQPSK). "
                         "--modulation is the same flag.")
    ap.add_argument("--fec", default="turbo", choices=["", "conv", "ldpc", "turbo"],
                    help="USRP forward error correction. LoRa has its coding rate "
                         "(--lora-cr) and its CRC in the chip instead.")
    ap.add_argument("--sim-snr-db", "--snr-db", dest="snr_db", type=float, default=8.0,
                    help="SIMULATION ONLY (--snr-db is the older spelling): the link SNR the simulated channels model — "
                         "--usrp-backend pyphy adds AWGN at this Es/N0, and --lora-backend "
                         "sim tests it against the spreading factor's demodulator floor. On "
                         "REAL hardware SNR is MEASURED, not set: drive the link with "
                         "--tx-gain/--rx-gain (USRP) or --lora-power/--lora-sf (LoRa) and read "
                         "the SNR the receiver reports back.")
    # radio knobs
    ap.add_argument("--radio", default="",
                    help="THIS node's USRP: serial=30CD424 (B210) or addr=192.168.40.2 "
                         "(X310/N210); a bare serial or IP also works. Sets both --tx-args "
                         "and --rx-args, so one flag names the radio this process owns. "
                         "Give --tx-args/--rx-args instead when a node has two radios.")
    ap.add_argument("--tx-args", default="", help="UHD device args of the transmit radio")
    ap.add_argument("--rx-args", default="", help="UHD device args of the receive radio")
    ap.add_argument("--ack-host", default="127.0.0.1")
    ap.add_argument("--ack-port", type=int, default=5599,
                    help="TCP port the USRP ARQ acknowledgement travels on (default "
                         "5599). Two nodes sharing a host need different ones, which is "
                         "what a topology file's ports.ack is for.")
    ap.add_argument("--net-host", default="127.0.0.1")
    ap.add_argument("--net-port", type=int, default=5700,
                    help="TCP reply port this node SERVES to the node upstream of it")
    ap.add_argument("--down-host", default=None,
                    help="relay only: host of the next hop downstream")
    ap.add_argument("--down-port", type=int, default=None,
                    help="relay only: that node's --net-port (default: --net-port + 1)")
    return ap


def main():
    ap = build_parser()
    a = ap.parse_args()
    a.role_index = a.hub_index = None
    a._typed = _typed_flags(ap)         # what was actually typed, vs what merely defaulted

    # the wiring file, when --topology names one: it fills in everything about THIS node
    # that was not typed on the command line (role, ports, hosts, radio, medium).
    topo = apply_topology(ap, a)
    if a.node is not None:
        a.node = int(a.node)                # a name has been resolved to its index

    if a.role is None:                      # --node 3 alone means "I am one peer of the network"
        a.role = "peer" if a.node is not None else "loopback"

    if a.print_plan:
        print_plan(a, topo)
        return

    if a.radio:                             # one flag names the radio this node process owns
        dev = device_args(a.radio)
        a.tx_args = a.tx_args or dev        # explicit --tx-args/--rx-args still win
        a.rx_args = a.rx_args or dev
        print(f"[run_algo] radio: tx_args={a.tx_args!r} rx_args={a.rx_args!r}")

    kind_sel = CHANNEL_ALIASES.get(a.channel, a.channel)
    warn_foreign_flags(a, ap, kind_sel)
    warn_simulation_only_flags(a, ap, kind_sel, a.role or "loopback")
    if kind_sel != "ideal" or a.node is not None or a.role in ("tx", "rx", "relay"):
        check_band(a.freq, kind_sel if kind_sel != "ideal" else "usrp")

    factory, how, mod = load_app_factory(a.algo)
    alias2t, t2alias = role_map(mod)
    transport, algo_role = resolve_role(a.role, alias2t, t2alias, a.algo)
    print(f"[run_algo] loaded algorithms/{a.algo} via {how}")
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
            sys.exit(f"algorithms/{a.algo} needs make(role) + run(sensors, ...) for --role aircomp")
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
            dial_ports = [int(p) for p in a.peer_ports.split(",") if p.strip()] or None
            link = pl.PeerLink(node_id=a.node, n_nodes=a.agents, topology=a.topology,
                               peers=hosts, base_port=a.peer_port, link=peer_link,
                               peer_ports=dial_ports, ack_port=a.ack_port,
                               tx_args=a.tx_args, rx_args=a.rx_args, scheme=a.scheme,
                               waveform=a.waveform, tx_subdev=a.tx_subdev,
                               rx_subdev=a.rx_subdev, tx_ant=a.tx_ant, rx_ant=a.rx_ant,
                               extra_cfg=_usrp_extra(a),
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
        app = factory(algo_role, index=a.role_index, total=a.clients)
        n = 0
        try:
            while link.step(app):
                n += 1
                if transport == "tx" and n >= a.steps:
                    break
        finally:
            # a node that stops because it ran out of --steps still owes the other end a
            # goodbye — without it the far side waits for a round that never comes
            if callable(getattr(link, "close", None)):
                link.close()
        print(f"[run_algo] {algo_role} ({transport}) done: {n} steps")

    # What did the PHY actually cost? For LoRa this is the headline number — the
    # airtime an experiment would really have spent on the air.
    if ch is not None and hasattr(ch, "stats"):
        print(f"[run_algo] {ch.name} PHY: {ch.stats()}")


if __name__ == "__main__":
    main()
