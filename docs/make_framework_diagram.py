#!/usr/bin/env python3
"""
make_framework_diagram.py — one-page diagram of the uniform algorithm->PHY framework,
with MARL as the running example. Renders docs/framework_marl.{png,pdf}.
Same visual system as docs/make_stack_variants.py (cool neutrals + one teal accent).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch

plt.rcParams["font.family"] = "Helvetica"
plt.rcParams["font.sans-serif"] = ["Helvetica", "Arial", "DejaVu Sans"]

INK, MUTED, FAINT, LINE = "#17212e", "#6b7686", "#9aa5b3", "#e3e7ec"
PANEL, ACCENT, ACCENT_D, ACCENT_S, APP_S, WHITE = "#f5f7f9", "#0f766e", "#0b5850", "#e4f2f0", "#eef1f6", "#ffffff"

# (name, path, detail, tag, role)
BANDS = [
    ("Your algorithm", "algorithms/marl/app.py",
     "class MARL:   transmit()   ·   receive(msg)   ·   on_result(ack)          +   make(role)",
     "no framework import", "app"),
    ("Uniform framework", "drivers/usrp_uhd/python/   ·   phy_link.py + run_algo.py",
     "adapt()  ·  Codec: array<->bytes  ·  channels: ideal | pyphy | radio  ·  run_loopback / RadioRoundTrip",
     "the seam", "seam"),
    ("PHY engine", "phy/",
     "pyphy:   modulate · FEC · AWGN                    sdr_system:   source_arq (TX)  /  sink_arq (RX)",
     "c++ · dsp", "phy"),
    ("Radio", "drivers/usrp_uhd/build/sdr_system  ->  USRP",
     "B210 (agent, TX)      over the air · 915 MHz · DQPSK      N210 (AP, RX)",
     "rf", "radio"),
]


def spaced(s):
    return " ".join(s.upper())


def main():
    fig = plt.figure(figsize=(11.5, 7.4), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

    ax.text(50, 95.5, "From algorithm to PHY — the uniform framework", ha="center", va="center",
            fontsize=18.5, fontweight="bold", color=INK)
    ax.text(50, 90.3, "MARL as the example — an algorithm only says what to transmit / what to receive",
            ha="center", va="center", fontsize=10.6, color=MUTED)

    x0, x1 = 14, 86
    ytop, ybot = 85, 30
    n = len(BANDS); h = (ytop - ybot) / n
    ax.add_patch(FancyBboxPatch((x0, ybot), x1 - x0, ytop - ybot,
                 boxstyle="round,pad=0,rounding_size=1.3", fc=WHITE, ec=LINE, lw=1.4, zorder=1))

    for i, (name, path, detail, tag, role) in enumerate(BANDS):
        yt = ytop - i * h; yb = yt - h; yc = (yt + yb) / 2
        if i > 0:
            ax.plot([x0, x1], [yt, yt], color=LINE, lw=1.0, zorder=2)
        if role == "app":
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=APP_S, ec="none", zorder=1.4))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=INK, ec="none", alpha=.55, zorder=3)); nmc = INK
        elif role == "seam":
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=ACCENT_S, ec="none", zorder=1.4))
            ax.add_patch(FancyBboxPatch((x0 + 0.5, yb + 0.6), x1 - x0 - 1.0, h - 1.2,
                         boxstyle="round,pad=0,rounding_size=1.0", fc="none", ec=ACCENT, lw=1.5,
                         ls=(0, (4, 3)), zorder=3))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=ACCENT, ec="none", zorder=3)); nmc = ACCENT_D
        else:
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=PANEL, ec="none", zorder=1.4))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=FAINT, ec="none", alpha=.7, zorder=3)); nmc = INK
        ax.text(x0 + 3.0, yc + h * 0.22, name, ha="left", va="center", fontsize=12.6,
                fontweight="bold", color=nmc, zorder=4)
        ax.text(x0 + 3.0, yc + h * 0.005, path, ha="left", va="center", fontsize=8.4,
                style="italic", color=MUTED, zorder=4)
        ax.text(x0 + 3.0, yc - h * 0.28, detail, ha="left", va="center", fontsize=7.5,
                color=MUTED, zorder=4)  # detail
        ax.text(x1 - 2.2, yt - 2.2, spaced(tag), ha="right", va="top", fontsize=6.6,
                color=FAINT, fontweight="bold", zorder=4)

    # side rails: transmit (payload) DOWN, on_result (reward) UP
    lx, rx = x0 - 4.0, x1 + 4.0
    ax.add_patch(FancyArrowPatch((lx, ytop), (lx, ybot), arrowstyle="-|>", mutation_scale=13,
                 lw=1.5, color=MUTED, zorder=5))
    ax.text(lx - 1.6, (ytop + ybot) / 2, "transmit   burst[8] float32", rotation=90, ha="center",
            va="center", fontsize=8.6, color=MUTED)
    ax.add_patch(FancyArrowPatch((rx, ybot), (rx, ytop), arrowstyle="-|>", mutation_scale=13,
                 lw=1.5, color=ACCENT, zorder=5))
    ax.text(rx + 1.6, (ytop + ybot) / 2, "on_result   ACK = reward", rotation=90, ha="center",
            va="center", fontsize=8.6, color=ACCENT_D)

    # bottom ribbon: one MARL slot traced through the stack (DejaVu for the arrows)
    ax.add_patch(FancyBboxPatch((10, 9), 80, 12.5, boxstyle="round,pad=0,rounding_size=1.2",
                 fc=PANEL, ec=LINE, lw=1.2, zorder=1))
    ax.text(50, 17.2, "one MARL slot", ha="center", va="center", fontsize=9.5,
            fontweight="bold", color=ACCENT_D)
    ax.text(50, 12.6,
            "agent.transmit(burst) → Codec.pack → [ modulate · FEC · +AWGN · demod ] "
            "→ Codec.unpack → AP.receive → ACK(crc_ok) → agent.on_result(reward)",
            ha="center", va="center", fontsize=8.2, color=INK, family="DejaVu Sans")

    fig.savefig("docs/framework_marl.png", dpi=200, facecolor="white")
    fig.savefig("docs/framework_marl.pdf", facecolor="white")
    plt.close(fig)
    print("wrote docs/framework_marl.png and docs/framework_marl.pdf")


if __name__ == "__main__":
    main()
