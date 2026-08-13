#!/usr/bin/env python3
"""
make_stack_variants.py — backup redesigns of Figure 1 (docs/sdr_stack.png,
"From the algorithm to the air"). Renders three distinct directions to
docs/fig1_variant_{a,b,c}.{png,pdf}:

  A  refined layered stack     — the layer metaphor, done with craft
  B  horizontal signal-flow    — a left->right flowgraph with a return path
  C  TX/RX duality core        — mirrored paths around one shared PHY core

One cohesive visual system across all three: chosen cool neutrals, a single
teal accent, the contract emphasized as the "seam". No rainbow layers, no
full-height arrows, no corner-tag clutter.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch, Arc, PathPatch
from matplotlib.path import Path

plt.rcParams["font.family"] = "Helvetica"
plt.rcParams["font.sans-serif"] = ["Helvetica", "Arial", "DejaVu Sans"]

# ---- palette (chosen, not defaulted): cool ink + slate neutrals + one teal accent
INK      = "#17212e"   # near-black with a cool bias
MUTED    = "#6b7686"   # slate label text
FAINT    = "#9aa5b3"   # tags / tertiary
LINE     = "#e3e7ec"   # hairlines
PANEL    = "#f5f7f9"   # neutral panel (the shared PHY)
PANEL2   = "#eceff3"   # slightly deeper neutral
ACCENT   = "#0f766e"   # teal-700 — the one accent
ACCENT_D = "#0b5850"   # deeper teal
ACCENT_S = "#e4f2f0"   # teal wash (contract fill)
APP_S    = "#eef1f6"   # cool wash for the algorithm layer
WHITE    = "#ffffff"

LAYERS = [
    ("Algorithm layer",
     "OSU policy   ·   MARL agent   ·   FedAvg / SGD   ·   your next algorithm",
     "your code", "app"),
    ("Uniform interface & codec",
     "SdrApp: next_payload · on_payload · on_result      PayloadSpec(dtype, shape)      Codec.pack / unpack",
     "the contract", "contract"),
    ("PHY adapter — Python",
     "PhyLink.send / recv   ·   WarmSource / AccessPoint   ·   sdr.py",
     "python", "phy"),
    ("PHY engine — sdr_system, C++",
     "sync   ·   CFO + phase   ·   equalize   ·   (de)modulate   ·   FEC   ·   CRC-16   ·   stop-and-wait ARQ",
     "c++", "phy"),
    ("Radio",
     "USRP  N210 / B210 / X310        over the air   ( 915 MHz · 2.4 GHz )",
     "rf front-end", "radio"),
]

def new_fig(w=11.0, h=6.4):
    fig = plt.figure(figsize=(w, h), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100); ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax

def spaced(s):  # fake letter-tracking for small caps tags
    return " ".join(s.upper())

def title(ax, sub):
    ax.text(50, 95.2, "From the algorithm to the air",
            ha="center", va="center", fontsize=18.5, fontweight="bold", color=INK)
    ax.text(50, 89.6, sub, ha="center", va="center", fontsize=10.6, color=MUTED)

def save(fig, stem):
    fig.savefig(f"docs/{stem}.png", dpi=200, facecolor="white")
    fig.savefig(f"docs/{stem}.pdf", facecolor="white")
    plt.close(fig)
    print("wrote", stem)

# ============================================================ A: layered stack
def variant_a():
    fig, ax = new_fig()
    title(ax, "one shared PHY — every algorithm plugs in through the same contract")
    x0, x1 = 18.5, 81.5
    ytop, ybot = 84.5, 11.5
    n = len(LAYERS); h = (ytop - ybot) / n

    # outer container
    ax.add_patch(FancyBboxPatch((x0, ybot), x1 - x0, ytop - ybot,
                 boxstyle="round,pad=0,rounding_size=1.4", fc=WHITE, ec=LINE, lw=1.4, zorder=1))

    for i, (name, sub, tag, role) in enumerate(LAYERS):
        yt = ytop - i * h; yb = yt - h; yc = (yt + yb) / 2
        if i > 0:
            ax.plot([x0, x1], [yt, yt], color=LINE, lw=1.0, zorder=2)
        if role == "app":
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=APP_S, ec="none", zorder=1.4))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=INK, ec="none", zorder=3, alpha=.55))
            nmc = INK
        elif role == "contract":
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=ACCENT_S, ec="none", zorder=1.4))
            ax.add_patch(FancyBboxPatch((x0 + 0.5, yb + 0.7), x1 - x0 - 1.0, h - 1.4,
                         boxstyle="round,pad=0,rounding_size=1.0", fc="none",
                         ec=ACCENT, lw=1.5, ls=(0, (4, 3)), zorder=3))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=ACCENT, ec="none", zorder=3))
            nmc = ACCENT_D
        else:
            ax.add_patch(Rectangle((x0, yb), x1 - x0, h, fc=PANEL, ec="none", zorder=1.4))
            ax.add_patch(Rectangle((x0, yb), 1.0, h, fc=FAINT, ec="none", zorder=3, alpha=.7))
            nmc = INK
        ax.text(x0 + 3.4, yc + h * 0.17, name, ha="left", va="center",
                fontsize=12.8, fontweight="bold", color=nmc, zorder=4)
        ax.text(x0 + 3.4, yc - h * 0.25, sub, ha="left", va="center",
                fontsize=8.9, color=MUTED, zorder=4)
        ax.text(x1 - 2.4, yt - 2.3, spaced(tag), ha="right", va="top",
                fontsize=6.8, color=FAINT, fontweight="bold", zorder=4)

    # thin payload (down) + result (up) rails, muted — not the fat arrows of the old fig
    lx, rx = x0 - 4.2, x1 + 4.2
    ax.add_patch(FancyArrowPatch((lx, ytop), (lx, ybot), arrowstyle="-|>", mutation_scale=12,
                 lw=1.4, color=MUTED, zorder=5))
    ax.text(lx - 1.6, (ytop + ybot) / 2, "payload   float32[ ] / bytes", rotation=90,
            ha="center", va="center", fontsize=8.6, color=MUTED)
    ax.add_patch(FancyArrowPatch((rx, ybot), (rx, ytop), arrowstyle="-|>", mutation_scale=12,
                 lw=1.4, color=ACCENT, zorder=5))
    ax.text(rx + 1.6, (ytop + ybot) / 2, "result   ACK bool / float32[ ]", rotation=90,
            ha="center", va="center", fontsize=8.6, color=ACCENT_D)

    save(fig, "fig1_variant_a")

# ============================================================ B: signal-flow
def variant_b():
    fig, ax = new_fig()
    title(ax, "the payload flows down to the air; the result flows back up")
    stages = [("Algorithm", "your code", "app"),
              ("Contract",  "SdrApp · Codec", "contract"),
              ("Adapter",   "PhyLink · sdr.py", "phy"),
              ("Engine",    "sdr_system", "phy"),
              ("Radio",     "USRP", "radio")]
    n = len(stages)
    x0, x1 = 6, 78
    bw, gap = (x1 - x0 - (n - 1) * 3.2) / n, 3.2
    yc = 63; bh = 15
    centers = []
    for i, (nm, sub, role) in enumerate(stages):
        bx = x0 + i * (bw + gap)
        fc = {"app": APP_S, "contract": ACCENT_S, "phy": PANEL, "radio": PANEL}[role]
        ec = ACCENT if role == "contract" else LINE
        lw = 1.6 if role == "contract" else 1.3
        ls = (0, (4, 3)) if role == "contract" else "solid"
        ax.add_patch(FancyBboxPatch((bx, yc - bh / 2), bw, bh,
                     boxstyle="round,pad=0,rounding_size=1.1", fc=fc, ec=ec, lw=lw, ls=ls, zorder=2))
        nmc = ACCENT_D if role == "contract" else INK
        ax.text(bx + bw / 2, yc + 2.2, nm, ha="center", va="center",
                fontsize=12.2, fontweight="bold", color=nmc, zorder=3)
        ax.text(bx + bw / 2, yc - 3.0, sub, ha="center", va="center",
                fontsize=8.2, color=MUTED, zorder=3)
        centers.append((bx, bx + bw))
        if i > 0:
            px = centers[i - 1][1]; nx = bx
            ax.add_patch(FancyArrowPatch((px + 0.4, yc), (nx - 0.4, yc), arrowstyle="-|>",
                         mutation_scale=13, lw=1.6, color=FAINT, zorder=1))
    # air broadcast glyph after Radio
    ax_ = centers[-1][1] + 5.5
    ax.add_patch(FancyArrowPatch((centers[-1][1] + 0.4, yc), (ax_ - 2.4, yc),
                 arrowstyle="-|>", mutation_scale=13, lw=1.6, color=FAINT, zorder=1))
    for r in (2.6, 4.8, 7.0):
        ax.add_patch(Arc((ax_ - 1.5, yc), r, r, angle=0, theta1=-52, theta2=52,
                     color=ACCENT, lw=1.7, zorder=3))
    ax.text(ax_ + 3.4, yc, "over\nthe air", ha="center", va="center",
            fontsize=9.2, color=ACCENT_D, fontweight="bold")

    # forward + return labels/rails
    ax.text(x0 + 1, yc + bh / 2 + 4.2, "payload   float32[ ] / bytes", ha="left",
            va="center", fontsize=9.4, color=MUTED)
    ry = 40
    ax.add_patch(FancyArrowPatch((ax_ - 1.5, yc - bh / 2 - 1.2), (ax_ - 1.5, ry),
                 arrowstyle="-", lw=1.5, color=ACCENT, zorder=1))
    ax.add_patch(FancyArrowPatch((ax_ - 1.5, ry), (x0 + bw / 2, ry),
                 arrowstyle="-", lw=1.5, color=ACCENT, zorder=1))
    ax.add_patch(FancyArrowPatch((x0 + bw / 2, ry), (x0 + bw / 2, yc - bh / 2 - 1.2),
                 arrowstyle="-|>", mutation_scale=13, lw=1.5, color=ACCENT, zorder=1))
    ax.text((x0 + ax_) / 2, ry - 3.2, "result   ·   ACK bool  /  RX float32[ ]", ha="center",
            va="center", fontsize=9.4, color=ACCENT_D)
    save(fig, "fig1_variant_b")

# ============================================================ C: TX/RX duality
def variant_c():
    fig, ax = new_fig()
    title(ax, "one payload down, one result up — around a shared PHY core")
    # central core
    cw, ch = 26, 30; cx = 50 - cw / 2; cy = 30
    ax.add_patch(FancyBboxPatch((cx, cy), cw, ch, boxstyle="round,pad=0,rounding_size=1.6",
                 fc=PANEL, ec=LINE, lw=1.5, zorder=2))
    ax.text(50, cy + ch - 4.5, "SHARED PHY", ha="center", va="center",
            fontsize=12.8, fontweight="bold", color=INK, zorder=3)
    for j, (nm, sub) in enumerate([("PHY engine", "sdr_system · C++"),
                                    ("Radio", "USRP · RF")]):
        yy = cy + ch - 12 - j * 8.5
        ax.add_patch(FancyBboxPatch((cx + 3, yy - 3), cw - 6, 6.2,
                     boxstyle="round,pad=0,rounding_size=0.9", fc=WHITE, ec=LINE, lw=1.1, zorder=3))
        ax.text(50, yy + 0.9, nm, ha="center", va="center", fontsize=9.8,
                fontweight="bold", color=INK, zorder=4)
        ax.text(50, yy - 1.7, sub, ha="center", va="center", fontsize=7.6, color=MUTED, zorder=4)

    # left = apps + contract stacked, feeding DOWN into core
    lx = 8
    ax.add_patch(FancyBboxPatch((lx, 78), 22, 9, boxstyle="round,pad=0,rounding_size=1.1",
                 fc=APP_S, ec=LINE, lw=1.3, zorder=2))
    ax.text(lx + 11, 78 + 6.2, "Algorithm", ha="center", va="center", fontsize=11.2,
            fontweight="bold", color=INK, zorder=3)
    ax.text(lx + 11, 78 + 2.6, "OSU · MARL · FL", ha="center", va="center",
            fontsize=7.8, color=MUTED, zorder=3)
    # contract chip on left
    ax.add_patch(FancyBboxPatch((lx, 62), 22, 8, boxstyle="round,pad=0,rounding_size=1.1",
                 fc=ACCENT_S, ec=ACCENT, lw=1.6, ls=(0, (4, 3)), zorder=2))
    ax.text(lx + 11, 66, "Contract", ha="center", va="center", fontsize=10.6,
            fontweight="bold", color=ACCENT_D, zorder=3)
    # down arrow into core
    ax.add_patch(FancyArrowPatch((lx + 11, 61.5), (lx + 11, 47), arrowstyle="-", lw=1.6,
                 color=MUTED, zorder=1))
    ax.add_patch(FancyArrowPatch((lx + 11, 47), (cx - 0.6, cy + ch * 0.6), arrowstyle="-|>",
                 mutation_scale=13, lw=1.6, color=MUTED, zorder=1))
    ax.text(lx + 8.7, 54, "payload", ha="right", va="center", fontsize=8.8, color=MUTED)

    # right = contract + apps, fed UP from core
    rx = 70
    ax.add_patch(FancyBboxPatch((rx, 78), 22, 9, boxstyle="round,pad=0,rounding_size=1.1",
                 fc=APP_S, ec=LINE, lw=1.3, zorder=2))
    ax.text(rx + 11, 78 + 6.2, "Algorithm", ha="center", va="center", fontsize=11.2,
            fontweight="bold", color=INK, zorder=3)
    ax.text(rx + 11, 78 + 2.6, "reward / RX data", ha="center", va="center",
            fontsize=7.8, color=MUTED, zorder=3)
    ax.add_patch(FancyBboxPatch((rx, 62), 22, 8, boxstyle="round,pad=0,rounding_size=1.1",
                 fc=ACCENT_S, ec=ACCENT, lw=1.6, ls=(0, (4, 3)), zorder=2))
    ax.text(rx + 11, 66, "Contract", ha="center", va="center", fontsize=10.6,
            fontweight="bold", color=ACCENT_D, zorder=3)
    ax.add_patch(FancyArrowPatch((cx + cw + 0.6, cy + ch * 0.6), (rx + 11, 47),
                 arrowstyle="-", lw=1.6, color=ACCENT, zorder=1))
    ax.add_patch(FancyArrowPatch((rx + 11, 47), (rx + 11, 61.5), arrowstyle="-|>",
                 mutation_scale=13, lw=1.6, color=ACCENT, zorder=1))
    ax.text(rx + 13.3, 54, "result", ha="left", va="center", fontsize=8.8, color=ACCENT_D)

    # air glyph below the core
    for r in (3.0, 5.5, 8.0):
        ax.add_patch(Arc((50, cy - 1.0), r, r, angle=0, theta1=200, theta2=340,
                     color=ACCENT, lw=1.7, zorder=3))
    ax.text(50, cy - 8.5, "over the air", ha="center", va="center",
            fontsize=9.2, color=ACCENT_D, fontweight="bold")
    save(fig, "fig1_variant_c")

if __name__ == "__main__":
    variant_a(); variant_b(); variant_c()
