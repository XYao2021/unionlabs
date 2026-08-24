# env/ — the account's own libraries, installed once

The containers are deleted after every experiment, so anything installed *in*
one dies with it. This folder is the fix: it lives on the shared workspace,
which survives sessions and is visible from every testbed of the account.

| File | What it is |
|---|---|
| `requirements.txt` | the libraries the account wants — **edit this** |
| `packages.txt` | apt-level packages (reserved: consumed by the agent's image bake) |
| `venv/` | the persistent environment itself — generated, never edit |
| `.applied` | hash of the last-applied requirements — generated |

## How it behaves

Session start activates `venv/` (a PATH entry — instant) and compares
`requirements.txt` against the hash in `.applied`:

- **unchanged** → nothing happens. No reading, no resolving, no network.
- **changed** → `pip install -r` runs; libraries already present are skipped,
  so the cost is exactly the lines just added — once, for every future
  session of the account.

`pip install X` by hand inside a session also works (it lands in the same
venv) — but a line in `requirements.txt` is the version that is recorded,
reproducible, and ready to be baked into an image later.
