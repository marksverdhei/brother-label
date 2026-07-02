# RFC-002 — Wire `label-sheet` into the `label` CLI (batch tape-sparing)

**Status:** Proposed — awaiting Markus's go-ahead + one hardware calibration pass
**Author:** brother-label clerk (Claude)
**Branch:** `feat/label-sheet-cli` (draft PR)

`bin/label-sheet` packs N pictograms onto one tape strip, prints it as a
**single job with a single cut**, and you scissor the tiles apart along printed
guides. It works and is unit-tested (`test/test_sheet.py`), but it's a standalone
POC — the README literally says *"Not wired into `label` yet."* This RFC proposes
finishing that integration, with the decisions I can't make alone laid out with
recommendations so they resolve in one pass.

---

## Why now (the motivation is a real pain, not polish)

The **eject jam is intrinsic**: every ~2nd–3rd *consecutive auto-cut job* jams,
regardless of label length, inter-job pause, or tray clearing (RFC-001 D6, 5 jams
across 3 batches). Today that caps us at **≤2 unattended jobs** — printing a set
of, say, 9 drawer/gem stickers means babysitting the printer through jam-clears.

`label-sheet` sidesteps the failure mode directly: **N stickers → 1 feed → 1
cut**, so a 9-sticker sheet is *one* cut instead of nine. It is the software-side
mitigation that complements the firmware fix (D6) — and unlike the firmware, I
can ship it without waiting on Brother. That makes wiring it in genuinely wanted,
not speculative.

Trade-off it accepts: tiles are **hand-cut** along printed guides, so edges
aren't machine-clean. Best for wireframe/line-art glyphs where a hand-cut border
doesn't matter (the gem hexes, connector icons, drawer pictograms) — not for
labels whose value is a crisp die-cut edge.

---

## D1 — Do we wire it in at all? — **recommend: yes**

Add a `label sheet …` subcommand. It's the only remaining unblocked item in the
epoch backlog, it addresses a live pain, and the packing code is already written
and tested. **Needs from you:** a yes/no.

## D2 — Implementation shape — **recommend: thin delegation (Option A)**

- **Option A — thin wrapper (recommended).** `label sheet` builds an argv and
  `subprocess.run`s `bin/label-sheet` (via `sys.executable`). `label-sheet`
  keeps owning packing, preview, and the print. ~15 lines in `bin/label`, one
  hardware-free test asserting the argv mapping. Lowest risk; nothing about the
  proven POC changes.
- **Option B — import as a module.** `label` imports `label-sheet` and calls
  `compose()`/`send()` inline, unifying the print path through
  `brother_print.send`. Cleaner long-term, but re-plumbs a working POC and its
  `--print` path for little immediate gain.

Recommend **A now**, with B as a later refactor once the feature has earned its
keep. The draft PR on this branch implements A.

Proposed surface (mirrors `label-sheet`'s own flags):

```
label sheet IMG1 IMG2 …            # pack given images, preview only
label sheet --demo                 # 6 built-in wireframe glyphs, preview
label sheet … --tile 0.45          # tile size in inches (default 0.45")
label sheet … --no-ruler           # drop the calibration ruler edge
label sheet … --print              # actually print (one job, one cut)
```

## D3 — Calibration before we trust it — **recommend: keep the ruler ON for the first real print**

`label-sheet`'s real-size math assumes `autofit` scales the strip to exactly the
1.00" tape. That **has never been verified on hardware** (the printer's been
offline since this work started). The tool already prints a ¼" ruler edge for
exactly this: print one `--demo` sheet, measure the ruler against a real ruler,
and correct `PRINT_DPI`/scale in `bin/label-sheet` if autofit's effective scale
differs. Until that pass is done, treat printed sheet dimensions as approximate.
**Needs from you:** the printer back online for one calibration sheet.

## D4 — Default tile size — **recommend: 0.45"**

0.45" fits **2 columns** on the 1" tape (`(320 − 2·8 − 14) / round(0.45·320) =
290/144 = 2`), a sensible density for glyph stickers. Kept as the default;
override with `--tile`. No decision needed unless you want a different default.

---

## What this RFC does *not* change

- The standalone `bin/label-sheet` keeps working exactly as-is.
- No change to the native print path, the drawer/icon/tag flows, or auto-cut.
- The draft PR is **draft on purpose**: it's the concrete proposal for D1/D2, not
  something I'll self-merge. Merge is yours once D1 is a yes and D3's calibration
  sheet has been eyeballed.

## Cross-refs

- Eject jam / firmware: `docs/RFC-001-…` D6, `README.md` "Reliability notes".
- POC + tests: `bin/label-sheet`, `test/test_sheet.py`.
