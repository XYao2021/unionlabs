# How to add your algorithm

Follow these four steps to run your own algorithm over the SDR PHY. You write **one small
file** (`app.py`); the framework handles the radio.

---

## Step 1 — Where to put it

Create a folder under `algorithms/` whose **name is what you'll pass to `run.sh`**, and put an
`app.py` inside it:

```
Hardware_update/
└── algorithms/
    └── my_algo/            ←  the folder name = the algorithm name
        └── app.py          ←  REQUIRED. the framework looks for exactly this file
```

Then you run it with that same name:

```bash
./run.sh --algo my_algo
```

> Rule: `--algo <name>` must match the folder name, and the folder must contain `app.py`.
> `run.sh` lives at the repo root; run it from there. `./run.sh list` shows all algorithms found.

---

## Step 2 — What `app.py` must provide

`app.py` must define a function **`make(role)`** that returns an object with two methods:

```python
def make(role):            # role is "tx" or "rx" (the framework sets it)
    return YourObject(role)
```

The returned object exposes:

| Method / field | Required? | What it is |
|---|---|---|
| `transmit()` | **yes** | returns the numpy array to send. Return `None` when there's nothing left to send (ends the run). |
| `receive(msg)` | **yes** | called with the received numpy array — feed it into your algorithm. |
| `spec = (dtype, shape)` | optional | declares your output, e.g. `("float32", (16,))`. |
| `on_result(ack)` | optional | called with `True`/`False` = delivered/lost (a reinforcement-learning reward). |

That's the whole contract. No radio code, no imports from the framework.

---

## Step 3 — Two ways to write `app.py`

### 3A. Simplest — write the algorithm inline (copy `algorithms/_template/`)

```python
# algorithms/my_algo/app.py
import numpy as np

class MyAlgo:
    spec = ("float32", (8,))
    def __init__(self, role):
        self.role = role
    def transmit(self):
        return np.ones(8, np.float32)       # what to transmit  (None = done)
    def receive(self, msg):
        print("got", msg)                    # what to receive

def make(role):
    return MyAlgo(role)
```

### 3B. Link your OWN existing algorithm (copy `algorithms/plain_echo/`)

Leave your algorithm **untouched** in its own file next to `app.py`, and let `app.py` just map
its methods. Nothing in your algorithm needs to change or import our framework.

```
algorithms/my_algo/
├── my_model.py         ←  YOUR existing code (unchanged)
└── app.py              ←  the 10-line binding
```

```python
# my_model.py — YOUR algorithm. Knows nothing about radios.
import numpy as np
class MyModel:
    def __init__(self):
        self.buf = [np.array([i, i+1, i+2], np.float32) for i in range(5)]
        self.k = 0
    def next_output(self):                   # your method that produces data
        if self.k >= len(self.buf): return None
        out = self.buf[self.k]; self.k += 1; return out
    def take_input(self, x):                 # your method that consumes data
        print("   my_model received:", x)
```

```python
# app.py — the binding: map YOUR methods onto transmit()/receive()
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # import files in this folder
from my_model import MyModel

def make(role):
    model = MyModel()
    class Bind:
        spec = ("float32", (3,))
        def __init__(self): self.role = role
        def transmit(self):
            return model.next_output()       # <- your algorithm's output goes on the air
        def receive(self, msg):
            model.take_input(msg)            # <- the received array goes into your algorithm
    return Bind()
```

Point `transmit`/`receive` at whatever your algorithm's real method names are — that's the
"link". If your algorithm needs extra packages (numpy, torch, …), just `pip install` them; the
binding imports your code as-is.

---

## Step 4 — tx vs rx (only if your two ends differ)

Each node is built with a `role`: **`tx`** (transmitter, starts by sending) or **`rx`**
(receiver, starts by receiving and may reply). If your two ends do the *same* thing, ignore
`role`. If they differ, branch on it — e.g. the receiver classifies and replies:

```python
def transmit(self):
    if self.role == "rx":
        return np.array([self.last_answer], np.float32)   # rx's reply
    return self.next_request()                            # tx's request
def receive(self, msg):
    if self.role == "rx":
        self.last_answer = my_model.process(msg)          # rx handles the request
    else:
        my_model.apply(msg)                               # tx handles the reply
```

(See `algorithms/clip_semcom/` — the `tx` sends an image embedding, the `rx` classifies it and
replies the label.)

---

## Step 5 — Run it

```bash
# 1) radio-free, lossless — check your logic:
./run.sh --algo my_algo

# 2) radio-free THROUGH THE REAL MODEM + noise:
./run.sh --algo my_algo --channel pyphy --snr-db 6

# 3) over the radio (two hosts) — start the rx FIRST, then the tx:
./run.sh --algo my_algo --role rx --rx-args addr=192.168.20.2
./run.sh --algo my_algo --role tx --tx-args serial=30CD424 --ack-host <RX_IP>
```

Add `--steps N` to control how many rounds run. `./run.sh --help` lists every option.

---

## Checklist / common mistakes

- ☐ Folder is `algorithms/<name>/` and `--algo <name>` matches it exactly.
- ☐ `app.py` exists and defines `make(role)`.
- ☐ `transmit()` returns a **numpy array**, or `None` to stop.
- ☐ Build state inside `make()` (per node) — **not** as module-level globals, or the two
  loopback ends would share state.
- ☐ Importing your own file? add `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))`
  at the top of `app.py` (as in 3B).
- ☐ `spec` is optional — the wire format is self-describing, so shapes/dtypes are carried for you.

More detail: the full contract and the framework functions are in `algorithms/README.md`; the PHY options
are in `drivers/usrp_uhd/GUIDE.md`.
