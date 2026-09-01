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
refuses("a reply asked to go over RF", ["--topology", duplex, "--node", "a"],
        "carries the reply over TCP")

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

# ══ summary ══════════════════════════════════════════════════════════════════
bad = [label for label, ok in results if not ok]
print(f"\n  {len(results) - len(bad)}/{len(results)} topology paths checked")
if bad:
    print(f"  {RED}FAILED{OFF}: " + ", ".join(bad))
    sys.exit(1)
print(f"  {GREEN}every setting in a topology file reaches the object it names{OFF}\n")
