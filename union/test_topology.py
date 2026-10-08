#!/usr/bin/env python3
"""
test_topology.py — does a topology FILE actually reach the objects it configures?

    python3 union/test_topology.py

The same standard test_flags.py holds the CLI to, applied to the wiring file: a setting
that is parsed and then dropped before the transport is built looks completely
functional — the run starts, the file is read, and the node is wired the way nobody
asked. So each check here walks the FULL path (file + argv in, constructed link out)
and asserts the value arrived.

The second half checks the REFUSALS, which are the reason the file is worth having: a
wireless link whose transmitter has no transmit radio, two nodes on one host claiming
one port, peer ports that do not match the base+index rule PeerLink actually uses. Each
of those is a run that fails minutes later on a testbed, with an error naming the wrong
layer. They have to fail HERE, at load, and say which node.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import run_algo as R                                        # noqa: E402
import phy_link as pl                                       # noqa: E402
import topology as tp                                       # noqa: E402

GREEN, RED, YEL, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = OFF = ""

results = []
TMP = tempfile.mkdtemp(prefix="union-topo-")


def parse(argv, algo="fl"):
    """Exactly what main() does before it runs anything: parse, then let the file fill
    in what was not typed."""
    for k in [k for k in os.environ if k.startswith("UNION_")]:
        del os.environ[k]                       # each check starts from a clean slate
    ap = R.build_parser()
    a = ap.parse_args(["--algo", algo] + argv)
    a.role_index = a.hub_index = None
    a._typed = R._typed_flags(ap, argv)
    a._typed_usrp = {kv.split("=", 1)[0].strip().replace("-", "_")
                     for kv in (getattr(a, "usrp_set", []) or []) if "=" in kv}
    topo = R.apply_topology(ap, a)
    if a.node is not None:
        a.node = int(a.node)
    if a.role is None:
        a.role = "peer" if a.node is not None else "loopback"
    return a, topo


def check(label, get, want):
    try:
        got = get()
        ok = got == want
    except BaseException as e:                              # noqa: BLE001
        got, ok = f"{type(e).__name__}: {e}", False
    results.append((label, ok))
    print(f"  {label:<34} {GREEN+'OK  '+OFF if ok else RED+'FAIL'+OFF}  "
          f"{DIM}got={got!r}{OFF}" + ("" if ok else f"  want={want!r}"))


def refuses(label, argv, needle, algo="fl"):
    """A bad file must be refused, and the message must name the problem."""
    try:
        parse(argv, algo=algo)
        got, ok = "accepted it", False
    except (SystemExit, tp.TopologyError) as e:
        got = str(e)
        ok = needle.lower() in got.lower()
        got = got.strip().splitlines()[0][:70]
    except BaseException as e:                              # noqa: BLE001
        got, ok = f"{type(e).__name__}: {e}", False
    results.append((label, ok))
    print(f"  {label:<34} {GREEN+'OK  '+OFF if ok else RED+'FAIL'+OFF}  {DIM}{got}{OFF}"
          + ("" if ok else f"   (expected a message mentioning {needle!r})"))


def wrote(name, doc):
    p = os.path.join(TMP, name + ".json")
    with open(p, "w") as fh:
        json.dump(doc, fh)
    return p


def peer_link(a):
    """Build the real PeerLink this node would use, then let go of its socket."""
    link = pl.PeerLink(node_id=a.node, n_nodes=a.agents, topology=a.topology,
                       peers=[h for h in a.peers.split(",") if h] or None,
                       base_port=a.peer_port, link=a.peer_link or "tcp",
                       peer_ports=[int(p) for p in a.peer_ports.split(",") if p.strip()]
                       or None,
                       tx_args=a.tx_args, rx_args=a.rx_args, scheme=a.scheme,
                       tx_ant=a.tx_ant, rx_ant=a.rx_ant, tx_subdev=a.tx_subdev,
                       rx_subdev=a.rx_subdev)
    link.close()
    return link


# ══ 1. the file reaches the run ══════════════════════════════════════════════
print("\n  fl-star-tcp — a federated star with no radio in it")
c0, _ = parse(["--topology", "fl-star-tcp", "--node", "c0"])
check("--node by NAME -> index",   lambda: c0.node,                     1)
check("role comes from the file",  lambda: c0.role,                     "client")
check("node count -> --agents",    lambda: c0.agents,                   3)
check("defaults.steps",            lambda: c0.steps,                    20)
check("tcp medium -> --link",      lambda: c0.link,                     "tcp")
check("client dials the hub",      lambda: (c0.net_host, c0.net_port),  ("127.0.0.1", 5700))
check("client id published",       lambda: os.environ["UNION_ROLE_INDEX"], "0")
check("client count published",    lambda: os.environ["UNION_CLIENTS"],   "2")
check("client -> TcpStar hub",     lambda: (lambda L: (type(L).__name__, L.hub_port, L.id))(
                                       R.build_link(c0, "tx")), ("TcpStar", 5700, 0))
c1, _ = parse(["--topology", "fl-star-tcp", "--node", "c1"])
check("the OTHER client's shard",  lambda: os.environ["UNION_ROLE_INDEX"], "1")
srv, _ = parse(["--topology", "fl-star-tcp", "--node", "srv"])
check("server role",               lambda: srv.role,                    "server")
check("server aggregates over N",  lambda: srv.clients,                 2)
check("server binds its own port", lambda: srv.net_port,                5700)

print("\n  fl-star-radio — the same star on the RX-only-N210 rig")
rc0, _ = parse(["--topology", "fl-star-radio", "--node", "c0"])
check("wireless up -> USRP link",  lambda: rc0.link,                    "usrp")
check("radio args",                lambda: rc0.tx_args,                 "serial=30CD424")
check("TX connector",              lambda: rc0.tx_ant,                  "TX/RX")
check("TX RF channel",             lambda: rc0.tx_subdev,               "A:A")
check("TX gain",                   lambda: rc0.tx_gain,                 78)
check("ack goes to the server",    lambda: rc0.ack_host,                "192.168.10.1")
check("...on the SINK's ack port", lambda: parse(["--topology", wrote("ack-port", {
    "schema": 1, "name": "ack-port", "nodes": [
        {"id": "srv", "role": "server", "host": "10.0.0.9",
         "ports": {"net": 5700, "ack": 5610},
         "radio": {"args": "addr=2", "rx": {}}},
        {"id": "c0", "role": "client", "host": "10.0.0.8",
         "radio": {"args": "serial=1", "tx": {}}}],
    "links": [{"from": "c0", "to": "srv", "medium": {"up": "wireless", "down": "tcp"}}]}),
    "--node", "c0"])[0].ack_port, 5610)
check("-> RadioRoundTrip cfg",     lambda: (lambda L: (L.cfg["tx_ant"], L.cfg["tx_subdev"],
                                                       L.tx_gain, L.cfg["scheme"],
                                                       L.cfg["tx_freq"]))(
                                       R.build_link(rc0, "tx")),
      ("TX/RX", "A:A", 78, "QPSK", 915e6))
rsrv, _ = parse(["--topology", "fl-star-radio", "--node", "srv"])
check("server RX connector",       lambda: (rsrv.rx_args, rsrv.rx_ant, rsrv.rx_subdev,
                                            rsrv.rx_gain),
      ("addr=192.168.10.2", "RX2", "A:0", 25))
check("ports.ack -> the ARQ socket", lambda: rsrv.ack_port,             5599)
check("...and into the modem cfg", lambda: R.build_link(rsrv, "rx").cfg["ack_port"], 5599)
check("server binds a reachable IP", lambda: rsrv.net_host,             "192.168.10.1")
check("...on every interface",     lambda: R.build_link(rsrv, "rx").bind_host,  "0.0.0.0")

print("\n  dl-ring3-tcp — a decentralised ring, one process per node")
n1, topo = parse(["--topology", "dl-ring3-tcp", "--node", "n1"], algo="dl")
check("role peer",                 lambda: n1.role,                     "peer")
check("graph -> edge list",        lambda: n1.topology,                 "0-1,1-2,2-0")
check("file order IS the schedule", lambda: topo.edges(),               [(0, 1), (1, 2), (2, 0)])
check("tcp medium -> --peer-link", lambda: n1.peer_link,                "tcp")
check("peer base port",            lambda: n1.peer_port,                5800)
check("-> PeerLink neighbours",    lambda: peer_link(n1).neighbours,    [0, 2])
check("-> PeerLink listens on",    lambda: peer_link(n1).base_port + n1.node, 5801)
check("shard id published",        lambda: os.environ["UNION_INDEX"],   "1")

print("\n  dl-pair-wireless — two peers over the air")
w0, _ = parse(["--topology", "dl-pair-wireless", "--node", "n0"], algo="dl")
check("wireless -> --peer-link",   lambda: w0.peer_link,                "wireless")
check("both directions wired",     lambda: (w0.tx_args, w0.rx_args),
      ("serial=30CD424", "serial=30CD424"))
check("-> PeerLink radio cfg",     lambda: (lambda L: (L.cfg["tx_ant"], L.cfg["rx_ant"],
                                                       L.cfg["rx_subdev"]))(peer_link(w0)),
      ("TX/RX", "RX2", "A:A"))

print("\n  fl-chain-mixed — one hop over the air, the next over Ethernet")
m1, _ = parse(["--topology", "fl-chain-mixed", "--node", "n1"])
check("source transmits by radio",  lambda: m1.link,                     "usrp")
check("it dials the RELAY, not n3", lambda: (m1.net_host, m1.net_port),  ("10.0.0.2", 5700))
m2, _ = parse(["--topology", "fl-chain-mixed", "--node", "n2"])
check("relay with mixed hops",      lambda: m2.link,                     "chain")
check("hop in / hop out",           lambda: (m2.up_medium, m2.down_medium),
      ("wireless", "tcp"))
check("relay SERVES its own port",  lambda: (m2.net_host, m2.net_port),  ("10.0.0.2", 5700))
check("relay dials the next hop",   lambda: (m2.down_host, m2.down_port), ("10.0.0.3", 5701))
check("ports.down names it instead", lambda: parse(["--topology", wrote("down-port", {
    "schema": 1, "name": "down-port", "nodes": [
        {"id": "a", "role": "client", "host": "10.0.0.1"},
        {"id": "b", "role": "relay", "host": "10.0.0.2",
         "ports": {"net": 5700, "down": 5999}},
        {"id": "c", "role": "server", "host": "10.0.0.3", "ports": {"net": 5701}}],
    "links": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}),
    "--node", "b"])[0].down_port, 5999)
check("RX-only relay needs no TX",  lambda: (m2.rx_args, m2.tx_args),
      ("addr=192.168.10.2", ""))
check("-> ChainRelay legs",         lambda: (lambda L: (type(L).__name__, L.up, L.down,
                                                        L.down_port))(
                                        R.build_link(m2, "relay")),
      ("ChainRelay", "wireless", "tcp", 5701))
m3, _ = parse(["--topology", "fl-chain-mixed", "--node", "n3"])
check("sink is plain TCP",          lambda: (m3.link, m3.net_port),      ("tcp", 5701))
check("one source in a chain",      lambda: os.environ["UNION_CLIENTS"], "1")
c2, _ = parse(["--topology", "fl-chain-tcp", "--node", "n2"])
check("an all-TCP relay",           lambda: (c2.link, c2.up_medium, c2.down_medium),
      ("chain", "tcp", "tcp"))
check("frame id = index at the hub", lambda: R._hub_index(c2),           0)
cc0, _ = parse(["--topology", "fl-star-tcp", "--node", "c1"])
check("...and in a star, per client", lambda: R._hub_index(cc0),         1)

print("\n  advertise — what a node BINDS vs what everyone else DIALS (NodePort)")
NODEPORT = {"schema": 1, "name": "nodeport", "algo": "fl", "nodes": [
    {"id": "srv", "role": "server", "host": "10.42.0.107",
     "ports": {"net": 5700, "ack": 5599},
     "advertise": {"host": "10.10.1.23", "ports": {"net": 35700, "ack": 35999}},
     "radio": {"args": "addr=192.168.10.2", "rx": {}}},
    {"id": "c0", "role": "client", "host": "10.42.0.108",
     "radio": {"args": "serial=30CD424", "tx": {}}}],
    "links": [{"from": "c0", "to": "srv", "medium": {"up": "wireless", "down": "tcp"}}]}
np_file = wrote("nodeport", NODEPORT)
nc, _ = parse(["--topology", np_file, "--node", "c0"])
check("dials the PUBLISHED address", lambda: (nc.net_host, nc.net_port),
      ("10.10.1.23", 35700))
check("...and the published ack",  lambda: (nc.ack_host, nc.ack_port),
      ("10.10.1.23", 35999))
ns, _ = parse(["--topology", np_file, "--node", "srv"])
check("the sink BINDS its own port", lambda: (ns.net_port, ns.ack_port), (5700, 5599))
check("...on every interface",     lambda: R.build_link(ns, "rx").bind_host, "0.0.0.0")

PEERPORT = {"schema": 1, "name": "peer-np", "algo": "dl", "defaults": {"medium": "tcp"},
            "nodes": [
                {"id": "p0", "role": "peer", "host": "10.42.0.1", "ports": {"peer": 5800},
                 "advertise": {"host": "10.10.1.21", "ports": {"peer": 30801}}},
                {"id": "p1", "role": "peer", "host": "10.42.0.2", "ports": {"peer": 5801},
                 "advertise": {"host": "10.10.1.22", "ports": {"peer": 30907}}}],
            "links": [{"from": "p0", "to": "p1", "medium": "tcp"}]}
pp, _ = parse(["--topology", wrote("peer-np", PEERPORT), "--node", "p0"], algo="dl")
check("peers dial published ports", lambda: pp.peer_ports,            "30801,30907")
check("...but LISTEN on base+k",   lambda: peer_link(pp).base_port + pp.node, 5800)
check("-> PeerLink dial table",    lambda: peer_link(pp).peer_ports,  [30801, 30907])
check("peers dial published hosts", lambda: pp.peers,                 "10.10.1.21,10.10.1.22")

# ══ 2. anything typed WINS over the file ═════════════════════════════════════
print("\n  the command line beats the file")
check("--steps",     lambda: parse(["--topology", "fl-star-tcp", "--node", "c0",
                                    "--steps", "7"])[0].steps,             7)
check("--net-port",  lambda: parse(["--topology", "fl-star-tcp", "--node", "c0",
                                    "--net-port", "6001"])[0].net_port,    6001)
check("--role",      lambda: parse(["--topology", "fl-star-tcp", "--node", "c0",
                                    "--role", "server"])[0].role,          "server")
check("--tx-gain",   lambda: parse(["--topology", "fl-star-radio", "--node", "c0",
                                    "--tx-gain", "50"])[0].tx_gain,        50.0)
check("--tx-ant",    lambda: parse(["--topology", "fl-star-radio", "--node", "c0",
                                    "--tx-ant", "RX2"])[0].tx_ant,         "RX2")
check("a typed default still wins",
      lambda: parse(["--topology", "fl-star-tcp", "--node", "c0", "--steps", "5"])[0].steps, 5)

# ══ 3. the built-in graphs are untouched ═════════════════════════════════════
print("\n  no file: every existing command still means what it did")
check("--topology ring",  lambda: parse(["--topology", "ring", "--node", "0",
                                         "--agents", "4"], algo="dl")[0].topology, "ring")
check("--topology full",  lambda: len(pl.gossip_edges(4, "full")),               6)
check("an edge list",     lambda: parse(["--topology", "0-1,1-2", "--node", "0",
                                         "--agents", "3"], algo="dl")[0].topology, "0-1,1-2")
check("no topology file",  lambda: parse(["--topology", "ring", "--node", "0",
                                          "--agents", "2"], algo="dl")[1],        None)

# ══ 4. the refusals — each one is a testbed run that would have failed later ══
print("\n  a file that cannot be run as written is refused, by name")
no_tx = wrote("no-tx", {"schema": 1, "name": "no-tx", "nodes": [
    {"id": "srv", "role": "server", "host": "10.0.0.1", "ports": {"net": 5700},
     "radio": {"args": "addr=192.168.10.2", "rx": {"ant": "RX2"}}},
    {"id": "c0", "role": "client", "host": "10.0.0.2"}],
    "links": [{"from": "c0", "to": "srv", "medium": {"up": "wireless", "down": "tcp"}}]})
refuses("transmits with no TX radio", ["--topology", no_tx, "--node", "c0"], "cannot transmit")

no_rx = wrote("no-rx", {"schema": 1, "name": "no-rx", "nodes": [
    {"id": "srv", "role": "server", "host": "10.0.0.1", "ports": {"net": 5700}},
    {"id": "c0", "role": "client", "host": "10.0.0.2",
     "radio": {"args": "serial=30CD424", "tx": {"ant": "TX/RX"}}}],
    "links": [{"from": "c0", "to": "srv", "medium": {"up": "wireless", "down": "tcp"}}]})
refuses("receives with no RX radio", ["--topology", no_rx, "--node", "srv"], "cannot receive")

duplex = wrote("duplex", {"schema": 1, "name": "duplex", "nodes": [
    {"id": "a", "host": "10.0.0.1", "role": "client",
     "radio": {"args": "serial=1", "tx": {}, "rx": {}}},
    {"id": "b", "host": "10.0.0.2", "role": "server",
     "radio": {"args": "serial=2", "tx": {}, "rx": {}}}],
    "links": [{"from": "a", "to": "b", "medium": "wireless"}]})
# A wireless REPLY is allowed when the radios can actually do it, and selects the
# modem's rf ACK transport. This was refused outright until 2026-10, on the stated
# grounds that "the RX-only N210 never transmits" -- a fact about one rig written as
# though it were a fact about every rig. Both ends here declare tx and rx, so each can
# run one radio full duplex: data on one RF path, the acknowledgement on the other.
check("wireless reply selects rf ack",
      lambda: parse(["--topology", duplex, "--node", "a"])[0].ack_transport, "rf")
check("wireless reply, other end too",
      lambda: parse(["--topology", duplex, "--node", "b"])[0].ack_transport, "rf")

# ...and is still refused when the end that must answer cannot transmit. That check
# lives in topology.py and fires per direction at load time, which is why there is no
# second copy of it in run_algo: one guard, at the layer that owns the radio schema.
half = wrote("half-duplex", {"schema": 1, "name": "half-duplex", "nodes": [
    {"id": "a", "host": "10.0.0.1", "role": "client",
     "radio": {"args": "serial=1", "tx": {}, "rx": {}}},
    {"id": "b", "host": "10.0.0.2", "role": "server",
     "radio": {"args": "serial=2", "rx": {}}}],          # no tx: cannot acknowledge
    "links": [{"from": "a", "to": "b", "medium": "wireless"}]})
refuses("wireless reply needs a transmitter", ["--topology", half, "--node", "a"],
        "no radio.tx")

# a TCP reply under a wireless uplink stays tcp — the split still exists, it is just
# no longer the only option
split = wrote("split-medium", {"schema": 1, "name": "split-medium", "nodes": [
    {"id": "a", "host": "10.0.0.1", "role": "client",
     "radio": {"args": "serial=1", "tx": {}}},
    {"id": "b", "host": "10.0.0.2", "role": "server", "ports": {"ack": 5599},
     "radio": {"args": "serial=2", "rx": {}}}],
    "links": [{"from": "a", "to": "b", "medium": {"up": "wireless", "down": "tcp"}}]})
check("tcp reply stays tcp",
      lambda: parse(["--topology", split, "--node", "a"])[0].ack_transport, "tcp")

dup = wrote("dup-port", {"schema": 1, "name": "dup-port", "nodes": [
    {"id": "a", "host": "127.0.0.1", "ports": {"net": 5700}},
    {"id": "b", "host": "127.0.0.1", "ports": {"net": 5700}}],
    "links": [{"from": "a", "to": "b"}]})
refuses("two nodes, one port", ["--topology", dup, "--node", "a"], "both listen on")

base = wrote("bad-base", {"schema": 1, "name": "bad-base", "nodes": [
    {"id": "a", "role": "peer", "host": "127.0.0.1", "ports": {"peer": 5800}},
    {"id": "b", "role": "peer", "host": "127.0.0.1", "ports": {"peer": 5900}}],
    "links": [{"from": "a", "to": "b"}]})
refuses("peer ports must be base+k", ["--topology", base, "--node", "a"], "base+index")

typo = wrote("typo", {"schema": 1, "name": "typo", "nodes": [
    {"id": "a", "host": "127.0.0.1", "rol": "client"},
    {"id": "b", "host": "127.0.0.1"}], "links": [{"from": "a", "to": "b"}]})
refuses("a misspelled key", ["--topology", typo, "--node", "a"], "unknown key")

ghost = wrote("ghost", {"schema": 1, "name": "ghost", "nodes": [
    {"id": "a", "host": "127.0.0.1"}, {"id": "b", "host": "127.0.0.1"}],
    "links": [{"from": "a", "to": "zz"}]})
refuses("a link to nowhere", ["--topology", ghost, "--node", "a"], "is not a node")

future = wrote("future", {"schema": 99, "name": "future", "nodes": [{"id": "a"}],
                          "links": [{"from": "a", "to": "a"}]})
refuses("a schema we cannot read", ["--topology", future, "--node", "a"], "schema")

split = wrote("split-medium", {"schema": 1, "name": "split-medium", "nodes": [
    {"id": "a", "role": "client", "host": "10.0.0.1",
     "radio": {"args": "serial=1", "tx": {}}},
    {"id": "b", "role": "client", "host": "10.0.0.2"},
    {"id": "c", "role": "server", "host": "10.0.0.3", "ports": {"net": 5700},
     "radio": {"args": "addr=2", "rx": {}}}],
    "links": [{"from": "a", "to": "c", "medium": {"up": "wireless", "down": "tcp"}},
              {"from": "b", "to": "c", "medium": "tcp"}]})
refuses("a node receiving two ways", ["--topology", split, "--node", "c"],
        "at once")

clash = wrote("published-clash", {"schema": 1, "name": "published-clash", "nodes": [
    {"id": "a", "role": "client", "host": "10.0.0.1",
     "advertise": {"host": "10.10.1.9", "ports": {"net": 35700}}},
    {"id": "b", "role": "server", "host": "10.0.0.2", "ports": {"net": 5700},
     "advertise": {"host": "10.10.1.9", "ports": {"net": 35700}}}],
    "links": [{"from": "a", "to": "b"}]})
refuses("two nodes, one published port", ["--topology", clash, "--node", "a"],
        "both published at")

homeless = wrote("homeless-ports", {"schema": 1, "name": "homeless-ports", "nodes": [
    {"id": "a", "role": "client", "host": "10.0.0.1"},
    {"id": "b", "role": "server", "ports": {"net": 5700},
     "advertise": {"ports": {"net": 35700}}}],
    "links": [{"from": "a", "to": "b"}]})
refuses("published ports, no address", ["--topology", homeless, "--node", "a"],
        "without a host")

refuses("--node that is not in the file", ["--topology", "fl-star-tcp", "--node", "zz"],
        "is not in")
refuses("a topology file that is absent", ["--topology", "no-such-topology", "--node", "0"],
        "no topology")

# ── modem options stated in the file, not retyped per run ────────────────────
# det_mult / sync_threshold / bytes_length have no run_algo flag: they reach the C++
# modem through --usrp-set. A topology may now state them, so a rig's known detector
# settings live beside the gains and connectors they belong with. An unrecognised
# defaults key used to configure nothing and say nothing, which is how someone would
# discover this was unsupported: by a run that quietly used the modem's defaults.
modem = wrote("modem-defaults", {
    "schema": 1, "name": "modem-defaults", "algo": "echo",
    "defaults": {"channel": "usrp", "scheme": "QPSK", "waveform": "sc",
                 "det_mult": 30, "sync_threshold": 10, "bytes_length": 1000},
    "nodes": [
        {"id": "src", "role": "tx",
         "radio": {"device": "x310", "addr": "192.168.30.2",
                   "tx": {"ant": "TX/RX", "subdev": "A:0", "gain": 25,
                          "freq_mhz": 2400}}},
        {"id": "snk", "role": "rx", "ports": {"ack": 5599},
         "radio": {"device": "x310", "addr": "192.168.40.2",
                   "rx": {"ant": "RX2", "subdev": "A:0", "gain": 20,
                          "freq_mhz": 2400}}}],
    "links": [{"from": "src", "to": "snk",
               "medium": {"up": "wireless", "down": "tcp"}}]})


def usrp_set(argv):
    a, _ = parse(argv, algo="echo")
    return sorted(getattr(a, "usrp_set", []) or [])


check("defaults -> --usrp-set",
      lambda: usrp_set(["--topology", modem, "--node", "snk",
                        "--usrp-backend", "radio"]),
      ["bytes_length=1000", "det_mult=30", "sync_threshold=10"])
# the in-process backend REFUSES --usrp-set, so injecting there would stop the run
# starting at all -- the trap the surveyed det_mult already fell into once. A wireless
# link selects the radio backend by itself, so the case that can actually break is a
# file carrying modem defaults over a medium that never starts the modem.
tcp_modem = wrote("modem-tcp", {
    "schema": 1, "name": "modem-tcp", "algo": "echo",
    "defaults": {"det_mult": 30, "bytes_length": 1000},
    "nodes": [{"id": "a", "role": "client", "host": "127.0.0.1"},
              {"id": "b", "role": "server", "ports": {"net": 5700}}],
    "links": [{"from": "a", "to": "b", "medium": "tcp"}]})
check("...not on a backend that cannot take them",
      lambda: usrp_set(["--topology", tcp_modem, "--node", "a"]), [])
# and a typed value still beats the file, like every other setting
check("typed --usrp-set still wins",
      lambda: [x for x in usrp_set(["--topology", modem, "--node", "snk",
                                    "--usrp-backend", "radio",
                                    "--usrp-set", "sync_threshold=25"])
               if "sync_threshold" in x],
      ["sync_threshold=25"])
check("single carrier reaches the modem",
      lambda: parse(["--topology", modem, "--node", "snk"], algo="echo")[0].waveform,
      "sc")

# ── what counts as a carrier ─────────────────────────────────────────────────
# A QUOTED number is accepted and coerced, because the blank a generated draft
# leaves is itself a quoted string ("freq_mhz": "REPLACE_ME_WITH_FREQ_OPTION") and
# the obvious edit is to replace the text between the quotes. Refusing "2450" is
# technically right and practically useless. Normalising at LOAD matters too: a
# string reaching the candidate code would be iterated character by character.
def freq_file(v):
    return wrote(f"freq-{str(v).replace(' ', '')[:12]}", {
        "schema": 1, "name": "freqform", "algo": "echo",
        "defaults": {"channel": "usrp"},
        "nodes": [
            {"id": "a", "role": "tx",
             "radio": {"device": "x310", "serial": "AAA",
                       "tx": {"ant": "TX/RX", "subdev": "A:0", "freq_mhz": v}}},
            {"id": "b", "role": "rx", "ports": {"ack": 5599},
             "radio": {"device": "x310", "serial": "BBB",
                       "rx": {"ant": "RX2", "subdev": "A:0", "freq_mhz": 2462.5}}}],
        "links": [{"from": "a", "to": "b",
                   "medium": {"up": "wireless", "down": "tcp"}}]})


def freq_of(v):
    return tp.load(freq_file(v)).node("a").side("tx", "freq_mhz")


check("an integer carrier", lambda: freq_of(2450), 2450.0)
check("a float carrier", lambda: freq_of(2462.5), 2462.5)
check("a QUOTED integer is coerced", lambda: freq_of("2450"), 2450.0)
check("a quoted float is coerced", lambda: freq_of("2462.5"), 2462.5)
check("whitespace around it is tolerated", lambda: freq_of(" 2450 "), 2450.0)
check("a candidate list of numbers", lambda: freq_of([2462.5, 2450]), [2462.5, 2450.0])
check("a candidate list of quoted numbers",
      lambda: freq_of(["2450", 2462.5]), [2450.0, 2462.5])
check("a draft's blank passes through", lambda: freq_of("REPLACE_ME_X"), "REPLACE_ME_X")
refuses("a carrier that is not a number",
        ["--topology", freq_file("abc"), "--node", "a"],
        "A number here needs no quotes", algo="echo")
refuses("a carrier of zero", ["--topology", freq_file(0), "--node", "a"],
        "not a positive frequency", algo="echo")
refuses("true is not a carrier", ["--topology", freq_file(True), "--node", "a"],
        "is not a frequency in MHz", algo="echo")

# ── // comments, so a topology can explain itself above the JSON ─────────────
# JSON has no comments and this schema refuses unknown keys, so explanation had
# nowhere to live but inside the data -- `note` fields holding paragraphs, which is
# what made a generated file read as generated output rather than as a topology.
def wrote_raw(name, text):
    q = os.path.join(TMP, name + ".json")
    with open(q, "w") as fh:
        fh.write(text)
    return q


commented = wrote_raw("commented", """// a header above the file
// explaining what it is
{
  "schema": 1, "name": "commented", "algo": "echo",   // trailing comments too
  "defaults": { "channel": "usrp" },
  "nodes": [
    { "id": "a", "role": "client", "host": "127.0.0.1" },
    { "id": "b", "role": "server", "ports": { "net": 5700 } }
  ],
  "links": [ { "from": "a", "to": "b" } ]
}
""")
check("a commented topology loads", lambda: len(tp.load(commented).nodes), 2)
check("...and a trailing comment does not eat the line",
      lambda: tp.load(commented).algo, "echo")

# a // INSIDE a value must survive: cutting at the first // anywhere would eat a
# URL or a UNC path, and the result would usually still parse -- silent corruption
slashes = wrote_raw("slashes", """// header
{ "schema": 1, "name": "slashes", "algo": "echo",
  "description": "see http://example.com//docs for why",
  "nodes": [ { "id": "a", "role": "client", "host": "127.0.0.1" },
             { "id": "b", "role": "server", "ports": { "net": 5700 } } ],
  "links": [ { "from": "a", "to": "b" } ] }
""")
check("// inside a value survives",
      lambda: tp.load(slashes).description, "see http://example.com//docs for why")

# a JSON error must still name the line the file actually has, so comment lines are
# blanked rather than deleted
broken = wrote_raw("broken", """// one
// two
{ "schema": 1, "name": "broken",
  "nodes": [ { "id": "a" ]
""")
refuses("a comment header keeps line numbers", ["--topology", broken, "--node", "a"],
        "line 4", algo="echo")

# the shipped TEMPLATE is a real topology, header and all
check("TEMPLATE parses with its header",
      lambda: [nd.id for nd in tp.load("TEMPLATE").nodes], ["src", "snk"])

# ── .jsonc, so an editor does not call a commented topology broken ───────────
# An editor decides whether comments are legal by EXTENSION: VS Code marks every
# comment in a .json file as an error, and a generated file an editor calls broken
# gets edited into something that is. Both extensions resolve, so nothing that
# already exists has to be renamed.
jc = os.path.join(TMP, "ext-demo.jsonc")
with open(jc, "w") as fh:
    fh.write('''// a header an editor will accept here
{ "schema": 1, "name": "ext-demo", "algo": "echo",
  "nodes": [ { "id": "a", "role": "client", "host": "127.0.0.1" },
             { "id": "b", "role": "server", "ports": { "net": 5700 } } ],
  "links": [ { "from": "a", "to": "b" } ] }
''')
check("a .jsonc topology loads by path", lambda: len(tp.load(jc).nodes), 2)
check("...and by bare name, with the extension inferred",
      lambda: os.path.basename(tp.resolve(os.path.join(TMP, "ext-demo"))),
      "ext-demo.jsonc")
check("the listing name keeps no stray dot",
      lambda: os.path.splitext(os.path.basename(jc))[0], "ext-demo")
# the shipped template is .jsonc and still answers to its bare name
check("TEMPLATE resolves without its extension",
      lambda: os.path.basename(tp.resolve("TEMPLATE")), "TEMPLATE.jsonc")
check("...and both extensions are searched",
      lambda: tp.EXTS, (".json", ".jsonc"))

# ── ack_wireless: one switch for how the reply travels ───────────────────────
# A draft carries the wiring for BOTH reply paths, so choosing between them by
# editing every link's medium is the kind of edit that gets half-done. The switch
# also has to make the unused path genuinely unused: left configured, the modem
# tunes a subdev a single-daughterboard X310 does not have, and fails on hardware
# for a direction the file just said not to use.
def ack_pair(flag):
    return wrote(f"ackw-{flag}", {
        "schema": 1, "name": f"ackw-{flag}", "algo": "echo",
        "defaults": {"channel": "usrp", "ack_wireless": flag},
        "nodes": [
            {"id": "src", "role": "tx",
             "radio": {"device": "x310", "serial": "AAA",
                       "tx": {"ant": "TX/RX", "subdev": "A:0", "gain": 25,
                              "freq_mhz": 2404.5},
                       "rx": {"ant": "RX2", "subdev": "B:0", "gain": 20,
                              "freq_mhz": 2440.5}}},
            {"id": "snk", "role": "rx", "ports": {"ack": 5599},
             "radio": {"device": "x310", "serial": "BBB",
                       "rx": {"ant": "RX2", "subdev": "A:0", "gain": 20,
                              "freq_mhz": 2404.5},
                       "tx": {"ant": "TX/RX", "subdev": "B:0", "gain": 25,
                              "freq_mhz": 2440.5}}}],
        "links": [{"from": "src", "to": "snk",
                   "medium": {"up": "wireless", "down": "tcp"}}]})


check("ack_wireless true -> the reply goes over the air",
      lambda: tp.load(ack_pair(True)).links[0].down, "wireless")
check("ack_wireless false -> over TCP",
      lambda: tp.load(ack_pair(False)).links[0].down, "tcp")
# true keeps both RF paths, because both are needed
check("true keeps both directions on the source",
      lambda: tp.load(ack_pair(True)).node("src").radio["rx"] is not None, True)
# false drops the reply path on BOTH nodes: the transmitter's receive side and the
# receiver's transmit side are the ACK path, and nothing else uses them
check("false drops the source's reply receiver",
      lambda: tp.load(ack_pair(False)).node("src").radio["rx"], None)
check("false drops the sink's reply transmitter",
      lambda: tp.load(ack_pair(False)).node("snk").radio["tx"], None)
check("...but keeps the data path intact",
      lambda: (tp.load(ack_pair(False)).node("src").radio["tx"]["subdev"],
               tp.load(ack_pair(False)).node("snk").radio["rx"]["subdev"]),
      ("A:0", "A:0"))
# a topology that never mentions the switch is untouched, blocks and all
check("no switch -> every block kept as authored",
      lambda: tp.load("echo-pair-wireless").node("tx").radio["rx"] is not None, True)
refuses("ack_wireless must be a boolean",
        ["--topology", wrote("ackw-bad", {
            "schema": 1, "name": "ackw-bad", "algo": "echo",
            "defaults": {"ack_wireless": "yes"},
            "nodes": [{"id": "a", "role": "client", "host": "127.0.0.1"},
                      {"id": "b", "role": "server", "ports": {"net": 5700}}],
            "links": [{"from": "a", "to": "b"}]}), "--node", "a"],
        "must be true or false", algo="echo")

# ── a DRAFT lives in topologies/ but must not RUN ────────────────────────────
# prepare.sh writes one at the end of a survey with the far end left as a placeholder.
# It belongs with every other topology so it is found by name; what stops it being a
# trap is that a run refuses it. Left to UHD, serial=REPLACE_ME surfaces as "no device
# found", which sends the reader to the radio, the cabling and the FPGA image.
draft = wrote("a-draft", {
    "schema": 1, "name": "a-draft", "algo": "echo",
    "defaults": {"channel": "usrp"},
    "nodes": [
        {"id": "src", "role": "tx",
         "radio": {"device": "x310", "serial": "REPLACE_ME_SOURCE_ID",
                   "tx": {"ant": "TX/RX", "subdev": "A:0", "freq_mhz": 2404.5}}},
        {"id": "snk", "role": "rx", "ports": {"ack": 5599},
         "radio": {"device": "x310", "serial": "3620E8D",
                   "rx": {"ant": "RX2", "subdev": "A:0", "freq_mhz": 2404.5}}}],
    "links": [{"from": "src", "to": "snk",
               "medium": {"up": "wireless", "down": "tcp"}}]})
# The blank is on src, so src is the node that cannot run -- and snk, whose own
# fields are complete, must run. Each container fills in its own end.
refuses("a draft is refused for the node that owns the blank",
        ["--topology", draft, "--node", "src"], "is still a DRAFT", algo="echo")
refuses("...and it names the field to fill",
        ["--topology", draft, "--node", "src"],
        "radio.args = serial=REPLACE_ME_SOURCE_ID", algo="echo")
check("the OTHER node still runs: it does not read that radio",
      lambda: tp.placeholders(tp.load(draft), "snk"), [])
check("the file as a whole still shows the blank",
      lambda: len(tp.placeholders(tp.load(draft))), 1)
check("...and a finished file has none",
      lambda: tp.placeholders(tp.load("echo-pair-radio")), [])

# a host is only read when something DIALS it: with data AND reply over the air there
# is no socket in the experiment, so an unfilled host is not a missing fact
hostless = wrote("hostless-rf", {
    "schema": 1, "name": "hostless-rf", "algo": "echo",
    "defaults": {"channel": "usrp", "ack_wireless": True},
    "nodes": [
        {"id": "src", "role": "tx", "host": "FILL_ME",
         "radio": {"device": "x310", "serial": "AAA",
                   "tx": {"ant": "TX/RX", "subdev": "A:0", "freq_mhz": 2462.5},
                   "rx": {"ant": "RX2", "subdev": "B:0", "freq_mhz": 2472.5}}},
        {"id": "snk", "role": "rx", "host": "FILL_ME", "ports": {"ack": 5599},
         "radio": {"device": "x310", "serial": "BBB",
                   "rx": {"ant": "RX2", "subdev": "A:0", "freq_mhz": 2462.5},
                   "tx": {"ant": "TX/RX", "subdev": "B:0", "freq_mhz": 2472.5}}}],
    "links": [{"from": "src", "to": "snk",
               "medium": {"up": "wireless", "down": "tcp"}}]})
check("a wireless reply needs no host at all",
      lambda: tp.placeholders(tp.load(hostless), "snk"), [])
# ...and the blank must not be CARRIED as an address either: left in place it sits in
# the config as net_host="FILL_ME", looking like a setting, and reaches bind()/connect()
# as a literal in any run that does open a socket
check("a blank host is not treated as an address",
      lambda: tp.load(hostless).node("snk").host, "")
check("...but is still reported as a blank",
      lambda: tp.load(hostless).node("snk").host_blank, "FILL_ME")
check("...and a real host is untouched",
      lambda: tp.load("fl-star-tcp").node("c0").host_blank, "")
check("...for either end", lambda: tp.placeholders(tp.load(hostless), "src"), [])
# flip the reply to TCP and the same unfilled host becomes a real missing fact
tcpreply = wrote("hostless-tcp", dict(
    json.load(open(hostless)), name="hostless-tcp",
    defaults={"channel": "usrp", "ack_wireless": False}))
check("a TCP reply makes the host required again",
      lambda: tp.placeholders(tp.load(tcpreply), "snk"),
      ["node snk: host = FILL_ME"])
check("...and the source is told whose address it dials",
      lambda: [t for t in tp.placeholders(tp.load(tcpreply), "src") if "dials" in t],
      ["node snk: host = FILL_ME  (this node dials it for the reply)"])

mistyped = wrote("mistyped-default", {
    "schema": 1, "name": "mistyped-default", "algo": "echo",
    "defaults": {"det-mlt": 9},
    "nodes": [{"id": "a", "role": "client", "host": "127.0.0.1"},
              {"id": "b", "role": "server", "ports": {"net": 5700}}],
    "links": [{"from": "a", "to": "b"}]})
refuses("a defaults key nothing reads", ["--topology", mistyped, "--node", "a"],
        "has no setting called", algo="echo")

# ══ summary ══════════════════════════════════════════════════════════════════
bad = [label for label, ok in results if not ok]
print(f"\n  {len(results) - len(bad)}/{len(results)} topology paths checked")
if bad:
    print(f"  {RED}FAILED{OFF}: " + ", ".join(bad))
    sys.exit(1)
print(f"  {GREEN}every setting in a topology file reaches the object it names{OFF}\n")
