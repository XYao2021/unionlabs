# `paper/` — the UnionLabs demo submission

```bash
make            # -> mobicom-demo.pdf   (latexmk: pdflatex + bibtex + pdflatex x2)
make clean
```

Built with TeX Live 2022. Needs `IEEEtran.cls` and `IEEEtran.bst` (both present
here; elsewhere: `tlmgr install ieeetran`).

| File | What it is |
|---|---|
| `mobicom-demo.tex` | the paper — IEEEtran conference, 2-page body, references on page 3 |
| `refs.bib` | 9 cited entries + 2 spares for the reserved section |
| `CCNC25RT.pdf` | a reference demo paper, used for its formatting |
| `Makefile` | build / clean |
| `figs/` | standalone system figures (TikZ), each in two layouts: the wide `fig-*.pdf` for slides/poster, and the `fig-*-col.pdf` column variants the paper uses — `make` in that folder builds all of them |
| `mobicom-demo-acmart.tex.bak` | the earlier ACM `acmart` draft — delete when you no longer want it |

## Format: ACM MobiCom demo

Per the [MobiCom call for demos](https://www.sigmobile.org/mobicom/2025/demos.html):
**2 pages double-column, fonts no smaller than 10pt, ACM template, plus one
extra page for references** — which is why `\clearpage` precedes the
bibliography. Demo submissions are **single-blind**, so authors are named; do
not anonymise.

The class line is `\documentclass[sigconf,10pt]{acmart}` — the CFP names that
size explicitly, and it is denser than acmart's 9pt default, so the page fills
faster than you would expect.

The *structure* follows the reference paper `CCNC25RT.pdf`: motivation
→ design with bold run-in components → named demonstration scenarios →
references. No results section and no conclusion; two pages have no room, and a
demo is not judged on either. That reference is IEEEtran because CCNC is
an IEEE conference — the *template* follows the venue, the *structure* is what
transfers. `mobicom-demo-ieeetran.tex.bak` is that version if you ever need an
IEEE venue.

## Current state: everything in, one-column figures

All three system figures are in the paper as their **column variants**
(`fig-*-col`), each placed by the paragraph that references it: Fig. 1 union
API and Fig. 2 PHY pipeline top the two columns of the design page, Fig. 3
shared workspace sits beside the deployment text. The features section holds
Zero-Touch Edge Onboarding. Total: **3 pages** — still over MobiCom's
2-pages-plus-references shape, deliberately (assemble first, trim later).

When trimming to the submission shape:

1. Restore `\balance` + `\clearpage` before the bibliography (marked comment),
   and remove the `{\small ...}` wrapper around it.
2. Pick which figure survives — a 2-page body realistically keeps ONE.
   Rough costs: Fig. 1 ≈ ⅓ column-page, Fig. 2 ≈ ½, Fig. 3 ≈ ½.
   The wide variants stay in `figs/` for the poster and slides either way.
3. `make`, then check: 3 pages, references alone on the last.

## Before submitting

1. **Fill every `\TODO{...}`** — they render in red, so a missed one is visible
   in the PDF. Then delete the `\newcommand{\TODO}` line so the build fails
   loudly if one survives.
2. **Authors, affiliations, and the funding footnote.** The author block follows
   the reference's shape: superscript-numbered names, affiliations on one line, a
   single `Email: {...}@...` line.
3. **Verify every reference** against its DOI. `refs.bib` was written from
   memory of the literature; entries preceded by `%% CHECK this entry` are the
   ones whose author list, venue or year is most likely wrong. McMahan, Lian,
   and Nazer & Gastpar are the ones I am confident in. The AERPAW and GNU Radio
   entries are URLs and need an access date.
4. **Update the two counts** in Section II ("44 command-line flags and 82
   topology settings") from `./run.sh selftest` on submission day.
5. **Venue.** Nothing in the draft names a specific conference; add the venue's
   own template requirements (copyright block, page limits) if it has any.

## What the paper claims, and where it comes from

| Claim | Where in the repo |
|---|---|
| algorithm declares only produce/consume | `union/phy_link.py`, `experiments/*/app.py` |
| one modem, four schemes, three FEC families | `drivers/usrp/` |
| wiring as a file, per-link media, validation | `union/topology.py`, `deploy/workspace/topologies/` |
| mixed-media chain (air in, TCP out) | `union/phy_link.py: ChainRelay` |
| bind vs advertise (published ports) | `union/topology.py`, `deploy/testbed/expose-my-port.sh` |
| everything is checked | `union/selftest.py`, `test_flags.py`, `test_topology.py` |

The draft claims **no over-the-air measurement**, because none has been taken.
If you want a plot in Section III, that run has to happen first.
