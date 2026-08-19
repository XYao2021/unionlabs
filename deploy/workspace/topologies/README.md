# topologies/ — who exchanges with whom

One file per graph (e.g. `ring4.json`), naming nodes by the `node_id` used in a
`settings/` file. A topology is an experiment-level choice and is expected to change
often, which is why it is separate from the inventory.

## What already exists in the code

`union/phy_link.py: gossip_edges(n, topology)` is the current grammar, and any file
format should expand into exactly what it produces rather than becoming a second path:

```
ring (default)   0-1-2-…-0     each peer talks to 2 neighbours,  n edges
full             every pair    n(n-1)/2 edges
custom           "0-1,1-2,2-0" an explicit edge list — any graph
```

Two properties of it matter for the design below. Edges are **undirected**, and its
docstring already fixes the semantics: *one edge == one symmetric exchange (each end
sends its own payload to the other)*. Nodes are referred to by **integer index**; a file
format would use names instead, which is the easy part.

## OPEN — the hard part: decentralised nodes are transmitter AND receiver

The role vocabulary today (`tx`, `rx`, `relay`, `peer`, plus the whole-network roles
`loopback`, `chain`, `gossip`, `multi`) is directional for the two-node case and
symmetric for `peer`/`gossip`. A decentralised experiment needs every node to send and
receive **simultaneously**, which raises questions no file schema can paper over:

- Is an edge a **link** (symmetric, both ends transmit — what `gossip_edges` means) or a
  **direction** (one end transmits, the other receives)? Both are legitimate; a graph
  needs to say which it is, and a single experiment may want both kinds of edge.
- If every node both transmits and receives on the same PHY, what serialises them? Two
  nodes transmitting at once collide on a shared medium; a schedule, a slot assignment,
  or a contention protocol has to exist somewhere. Is that the topology's business, the
  algorithm's, or the PHY driver's?
- Does `role` then survive at all for decentralised runs, or is it derived — "a node with
  neighbours in a symmetric graph is a peer", full stop?
- Half-duplex hardware: one USRP cannot transmit and receive at the same instant. A graph
  that implies simultaneous bidirectional exchange must resolve to something the radio
  can actually do.

Answering these is the next design task. Until then no schema is committed here, because
the schema is downstream of the answer — an adjacency map with a `directed` flag is only
correct if "directed" turns out to be the right axis.
