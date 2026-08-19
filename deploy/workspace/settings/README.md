# settings/ — the devices this account has reserved

One file per reservation (e.g. `lab_a.json`), describing **only the devices actually
reserved** — not everything physically present on a testbed. A settings file is an
inventory: stable, hand-authored, reusable across many experiments.

## What is settled

- Keyed by a stable `node_id`. That id is the join key every other file refers to, so a
  map (not a list) is preferred: duplicate ids then become structurally impossible.
- Per node, the facts that identify hardware: radio serial, the UHD device args needed to
  reach it, which PHY it can drive.
- A **declared** IP is allowed where one is genuinely static (a lab host, an N210 on its
  own subnet). A **discovered** IP — a session pod's address — must never be written back
  here; see the parent README.

## OPEN — the reservation → file flow

How a settings file comes into existence is **not designed yet**. The candidates:

- hand-authored, committed alongside the experiment;
- generated from the platform's reservation (the portal knows what was granted);
- generated on the node by probing (`uhd_find_devices` already reports serials, and the
  radio's serial is arguably the truest node identity in an SDR lab);
- some combination — generated, then edited.

This choice decides whether the file is an *input* or an *artifact*, which in turn
decides whether it belongs in git, in the workspace, or both. Nothing else should be
specified until it is made.

## OPEN — does `role` belong here?

A node that transmits in one experiment receives in the next, so pinning a role in the
inventory makes the inventory single-use. A default here, overridable per topology, is
the likely answer — but see `../topologies/README.md`, where roles are genuinely hard.
