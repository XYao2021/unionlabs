# cross_channel/ — carrying a link BETWEEN testbeds

For experiments whose nodes are **not on the same testbed** — federated learning with
clients on separate testbeds being the motivating case.

## This is not the `--channel` concept

`--channel ideal|usrp|lora` selects the **PHY that carries payloads** between nodes that
can already reach each other. `cross_channel` is a different thing entirely: it is about
two nodes that have **no path to each other at all**, because each lives inside its own
cluster (`10.42.x` addresses are cluster-local and not routable between testbeds).

Sharing the name would be actively misleading, so whatever this becomes should not be
plumbed through `--channel`.

## What is known

- The shared `/workspace` is visible from **both** testbeds when reserved under the same
  account, while the network is not. That makes the workspace the one medium guaranteed
  to span the gap — file-based, seconds-granularity, and adequate for coordination and
  model exchange even though it would be absurd for sample data.
- Whatever carries a cross-testbed link, only *some* edges of an experiment need it. A
  run may be ordinary within each testbed and cross only at one or two edges, so this is
  naturally an **overlay on a topology**, not a replacement for one.

## OPEN — everything else

The logic differs from same-testbed communication and has not been designed: what the
unit is (an edge? a node pair? a group?), whether a public/routable endpoint is used when
one exists, how a file-based exchange is made atomic and ordered, how slow coordination
interacts with an algorithm's round structure, and what the failure semantics are when
one testbed's session ends mid-run.

No schema here until that design exists.
