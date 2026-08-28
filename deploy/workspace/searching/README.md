# `searching/` — what a testbed measured about its own RF environment

One file per radio, written by `./prepare.sh` (which runs
`drivers/usrp/python/prepare_phy.py`) and read back automatically by `run.sh` and
`radio.sh`:

    phy-<key>.json      the usable bands, the carrier, the noise floor,
                        and the detector thresholds measured there

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

## What is in a file

    recommended     the region prepare_phy suggests: carrier, usable band,
                    det_mult, sync_threshold
    candidates      EVERY quiet region it found, ranked, so a topology can point
                    a node at another one without re-measuring the site. Each says
                    whether its numbers were `measured` there or inherited from
                    the recommendation.

Read it with `python3 union/phy_profile.py --list`, or pick a different candidate
with `--pick 2` / `--pick 5190`.

## Precedence

    explicit flag  >  topology file  >  this measurement  >  built-in default

The first two are someone stating an intent; this is only a measurement of where
the radio happens to be standing, so it never overrides a choice anyone made.
