# The shared workspace layout

`/workspace` is mounted by the platform and is visible to **every session and node
reserved under the same account, across testbeds**. That makes it the one place several
machines can agree on without any of them being reachable from the others — which is why
experiment configuration lives here rather than in the image or in a per-session home.

This folder is the **versioned source** of that layout. `init-workspace.sh` materialises
it into `/workspace/experiments/` on a testbed and never overwrites anything already
there.

```
/workspace/experiments/
  algorithms/      the algorithms themselves (fl, dl, ...)      (seeded from the repo)
  env/             the account's libraries: requirements.txt -> a persistent venv
  settings/        which devices this account has RESERVED      (schema: OPEN)
  topologies/      the wiring of an experiment                  (schema: v1, in use)
  cross_channel/   carrying a link BETWEEN testbeds             (design: OPEN)
```

`algorithms/` holds the runnable experiment code. `init-workspace.sh` seeds each
algorithm folder from the repo checkout once and never overwrites it, so an
algorithm the account edits in the shared workspace stays as edited; `run.sh`
looks here FIRST and falls back to the repo's own copy
(`deploy/workspace/algorithms/`) when no workspace is mounted.

`topologies/` is live: `./run.sh --algo fl --topology <name> --node <id>` reads it, and
`./run.sh topology <name>` starts every node of it that lives on the machine you are on.
See its README for the schema and the four examples `init-workspace.sh` seeds.

The three are separate on purpose: they change at different rates, and separating them
lets one settings file compose with many topologies instead of being copied into each.

| Folder | Answers | Changes when |
|---|---|---|
| `settings/` | what is reserved, and what each device is | the reservation changes |
| `topologies/` | who exchanges with whom, over what, on which port and connector | every experiment |
| `algorithms/` | what the nodes actually run | when the code changes |
| `env/` | the account's extra libraries, installed once into a persistent venv | when a user adds a line |
| `cross_channel/` | how an inter-testbed link is carried | per cross-testbed run |

## Two constraints that already hold

**1. Nothing baked into the image survives here.** The platform mounts its own volume
over `/workspace`, hiding whatever the image wrote there — a build marker and a helper
script were both lost to exactly this before it was understood. Everything under
`/workspace` is written at runtime or by hand.

**2. Authored and discovered state must not mix.** A session pod's address changes every
session (`10.42.0.106` → `.107` → …). An address discovered at runtime and written back
into an authored file is stale by the next session, and worse, looks authoritative. Keep
authored files hand-written and stable; runtime facts belong somewhere else.

## Open: where runtime state lives

Nodes will need to publish their *current* address, claim their identity, and rendezvous
before a run — none of which belongs in the three authored folders above. A per-run area
(`runs/<run-id>/`) is the obvious candidate, and run-id namespacing matters: without it a
re-run reads the previous run's registrations and dials pods that no longer exist. Not
created yet, because the shape depends on the topology decisions below.

## Open questions, per folder

See each folder's README. `topologies/` is settled and read by `run.sh`; `settings/` is
written by `discover-node.py` and describes attachment rather than reservation;
`cross_channel/` is still a design.
