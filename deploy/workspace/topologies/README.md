# topologies/ — the wiring of an experiment, as a file

One file per experiment: **who the nodes are, what radio each one owns, which connector
and RF channel it uses, which port it listens on, and how every link between them is
carried.** Each node reads the same file and asks it one question — *which node am I* —
and the answer is the rest of its command line.

```bash
./run.sh --algo fl --topology fl-star-tcp --node srv     # on the server's machine
./run.sh --algo fl --topology fl-star-tcp --node c0      # on the first client's
./run.sh --algo fl --topology fl-star-tcp --node c1      # on the second's

./run.sh topology fl-star-tcp        # or: start every node that lives on THIS machine
./run.sh --algo fl --topology fl-star-tcp --node c0 --print-plan   # resolve, don't run
./run.sh topologies                  # list the files, with their descriptions
```

**Why a file at all.** A multi-node run is typed on several machines at once and all of
them have to agree — on the graph, on the ports, on which end transmits. Typed per node,
they disagree: a client dials 5700 while the server serves 5701, or a node is told to
transmit on a radio that is receive-only. The run then fails minutes later with an error
naming the wrong layer. One file makes the disagreement impossible to express, and
`--print-plan` shows what each node resolved to before anything is started.

Anything typed on the command line still wins over the file, so a one-off change
(`--steps 40`, `--tx-gain 65`) needs no edit.

---

## The example to start from

`fl-star-tcp.json` — federated learning, one server and two clients, **every link over
plain TCP/IP**, which is the wiring for a session with no antenna attached:

```json
{
  "schema": 1,
  "name": "fl-star-tcp",
  "algo": "fl",
  "defaults": { "steps": 20, "medium": "tcp" },
  "nodes": [
    { "id": "srv", "role": "server", "host": "127.0.0.1", "ports": { "net": 5700 } },
    { "id": "c0",  "role": "client", "host": "127.0.0.1" },
    { "id": "c1",  "role": "client", "host": "127.0.0.1" }
  ],
  "links": [
    { "from": "c0", "to": "srv", "medium": "tcp" },
    { "from": "c1", "to": "srv", "medium": "tcp" }
  ]
}
```

Change the two `host` fields to the real addresses and the same file runs across
machines. The four files here are meant to be copied and edited:

| File | What it wires |
|---|---|
| `fl-star-tcp.json` | FedAvg, 1 server + 2 clients, all TCP/IP — **no radio needed** |
| `fl-star-radio.json` | the same star on the lab rig: TX-only B210s → RX-only N210, reply over TCP |
| `fl-chain-tcp.json` | a 3-node chain, client → relay → server, all TCP/IP |
| `fl-chain-mixed.json` | the same chain with **one hop over the air and the next over Ethernet** |
| `fl-star-crossnode.json` | the radio star **across machines**: bind in the pod, publish on the node's IP |
| `dl-ring3-tcp.json` | decentralised learning, 3 peers in a ring, all TCP/IP |
| `dl-pair-wireless.json` | two peers exchanging over the air, taking turns |

---

## The schema (v1)

### nodes[]

| Key | Meaning |
|---|---|
| `id` | this node's name. `--node <id>` selects it; it is also the node's 0-based INDEX, by position in the list |
| `role` | what it IS, in the algorithm's own vocabulary — fl's `client` / `server` / `relay`, dl's `peer`. Whatever the algorithm declares in its `ROLES` map |
| `host` | where the process runs, as the OTHER nodes must dial it. Omit it when the address is not knowable in advance and pass `--peers` / `--net-host` at launch |
| `ports.net` | the TCP port this node SERVES to whoever it answers (server / relay) |
| `ports.peer` | a decentralised node's own port. Node k must be `base + k` — that is the rule `PeerLink` actually follows, and a file that breaks it is refused |
| `ports.ack` | the TCP port this node's USRP ARQ acknowledgement travels on (default 5599). Two radio nodes sharing a host need different ones |
| `ports.down` | a relay only: the port of the next hop, when it is not that node's `ports.net` |
| `advertise` | `{ "host", "ports" }` — what OTHERS dial, when a NodePort or a port mapping renumbers what this node binds. See below |
| `radio` | the USRP this node owns — **omit it entirely when the node has no radio** |
| `lora` | the LoRa radio's `backend` / `port` / `sf` / `cr` / `bw` / `power` |

### nodes[].radio

```json
"radio": {
  "device": "b210",
  "args":   "serial=30CD424",
  "tx": { "ant": "TX/RX", "subdev": "A:A", "gain": 78 },
  "rx": { "ant": "RX2",   "subdev": "A:0", "gain": 25 }
}
```

`args` is what UHD needs to find it (`serial=…` for a B210, `addr=…` for an N210/X310);
`serial` / `addr` may be given as their own keys instead. `device` is documentation.

**A direction that is absent means the node cannot use it.** Our N210 is receive-only, so
it carries an `rx` section and no `tx`, and any link asking it to transmit is refused at
load rather than by UHD three minutes into a run. An empty section (`"tx": {}`) means
"yes, with the usual defaults". `ant` is the CONNECTOR (`TX/RX`, `RX2`), `subdev` the RF
channel (a B210 has two: `A:A` = RF A, `A:B` = RF B; an N210/X310 has `A:0`).

### links[]

```json
{ "from": "c0", "to": "srv", "medium": { "up": "wireless", "down": "tcp" } }
```

| Key | Meaning |
|---|---|
| `from` / `to` | the two ends. The pair is ORDERED: `from` speaks first |
| `medium` | `tcp` \| `wireless` \| `lora` for both directions, or `{ "up": …, "down": … }` per direction — **up is from → to** |

`links` may also be the shorthand `"ring"`, `"full"` or `"star"` (node 0 is the hub), and
an entry may be the short string `"c0-srv"`. The medium then comes from
`defaults.medium`.

The per-direction split is not decoration. It is the lab rig exactly: the client
transmits over the air (`up: wireless`) and the server answers over TCP (`down: tcp`),
because the N210 never transmits — the same thing `fl.py --uplink wireless --downlink
tcp` has always meant.

### The medium belongs to the LINK, not to the experiment

Every hop chooses its own carrier, so a chain can change medium half way:

```json
"links": [
  { "from": "n1", "to": "n2", "medium": { "up": "wireless", "down": "tcp" } },
  { "from": "n2", "to": "n3", "medium": "tcp" }
]
```

`n1` transmits to `n2` over the air and `n2` answers over TCP; `n2` then forwards to `n3`
over TCP entirely. That is `fl-chain-mixed.json`, and it is the shape our rig actually
allows: one B210 transmitting, one RX-only N210 receiving, and everything past the N210
reached over Ethernet — because the N210 has nothing to transmit with.

The middle node's two hops are read from the links themselves: the link where it is `to`
is its **upstream** hop, the link where it is `from` is its **downstream** hop. So
`from → to` points downstream, along the direction data travels.

| Its hops | What carries them |
|---|---|
| wireless in, wireless out | `RadioRoundTrip` — the all-RF relay that already existed |
| **wireless in, tcp out** | `ChainRelay` — receives over the ARQ byte-pipe, forwards over TCP |
| **tcp in, wireless out** | `ChainRelay` — the other way round |
| tcp in, tcp out | `ChainRelay` — no radio anywhere in the chain |

Each leg speaks whatever its NEIGHBOUR speaks, which is why the medium cannot be a
property of a node: `n2`'s downstream leg is the client of `n3`'s server, so it must
frame its bytes the way `n3` reads them.

**Replies always come back over TCP.** `down: wireless` is refused, by name, rather than
half-honoured — the RX-only N210 never transmits, which is the whole reason the split
exists. Two peers of a decentralised graph are the exception: they take turns, so
`dl-pair-wireless.json` may use RF in both directions.

Without a file the same thing is reachable by flag:
`--link chain --up-medium wireless --down-medium tcp`.

### defaults

Experiment-wide knobs, applied to every node unless typed on the command line:
`steps`, `channel`, `scheme`, `fec`, `waveform`, `freq_mhz`, `samp_rate`, `symbol_rate`,
`sim_snr_db`, `arq`, `max_attempts`, `ack_transport`, `ack_timeout`, `peer_port`, and
`medium` (the default for links given in shorthand).

---

## Across machines: what a node BINDS vs what others DIAL

A container never receives traffic sent to its host's IP, so a cross-machine run needs
each listener published — `deploy/testbed/expose-my-port.sh 5599 35999` asks Kubernetes
for a NodePort Service pointing at this pod. **That renumbers the port**: the node listens
on 5599 inside its pod and callers must dial 35999 on the node's IP. One number per port
cannot say both, so a node may carry an `advertise` block:

```json
{ "id": "srv", "host": "10.42.0.107",
  "ports":     { "net": 5700,  "ack": 5599  },
  "advertise": { "host": "10.10.1.23", "ports": { "net": 35700, "ack": 35999 } } }
```

`ports` is what this node listens on; `advertise` is what everyone else dials. Without an
`advertise` block the two are the same, which is the ordinary same-network case. Pin the
nodePorts (`expose-my-port.sh 5599 35999`) so the file stays true across sessions; the
node's IP is not authored state and usually still arrives as `--net-host` / `--ack-host`
at launch.

Decentralised peers normally work out each other's ports as `base + k`. A NodePort is
whatever the cluster handed out, so when peers are published the dial ports travel as a
list (`--peer-ports 30801,30907`) while each node still LISTENS on `base + its own id`.

### How many ports must a host publish? Count its LISTENERS

| Node | Binds — publish these | Dials |
|---|---|---|
| transmits over the air (a B210 client) | **nothing** | the sink's `ack` and `net` |
| receives over the air (the N210 server) | **`ack` 5599 + `net` 5700** | — |
| TCP client | nothing | the hub's `net` |
| TCP server / hub | `net` 5700 | — |
| relay, air in → TCP out | **`ack` 5599 + `net` 5700** | the next hop's `net` |
| decentralised peer | its own `peer` port | its neighbours' |

So a machine that RECEIVES over the air needs two published ports, and a machine that only
transmits needs none. Which side listens is not a convention — `drivers/usrp/src/main.cpp`
has `sink_arq` call `accept_one(ack_port)` and `source_arq` call `connect_to(ack_host,
ack_port)`. Several listening nodes on one machine need one published port each, on
different numbers, which is what per-node `ports` is for.

## What the file does NOT contain

**Discovered state.** A session pod's address changes every session, so an address that
was *probed* belongs in `../settings/`, which is generated per session and carries a
heartbeat. A `host` here is an AUTHORED fact — a lab machine's static IP, or `127.0.0.1`
for several processes on one box. Where the address cannot be known in advance, leave
`host` out and pass `--peers` / `--net-host` at launch; a link that needs a host it has
not got says so, by node name.

---

## The three questions this schema answers

The earlier version of this README left them open, because the schema is downstream of
the answers. They are:

**Is an edge a link or a direction?** Both, and the pair is ORDERED. Data flows both ways
over a link — one edge is one exchange — but `from` is the end that speaks first. That is
already what `union/phy_link.py: gossip_edges()` means and what `PeerLink` schedules
("the node named first in the edge sends"). Direction survives where it is physically
real: as the medium of each direction, and as the roles at the two ends.

**What serialises two nodes that would transmit at once?** The link ORDER in this file.
One exchange completes before the next begins, so a half-duplex radio is never asked to
transmit and receive at the same instant, and two nodes never share the air. `edges()`
preserves file order precisely so that the file is the schedule.

**Does `role` survive for decentralised runs?** Yes — as what a node IS, in the
algorithm's vocabulary. It is not derived from the graph, because the graph cannot tell a
`server` from a `client` when both have one link. What IS derived is everything physical:
which transport a node uses, what it dials, what it listens on.

---

## What is checked when a file is loaded

Every one of these is a run that would otherwise fail minutes later, on a testbed, with
an error naming the wrong layer:

- a wireless direction whose transmitter has no `radio.tx`, or whose receiver has no
  `radio.rx` — **the "we have no antennas" check**
- two nodes on the same host claiming the same listening port
- peer ports that are not `base + index`
- a link naming a node that does not exist, a self-link, a duplicate link
- a misspelled key anywhere (silently ignoring one is how a run ends up wired the way
  nobody asked)
- a `tcp` link to a node with no `host`, where the caller is not local
- a reply direction (`down`) asked to go over the air: every transport here carries the
  reply over TCP. Two peers of a decentralised graph MAY use RF both ways — they take
  turns.
- a node receiving over two different media at once, or transmitting over two — a node is
  attached one way per direction

`union/test_topology.py` walks each of these, and each setting's full path (file in →
constructed link out); `./run.sh selftest` runs it, and then runs `fl-star-tcp` and
`dl-ring3-tcp` for real, one process per node.
