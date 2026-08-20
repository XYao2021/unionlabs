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
| `ports.ack` | where the USRP ARQ acknowledgement is collected (default 5599) |
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

### defaults

Experiment-wide knobs, applied to every node unless typed on the command line:
`steps`, `channel`, `scheme`, `fec`, `waveform`, `freq_mhz`, `samp_rate`, `symbol_rate`,
`sim_snr_db`, `arq`, `max_attempts`, `ack_transport`, `ack_timeout`, `peer_port`, and
`medium` (the default for links given in shorthand).

---

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
- both directions of a point-to-point link over the air: the USRP round trip carries its
  reply over TCP, so this is refused rather than half-honoured. Two peers of a
  decentralised graph MAY use RF both ways — they take turns.

`union/test_topology.py` walks each of these, and each setting's full path (file in →
constructed link out); `./run.sh selftest` runs it, and then runs `fl-star-tcp` and
`dl-ring3-tcp` for real, one process per node.
