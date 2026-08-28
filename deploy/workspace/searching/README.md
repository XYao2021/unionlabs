# `searching/` — what a testbed measured about its own RF environment

One file per radio, written by `./prepare.sh` (which runs
`drivers/usrp/python/prepare_phy.py`) and read back automatically by `run.sh` and
`radio.sh`:

    phy-<key>-<band>.json   the usable carriers, the noise floor, and the
                            detector thresholds measured through that antenna

`<band>` is there because one radio can carry two antennas — a VERT900 on one
port and a VERT2450 on another — and each needs its own survey. Keyed on the
serial alone, the second measurement silently replaced the first and the survivor
was whichever ran last. When a radio has more than one, name it with `--band` /
`$UNION_BAND`, or just type a `--freq` inside the band you mean and the right
survey is chosen; with no way to tell, the resolver reports the ambiguity instead
of picking one.

`<key>` is the radio's **serial** — stable and physical. Not the hostname: inside
a session that is the pod id and changes every session, so a profile filed under
it is orphaned the next time the session restarts. `$UNION_SITE` overrides, for a
site whose radios get swapped.

## Why this is separate from `settings/`

`settings/` holds records that describe **the session** — which devices this
account has reserved right now, which ports it published — and they churn: they
are rewritten at every session start and are meaningless once the pod is gone.

What is measured here is the opposite. A survey of a band takes minutes, costs a
radio, and stays true for as long as the antenna and the room do. Mixing the two
puts a months-lived measurement in the same folder as records that are expected
to be reaped, where it is one cleanup away from being deleted and one glance away
from being mistaken for something temporary.

## What is in a file (schema 3)

Every value appears **once**. What was measured once sits at the top; the
carriers you can choose between sit in `options`.

    radio           what was measured with: device, antenna, subdev, gain, band
    noise           the floor, its burstiness, and the receiver's own floor
                    beside it -- measured once, at the recommended carrier
    det_mult        derived from that noise. A RATIO above the floor, so it does
    sync_threshold  not change with gain or distance
    options[]       each one a complete parameter combination: a carrier, the
                    quiet band around it, its width, and whether the default
                    2 MS/s link fits. Only the recommended one is marked
                    `measured_here`; the rest inherit the thresholds
    use             which option is recommended (an index into `options`)

Read it with `python3 union/phy_profile.py --list`, or choose another option with
`--pick 2` (rank) or `--pick 5725` (carrier in MHz). `--max-options` on
`prepare.sh` changes how many are kept; anything dropped is named, never
silently truncated.

Schema 2 files still resolve — a measurement that cost a radio is not thrown away
over a layout change.

## Precedence

    explicit flag  >  topology file  >  this measurement  >  built-in default

The first two are someone stating an intent; this is only a measurement of where
the radio happens to be standing, so it never overrides a choice anyone made.
