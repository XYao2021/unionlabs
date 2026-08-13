#!/usr/bin/env python3
"""
run_algo.py — load an uploaded algorithm from algorithms/<name>/app.py and run it
over the PHY through the uniform phy_link contract.

  # radio-free round-trip (lossless):
  python3 union/run_algo.py --algo echo --role loopback

  # radio-free through the REAL modem + AWGN:
  PYTHONPATH=drivers/usrp_uhd/bindings arch -x86_64 python3 union/run_algo.py \
      --algo echo --role loopback --channel pyphy --snr-db 6

  # over the radio (two hosts): rx first, then tx
  python3 union/run_algo.py --algo echo --role rx --rx-args addr=192.168.20.2
  python3 union/run_algo.py --algo echo --role tx --tx-args serial=30CD424 \
      --ack-host <RX_IP> --net-host <RX_IP>
"""
import argparse, importlib.util, inspect, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                            # union/ -> repo
sys.path.insert(0, HERE)
import phy_link as pl


def _has_io(c):
    return any(hasattr(c, m) for m in ("transmit", "produce"))


def load_app_factory(name):
    """Return (factory(role) -> SdrApp, how). The algorithm only has to declare what
    to transmit and what to receive — provided as a make(role) binding, a plain class
    with transmit()/receive(msg), an SdrApp subclass, or module-level functions."""
    path = os.path.join(REPO, "algorithms", name, "app.py")
    if not os.path.exists(path):
        sys.exit(f"no algorithm at {path}\n"
                 f"create algorithms/{name}/app.py (copy algorithms/_template/app.py)")
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location(f"algo_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1) a make(role) binding -> read ANY (untouched) algorithm
    if callable(getattr(mod, "make", None)):
        return (lambda role: pl.adapt(mod.make(role), role)), "make(role) binding", mod
    own = [c for _, c in inspect.getmembers(mod, inspect.isclass) if c.__module__ == mod.__name__]
    # 2) an SdrApp subclass (advanced/stateful)
    subs = [c for c in own if issubclass(c, pl.SdrApp) and c is not pl.SdrApp]
    if subs:
        return (lambda role: subs[0](role)), f"SdrApp subclass {subs[0].__name__}", mod
    # 3) a plain class exposing transmit()/receive()
    plains = [c for c in own if _has_io(c)]
    if plains:
        cls = plains[0]
        def f(role):
            try:
                obj = cls(role)
            except TypeError:
                obj = cls()
            return pl.adapt(obj, role)
        return f, f"plain class {cls.__name__}", mod
    # 4) module-level transmit()/receive() (single instance)
    if _has_io(mod):
        return (lambda role: pl.adapt(mod, role)), "module-level transmit/receive", mod
    sys.exit(f"{path} exposes no algorithm interface "
             f"(need make(role), a class/SdrApp with transmit()/receive(), or module functions)")


def main():
    ap = argparse.ArgumentParser(description="run an uploaded algorithm over the PHY")
    ap.add_argument("--algo", required=True, help="folder name under algorithms/")
    ap.add_argument("--role", default="loopback",
                    choices=["loopback", "tx", "rx", "multi", "aircomp"])
    ap.add_argument("--channel", default="ideal", choices=["ideal", "pyphy"],
                    help="loopback/multi channel: ideal (lossless) or pyphy (real modem + AWGN)")
    ap.add_argument("--steps", type=int, default=5)
    ap.add_argument("--agents", type=int, default=4, help="number of agents (--role multi)")
    # pyphy channel knobs
    ap.add_argument("--scheme", default="QPSK")
    ap.add_argument("--fec", default="turbo", choices=["", "conv", "ldpc", "turbo"])
    ap.add_argument("--snr-db", type=float, default=8.0)
    # radio knobs
    ap.add_argument("--tx-args", default="")
    ap.add_argument("--rx-args", default="")
    ap.add_argument("--ack-host", default="127.0.0.1")
    ap.add_argument("--net-host", default="127.0.0.1")
    ap.add_argument("--net-port", type=int, default=5700)
    a = ap.parse_args()

    factory, how, mod = load_app_factory(a.algo)
    print(f"[run_algo] loaded algorithms/{a.algo} via {how}")

    if a.role == "loopback":
        ch = (pl.make_channel("pyphy", scheme=a.scheme, fec=(a.fec or None), snr_db=a.snr_db)
              if a.channel == "pyphy" else pl.make_channel("ideal"))
        ini, res = factory("tx"), factory("rx")
        st = pl.run_loopback(ini, res, ch, steps=a.steps)
        print(f"[run_algo] loopback done: {st['delivered']}/{st['steps']} round-trips "
              f"delivered over channel={ch.name}")
    elif a.role == "multi":
        ch = (pl.make_channel("pyphy", scheme=a.scheme, fec=(a.fec or None), snr_db=a.snr_db)
              if a.channel == "pyphy" else pl.make_channel("ideal"))
        agents = [factory("agent") for _ in range(a.agents)]      # N independent agents
        st = pl.run_slotted(agents, ch, slots=a.steps)
        ptx = [getattr(g._src, "p_transmit", lambda: float("nan"))() for g in agents]
        n = a.agents
        opt = (1 - 1.0 / n) ** (n - 1)                        # slotted-ALOHA optimal throughput
        print(f"[run_algo] multi done ({n} agents, channel={ch.name}, {st['slots']} slots): "
              f"throughput={st['delivered']/max(1,st['slots']):.2f}/slot "
              f"(slotted-ALOHA optimum = {opt:.2f})  "
              f"collision-rate={st['collisions']/max(1,st['slots']):.2f}  "
              f"per-agent P(transmit)=[{', '.join(f'{p:.2f}' for p in ptx)}]")
    elif a.role == "aircomp":
        # COMPUTE archetype: N sensors superpose -> AP recovers Σ v_i (the app owns the driver)
        if not callable(getattr(mod, "run", None)) or not callable(getattr(mod, "make", None)):
            sys.exit(f"algorithms/{a.algo} needs make(role) + run(sensors, ...) for --role aircomp")
        sensors = [mod.make("sensor") for _ in range(a.agents)]
        mod.run(sensors, snr_db=a.snr_db, steps=a.steps)
    else:
        link = pl.RadioRoundTrip(role=a.role, tx_args=a.tx_args, rx_args=a.rx_args,
                                 ack_host=a.ack_host, net_host=a.net_host, net_port=a.net_port,
                                 scheme=a.scheme)
        app = factory(a.role)
        n = 0
        while link.step(app):
            n += 1
            if a.role == "tx" and n >= a.steps:
                break
        print(f"[run_algo] radio {a.role} done: {n} steps")


if __name__ == "__main__":
    main()
