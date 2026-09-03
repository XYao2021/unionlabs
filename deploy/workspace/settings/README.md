# settings/ — one generated record per live node

**These files are generated, not hand-authored.** Each live session publishes
`<node_id>.json` here describing itself: the USRPs it can see, its addresses, the PHYs
available to it. `discover-node.py` writes it at session start and refreshes it while
the session lives.

Because `/workspace` is shared by every session reserved under the account — including
across testbeds that cannot reach each other over the network — this folder assembles
into a fleet-wide inventory with nobody collecting it.

```json
{ "schema": 1, "node_id": "3169C62", "serial": "3169C62", "ip": "192.168.10.2",
  "role": null, "host": "1-gnuradio-0", "heartbeat_utc": "2026-08-19T10:22:31Z",
  "radios": [ { "serial": "3169C62", "addr": "192.168.10.2", "type": "usrp2" } ],
  "interfaces": [ { "name": "eth0", "cidr": "10.42.0.107/32" },
                  { "name": "n210v", "cidr": "192.168.10.100/24" } ],
  "lora_ports": [], "uhd": "UHD 4.1.0.5-3" }
```

## Lifecycle: a record disappears when its node does

A session usually dies by SIGKILL — pod eviction, `docker kill`, a node reboot — and the
platform's container contract leaves no shutdown hook. A file that had to be *deleted*
to stay correct would therefore sometimes be wrong, advertising a node that no longer
exists.

So liveness is asserted rather than cleaned up. Every record carries a `heartbeat` its
owner refreshes (default every 30 s):

| Age of `heartbeat` | Meaning |
|---|---|
| fresh | the node is live; use it |
| stale | the node is gone; readers must ignore the record |
| older than `--gc` (default 1 h) | any live session deletes it |

Deletion also happens immediately on a *graceful* stop, but nothing depends on that.
Records are written temp-then-renamed, so a reader on a shared filesystem never sees a
half-written file; a record that is corrupt or unreadable is aged out by its mtime.

Re-probing on every beat also fixes a real race: the radio NIC is attached to a session
pod a few seconds **after** it starts, so the first probe can legitimately find nothing
and a later one picks the radio up. The record's `node_id` follows: a node that gains a
radio moves from its `host-<hostname>` fallback name to the radio serial, and the old
record is removed rather than left behind.

## What cannot be probed

`node_id` and `role` are inputs, not facts — a radio cannot say what an experiment calls
it or what job it is doing. Both default to empty (`node_id` falls back to the radio
serial, which is stable and physical, unlike the hostname, which is the container id and
changes every session). Set them with `--node-id` / `--role`, or `WS_NODE_ID` / `WS_ROLE`.

## OPEN — reserved vs merely visible

A probe reports what is *attached*, which is not the same as what the account
**reserved**. Until the reservation flow is designed, these records describe attachment
only. See `../README.md`.

## Usage

```bash
discover-node.py --once                      # probe, write, exit
discover-node.py --daemon                    # refresh + GC for the life of the session
discover-node.py --once --node-id n1 --role tx
```

The session hook starts the daemon automatically; run it by hand only to re-probe
immediately (for example right after a radio is attached).

## reservation.json — what SHOULD be here (authored, not generated)

Every file above is *generated* by a live session. `reservation.json` is the exception:
it is **hand-authored**, and it is the top of the PHY pipeline. Copy the template and fill it:

```bash
cp deploy/workspace/settings/reservation.template.json \
   /workspace/experiments/settings/reservation.json
# edit it: your reserved radios, each with role rx or tx
./calibration.sh --show-reservation      # read it back
```

It exists because the *transmitter is never surveyed* — a receive-only survey says nothing
about a TX — so nothing discovers it. The reservation is the one place a TX radio is named.

```
reservation.json  ->  prepare.sh --args <rx>   surveys the receiver, picks the carrier
                  ->  calibration.sh --plan     reads the rx/tx serials from here
                  ->  calibration.sh            measures the sync threshold
                  ->  radio.sh / run.sh         read the finished profile
```

`./calibration.sh --plan` with no `--rx-serial`/`--tx-serial` fills them from this file, so
once it is filled you never retype the serials. It is manually maintained until the testbed's
reservation feed populates it; an unfilled template (values still in `<…>`) is ignored, not
accepted. `reserved_local` is a note to yourself of when you last edited it — not load-bearing.
