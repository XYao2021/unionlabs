#!/usr/bin/env python3
"""
topology.py — the experiment's WIRING, as a file: who the nodes are, what radio each
one has, which connector it transmits on, which port it listens on, and how every link
between them is carried.

    /workspace/experiments/topologies/<name>.json

WHY A FILE. A multi-node run is typed on several machines at once, and every one of
them has to agree on the same graph, the same ports and the same media. Typed by hand
per node, they disagree — a client dials 5700 while the server serves 5701, or a node
is told to transmit on a radio that is receive-only, and the run fails minutes later
with an error that names the wrong layer. One file, read by every node, makes the
disagreement impossible to express: each node asks it "which node am I", and the answer
is the whole of its command line.

    ./run.sh --algo fl --topology fl-star-tcp --node c0

WHAT IT SAYS. Everything a node needs and nothing it can discover:

    nodes[]   id, role, host, ports, and the radio it owns — including, per direction,
              the CONNECTOR (TX/RX or RX2) and the RF channel (subdev). A node with no
              "tx" section cannot transmit; a node with no "radio" at all has no radio.
    links[]   one entry per exchange, "from" -> "to", each carried by a medium:
                  "tcp"       plain TCP/IP over the Ethernet the nodes already share
                  "wireless"  over the air, USRP -> USRP (drivers/usrp)
                  "lora"      over the air, SX1276 (drivers/lora)
              or, per direction, {"up": "wireless", "down": "tcp"} — up is from->to.
              That split is not decoration: our rig has an RX-only N210 and TX-only
              B210s, so the only RF path is B210 -> N210 and the reply has to come back
              over TCP. A file that claims otherwise is refused HERE, at load, rather
              than by UHD on the testbed.

WHAT IT DOES NOT SAY. Addresses that are DISCOVERED rather than authored belong in
settings/, which is generated per session (see ../settings/README.md). A `host` in this
file is an authored fact — a lab machine's static IP, or 127.0.0.1 for several
processes on one box. Where session pods get a fresh address every session, leave
`host` out and pass --peers/--net-host at launch; a link that needs a host it does not
have says so by name.

THE THREE QUESTIONS THE README LEFT OPEN, and the answers this schema commits to:

  * Is an edge a link or a direction?  BOTH, and the pair is ORDERED. "from" is the end
    that speaks first; data flows both ways over it (an exchange), which is exactly what
    phy_link.gossip_edges() already means and what PeerLink already schedules ("the node
    named first in the edge sends"). Direction survives where it is real: as the medium
    of each direction, and as the roles at the two ends.
  * What serialises two nodes that would transmit at once?  The link ORDER in this file.
    One exchange completes before the next begins, so a half-duplex radio is never asked
    to do two things at once. A node not on the current link sits it out.
  * Does `role` survive?  Yes, as the algorithm's own vocabulary (fl's client/server/
    relay, dl's peer). It is what the node IS; the medium is how it is attached.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SCHEMA = 1
MEDIA = ("tcp", "wireless", "lora")
# Where a bare name is looked up, in order. The workspace copy wins over the repo's, so
# a testbed can hold an experiment the checkout does not (and init-workspace.sh seeds it).
SEARCH = ("$UNION_TOPOLOGY_DIR", "/workspace/experiments/topologies",
          os.path.join(REPO, "deploy", "workspace", "topologies"))
LOCAL_HOSTS = ("", "127.0.0.1", "localhost", "::1")


class TopologyError(ValueError):
    """A wiring file that cannot be run as written. The message names the node or link."""


# ── helpers ───────────────────────────────────────────────────────────────────
def _dict(x, what):
    if x is None:
        return {}
    if not isinstance(x, dict):
        raise TopologyError(f"{what} must be an object, got {type(x).__name__}")
    return x


def _known(d, allowed, what):
    """A misspelled key is silently ignored by every JSON reader ever written — which is
    how a run ends up not configured the way its file says. Refuse it instead."""
    extra = [k for k in d if k not in allowed]
    if extra:
        raise TopologyError(f"{what}: unknown key(s) {', '.join(sorted(extra))} "
                            f"(known: {', '.join(sorted(allowed))})")


class Node:
    """One node: what it is, where it runs, what it listens on, what radio it owns."""

    KEYS = ("id", "role", "host", "ports", "radio", "lora", "note")
    PORTS = ("net", "peer", "ack", "down")
    RADIO = ("device", "args", "serial", "addr", "tx", "rx", "note")
    SIDE = ("ant", "subdev", "gain", "freq_mhz")

    def __init__(self, raw, index):
        raw = _dict(raw, f"nodes[{index}]")
        _known(raw, self.KEYS, f"nodes[{index}]")
        self.index = index
        self.id = str(raw.get("id") or "").strip()
        if not self.id:
            raise TopologyError(f"nodes[{index}] has no id")
        self.role = str(raw.get("role") or "peer").strip().lower()
        self.host = str(raw.get("host") or "").strip()
        self.note = raw.get("note", "")
        ports = _dict(raw.get("ports"), f"node {self.id}: ports")
        _known(ports, self.PORTS, f"node {self.id}: ports")
        self.ports = {k: int(v) for k, v in ports.items()}
        self.lora = _dict(raw.get("lora"), f"node {self.id}: lora")

        radio = raw.get("radio")
        self.radio = None
        if radio not in (None, False, {}):
            radio = _dict(radio, f"node {self.id}: radio")
            _known(radio, self.RADIO, f"node {self.id}: radio")
            args = radio.get("args") or ""
            if not args:                      # serial= / addr= spelled out separately
                if radio.get("serial"):
                    args = f"serial={radio['serial']}"
                elif radio.get("addr"):
                    args = f"addr={radio['addr']}"
            if not args:
                raise TopologyError(f"node {self.id}: radio needs args (serial=… or addr=…) "
                                    f"so UHD can find it — or drop the radio section")
            self.radio = {"device": str(radio.get("device") or "").lower(), "args": args,
                          "tx": None, "rx": None}
            for side in ("tx", "rx"):
                if side not in radio or radio[side] in (None, False):
                    continue                  # absent == this node cannot use that
                                              # direction (our N210 is receive-only)
                s = _dict(radio[side], f"node {self.id}: radio.{side}")
                _known(s, self.SIDE, f"node {self.id}: radio.{side}")
                self.radio[side] = s          # {} means "yes, with the usual defaults"
            if not self.can_tx() and not self.can_rx():
                raise TopologyError(f"node {self.id}: radio has neither a tx nor an rx "
                                    f"section, so it can do nothing — say which "
                                    f"direction(s) it is wired for")

    # what the node can physically do — the question a wireless link has to ask
    def can_tx(self):
        return bool(self.radio) and self.radio["tx"] is not None

    def can_rx(self):
        return bool(self.radio) and self.radio["rx"] is not None

    def side(self, which, key, default=None):
        s = (self.radio or {}).get(which) or {}
        return s.get(key, default)

    def port(self, which, default):
        return int(self.ports.get(which, default))

    def is_local(self):
        return self.host in LOCAL_HOSTS

    def __repr__(self):
        return f"<node {self.id} role={self.role} host={self.host or '-'}>"


class Link:
    """One exchange between two nodes, and how each direction of it is carried."""

    KEYS = ("from", "to", "medium", "up", "down", "note")

    def __init__(self, raw, index, by_id):
        raw = _dict(raw, f"links[{index}]")
        _known(raw, self.KEYS, f"links[{index}]")
        self.index = index
        a, b = str(raw.get("from") or ""), str(raw.get("to") or "")
        for who, name in (("from", a), ("to", b)):
            if not name:
                raise TopologyError(f"links[{index}] has no {who!r}")
            if name not in by_id:
                raise TopologyError(f"links[{index}]: {who} {name!r} is not a node "
                                    f"(nodes: {', '.join(by_id)})")
        if a == b:
            raise TopologyError(f"links[{index}]: {a!r} is linked to itself")
        self.a, self.b = by_id[a], by_id[b]
        med = raw.get("medium", "tcp")
        if isinstance(med, dict):
            _known(med, ("up", "down"), f"links[{index}]: medium")
            up, down = med.get("up"), med.get("down")
        else:
            up = down = med
        up, down = raw.get("up", up), raw.get("down", down)     # flat spelling also works
        self.up, self.down = self._medium(up, "up"), self._medium(down, "down")
        self.note = raw.get("note", "")

    def _medium(self, m, which):
        m = str(m or "tcp").strip().lower()
        if m in ("usrp", "radio", "rf", "air", "ota"):
            m = "wireless"
        if m in ("ip", "ethernet", "tcp/ip"):
            m = "tcp"
        if m not in MEDIA:
            raise TopologyError(f"links[{self.index}]: {which} medium {m!r} — "
                                f"use one of {', '.join(MEDIA)}")
        return m

    def media(self):
        return {self.up, self.down}

    def describe(self):
        arrow = f"{self.a.id} -> {self.b.id}"
        return (f"{arrow:24s} {self.up}" if self.up == self.down
                else f"{arrow:24s} up={self.up} down={self.down}")


class Topology:
    """A loaded, validated wiring file. Node order is node INDEX: the 0-based id every
    graph runner already speaks (gossip_edges, PeerLink, --node K)."""

    KEYS = ("schema", "name", "algo", "description", "defaults", "nodes", "links", "note")

    def __init__(self, raw, path=None):
        raw = _dict(raw, "topology")
        _known(raw, self.KEYS, "topology")
        self.path = path
        got = raw.get("schema")
        if got is not None and int(got) != SCHEMA:
            raise TopologyError(f"schema {got} — this build reads schema {SCHEMA}")
        self.name = str(raw.get("name") or (os.path.splitext(os.path.basename(path))[0]
                                            if path else "topology"))
        self.algo = raw.get("algo")
        self.description = raw.get("description", "")
        self.defaults = _dict(raw.get("defaults"), "defaults")
        nodes = raw.get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 1:
            raise TopologyError("topology needs a nodes[] list")
        # "n0" is shorthand for {"id": "n0"} — a plain peer graph stays a one-liner
        self.nodes = [Node(n if isinstance(n, dict) else {"id": n}, i)
                      for i, n in enumerate(nodes)]
        self.by_id = {}
        for nd in self.nodes:
            if nd.id in self.by_id:
                raise TopologyError(f"two nodes share the id {nd.id!r}")
            self.by_id[nd.id] = nd
        self.links = self._links(raw.get("links", "ring"))
        self._validate()

    # ── links: an explicit list, or the two shorthands the CLI already has ──
    def _links(self, spec):
        n = len(self.nodes)
        if isinstance(spec, str):
            t = spec.strip().lower()
            if t == "ring":
                pairs = [(0, 1)] if n == 2 else [(i, (i + 1) % n) for i in range(n)]
            elif t == "full":
                pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
            elif t == "star":
                pairs = [(0, i) for i in range(1, n)]     # node 0 is the hub
            else:
                raise TopologyError(f"links {spec!r} — use ring, full, star, or a list")
            med = self.defaults.get("medium", "tcp")
            spec = [{"from": self.nodes[i].id, "to": self.nodes[j].id, "medium": med}
                    for i, j in pairs]
        if not isinstance(spec, list):
            raise TopologyError("links must be a list, or ring/full/star")
        out, seen = [], set()
        for i, raw in enumerate(spec):
            if isinstance(raw, str):                       # "c0-srv" or "c0->srv"
                bits = [b for b in re.split(r"->|-|:", raw) if b]
                if len(bits) != 2:
                    raise TopologyError(f"links[{i}]: {raw!r} is not 'a-b'")
                raw = {"from": bits[0].strip(), "to": bits[1].strip(),
                       "medium": self.defaults.get("medium", "tcp")}
            ln = Link(raw, i, self.by_id)
            key = tuple(sorted((ln.a.id, ln.b.id)))
            if key in seen:
                raise TopologyError(f"links[{i}]: {ln.a.id}-{ln.b.id} is listed twice")
            seen.add(key)
            out.append(ln)
        if not out:
            raise TopologyError("topology has no links")
        return out

    # ── the checks worth having: every one of these is a real failure on a testbed ──
    def _validate(self):
        for ln in self.links:
            # a direction over the air needs a transmitter at one end and a receiver at
            # the other. THIS is the check that catches "we have no antennas".
            for (src, dst, med, which) in ((ln.a, ln.b, ln.up, "up"),
                                           (ln.b, ln.a, ln.down, "down")):
                if med == "wireless":
                    if not src.can_tx():
                        raise TopologyError(
                            f"link {ln.a.id}-{ln.b.id} ({which}) is wireless, but node "
                            f"{src.id} has no radio.tx — it cannot transmit. Give it a "
                            f"transmit section, or carry that direction over tcp.")
                    if not dst.can_rx():
                        raise TopologyError(
                            f"link {ln.a.id}-{ln.b.id} ({which}) is wireless, but node "
                            f"{dst.id} has no radio.rx — it cannot receive. Give it a "
                            f"receive section, or carry that direction over tcp.")
                if med == "lora" and not (src.lora or dst.lora):
                    raise TopologyError(
                        f"link {ln.a.id}-{ln.b.id} ({which}) is lora, but neither "
                        f"{src.id} nor {dst.id} has a lora section")
        # two processes on ONE host cannot share a listening port — the second one dies
        # with EADDRINUSE halfway through a run that looked fine when it started
        used = {}
        for nd in self.nodes:
            for which in ("net", "peer"):
                if which in nd.ports:
                    key = (nd.host or "127.0.0.1", nd.ports[which])
                    if key in used:
                        raise TopologyError(
                            f"nodes {used[key]} and {nd.id} both listen on "
                            f"{key[0]}:{key[1]} ({which}) — give them different ports")
                    used[key] = nd.id
        # a node that is dialled has to be reachable: peers over tcp need its host
        for ln in self.links:
            for (src, dst, med) in ((ln.a, ln.b, ln.up), (ln.b, ln.a, ln.down)):
                if med == "tcp" and not dst.host and not src.is_local():
                    raise TopologyError(
                        f"link {ln.a.id}-{ln.b.id} is tcp, but node {dst.id} has no host "
                        f"and {src.id} is not local — give {dst.id} a host, or pass one "
                        f"at launch with --peers/--net-host")

    # ── the graph, in the vocabulary the runners already speak ──
    def edges(self):
        """Ordered index pairs — the same thing gossip_edges() returns, in FILE ORDER.
        That order IS the schedule: one exchange at a time, so a half-duplex radio is
        never asked to transmit and receive at once."""
        return [(ln.a.index, ln.b.index) for ln in self.links]

    def edge_spec(self):
        """The graph as --topology's own edge-list string, e.g. '0-1,1-2,2-0'."""
        return ",".join(f"{i}-{j}" for i, j in self.edges())

    def node(self, which):
        """Look a node up by name, or by 0-based index — both spellings of --node."""
        if isinstance(which, int) or (isinstance(which, str) and which.strip().isdigit()):
            i = int(which)
            if not (0 <= i < len(self.nodes)):
                raise TopologyError(f"--node {i} is outside 0..{len(self.nodes) - 1} "
                                    f"({self.name} has {len(self.nodes)} nodes)")
            return self.nodes[i]
        key = str(which).strip()
        if key not in self.by_id:
            raise TopologyError(f"--node {key!r} is not in {self.name} "
                                f"(nodes: {', '.join(self.by_id)})")
        return self.by_id[key]

    def links_of(self, nd):
        return [ln for ln in self.links if nd.id in (ln.a.id, ln.b.id)]

    def peers_of(self, nd):
        return [(ln.b if ln.a.id == nd.id else ln.a) for ln in self.links_of(nd)]

    def role_group(self, nd):
        """(index among nodes of the same role, how many there are) — a client's shard
        id and the client count the server aggregates over, without either being typed."""
        same = [x for x in self.nodes if x.role == nd.role]
        return same.index(nd), len(same)

    def hub(self):
        """The one node every other links TO — the server of a star. None if there is no
        such node (a ring has none, and that is not an error)."""
        for nd in self.nodes:
            others = [x for x in self.nodes if x.id != nd.id]
            if others and all(nd.id in (ln.a.id, ln.b.id) for ln in self.links) \
                    and len(self.links_of(nd)) == len(self.links):
                return nd
        return None

    def medium_of(self, nd):
        """The media this node's links use, as a set — a node cannot be attached two
        ways at once, and finding out mid-run is worse than finding out here."""
        med = set()
        for ln in self.links_of(nd):
            med |= ln.media()
        return med

    def peer_link(self, nd):
        """How THIS node exchanges with its neighbours: tcp | wireless | lora."""
        med = self.medium_of(nd)
        if len(med) > 1:
            raise TopologyError(f"node {nd.id} has links over {', '.join(sorted(med))} "
                                f"— one node is attached one way; split it into two "
                                f"topologies or give the links one medium")
        return (med or {"tcp"}).pop()

    def summary(self):
        out = [f"topology {self.name}"
               + (f" ({self.description})" if self.description else "")
               + (f"  [{self.path}]" if self.path else "")]
        for nd in self.nodes:
            r = nd.radio
            radio = "no radio"
            if r:
                bits = []
                if r["tx"] is not None:
                    bits.append(f"TX {r['tx'].get('ant', 'TX/RX')}/{r['tx'].get('subdev', 'A:A')}")
                if r["rx"] is not None:
                    bits.append(f"RX {r['rx'].get('ant', 'RX2')}/{r['rx'].get('subdev', 'A:0')}")
                radio = f"{r['device'] or 'usrp'} {r['args']} [{', '.join(bits)}]"
            ports = ",".join(f"{k}:{v}" for k, v in sorted(nd.ports.items())) or "-"
            out.append(f"  [{nd.index}] {nd.id:<10} role={nd.role:<8} "
                       f"host={nd.host or '(unset)':<15} ports={ports:<20} {radio}")
        for ln in self.links:
            out.append(f"  link  {ln.describe()}")
        return "\n".join(out)


# ── loading ───────────────────────────────────────────────────────────────────
def search_path():
    out = []
    for p in SEARCH:
        if p.startswith("$"):
            p = os.environ.get(p[1:], "")
        if p:
            out.append(p)
    return out


def resolve(name):
    """A path, or a bare name looked up in the topology folders. Returns None if the
    string is not a file anywhere — the caller then reads it as ring/full/an edge list."""
    if not name:
        return None
    cand = [name]
    if not name.endswith(".json"):
        cand.append(name + ".json")
    for c in cand:
        if os.path.sep in c or c.startswith("."):
            if os.path.isfile(c):
                return os.path.abspath(c)
    for d in search_path():
        for c in cand:
            p = os.path.join(d, c)
            if os.path.isfile(p):
                return p
    if os.path.isfile(name):
        return os.path.abspath(name)
    return None


# an explicit edge list and nothing else: "0-1", "0-1,1-2,2-0", "0:1;1:2"
EDGE_LIST = re.compile(r"^\s*\d+\s*[-:]\s*\d+\s*([,;]\s*\d+\s*[-:]\s*\d+\s*)*$")


def looks_like_file(name):
    """Is this --topology value MEANT to be a file? Anything that is not one of the
    built-in graphs and not an edge list is a name we should have found on disk — so a
    typo in a filename reports 'no such topology' instead of 'unknown topology'. A file
    name may perfectly well contain digits (dl-ring3-tcp), which is why this matches the
    edge-list SHAPE rather than looking for a digit anywhere in the string."""
    t = str(name or "").strip().lower()
    if t in ("ring", "full", ""):
        return False
    return not EDGE_LIST.match(t)


def load(name):
    path = resolve(name)
    if path is None:
        raise TopologyError(
            f"no topology {name!r} — looked in {', '.join(search_path())}. "
            f"Built-in graphs are ring and full, or give an edge list like 0-1,1-2.")
    with open(path) as fh:
        try:
            raw = json.load(fh)
        except ValueError as e:
            raise TopologyError(f"{path}: not valid JSON — {e}")
    return Topology(raw, path=path)


def load_if_file(name):
    """-> Topology, or None when --topology names a built-in graph / an edge list."""
    if not looks_like_file(name):
        return None
    return load(name)


def available():
    out = []
    for d in search_path():
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".json"):
                    out.append(os.path.join(d, f))
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("topologies found:")
        for p in available():
            print("  " + p)
            try:
                print("      " + load(p).description)
            except TopologyError as e:
                print(f"      INVALID: {e}")
        sys.exit(0)
    try:
        print(load(sys.argv[1]).summary())
    except TopologyError as e:
        sys.exit(f"topology: {e}")
