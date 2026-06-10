# RFC-001 — Landing the native driver & open decisions

**Status:** Blocked on Markus (hardware + a few judgement calls)
**Author:** brother-label clerk (Claude)
**Branch:** `feat/native-driver` (11 commits; the repo's only branch — no `main` yet, no remote)

This RFC consolidates the decisions I can't make alone, each with a recommendation,
so they're resolvable in one pass. Everything software-side that I *can* do without
the hardware is already done and verified (native driver, 30 hardware-free tests,
hardened image pipeline, docs, deployed reliability timers).

---

## D1 — Hardware verification — ✅ RESOLVED 2026-06-10

**Outcome.** Verified end-to-end on hardware: the native driver prints,
**auto-cuts**, and returns to `IDLE/SUCCESS`. Markus confirmed the physical
label and the cut. Orientation is correct without rotation (`BROTHER_FLIP`
stays off).

Getting there required two protocol corrections (now in `docs/protocol.md` and
the driver): print jobs are **lockless** (taking the lock without embedding the
job_token in the `<print>` header makes the printer reject your own job as
"busy" — the cause of the long busy saga), and **the auto-cut is triggered by
closing the data socket** after the printer acks the image. A capture of the
working zsocket path (`cache/capture/zsocket-print-header-20260610.txt`) plus
the Sunburn-Schematics protocol captures were the ground truth. IPP (port 631)
was also exercised live: it prints but exposes no cut control.

**Residual check (cosmetic, non-blocking):** confirm the `BROTHER_MARGIN=4`
safe border fixed the slight edge-clipping on the original identicon label.

## D2 — Establish `main` & land the work

**State.** The repo started empty; all 11 commits live on `feat/native-driver`.
There is no `main`/`master` branch and no remote.

**Recommendation:** after D1 passes, establish `main` from the verified
`feat/native-driver` HEAD (`git branch main feat/native-driver` or fast-forward
merge), and make it the default. Holding until verification so unverified code
isn't blessed as trunk. If you want a GitHub remote too, that's an outward action
I won't take without your say-so.

**Needs from you:** confirm the branch strategy (establish `main` post-verify?),
and whether to add a remote.

## D3 — OpenRouter key is dead

**State.** `OPENAI_API_KEY` / `OPENROUTER_API_KEY` (both `sk-or-v1…`) now return
**401 "User not found"** even on `/api/v1/key` — invalid/revoked (was a 402
out-of-credits before). Image generation via OpenRouter is down.

**Mitigation in place:** `gen_image` now falls back to SearXNG on 401/402/403
instead of crashing, and validates downloaded images; but SearXNG results are
unverified (this is how the captcha + AARCH64 junk got in), so they need a human
eyeball. I refetched + visually verified the two junk drawer icons by hand.

**Needs from you:** rotate/replace the OpenRouter key so clean image-gen returns.

## D4 — Scratch PNGs committed to `test/` (≈768K) — tracked as task #16

**State.** The initial commit swept 5 unreferenced binary PNGs into `test/`
(`generated_raw.png` alone is 676K). None are used by the test suite.

**Recommendation:** `git rm` `generated_raw.png` + `generated_label.png` (clearly
scratch dumps) and add `test/*.png` to `.gitignore`; optionally keep the tiny
`hello.png` / `cut_test.png` as manual print fixtures. Not deleting unilaterally —
these pre-existed in the working tree (I didn't author the images). Cheap to do
as part of the D2 landing.

**Needs from you:** OK to remove them (or which to keep)?

## D5 — DHCP reservation (router) — reliability

**Recommendation:** bind MAC `94:8C:D7:A3:C4:BF` → `192.168.8.249` on the router
so the printer's IP never changes. The driver already tolerates IP changes (mDNS
via `avahi-resolve` + IP fallback), so this is belt-and-suspenders, not required.

**Needs from you:** a one-time router config change (only you can do this).

---

## What proceeds automatically (no decision needed)

- The watcher verifies D1 on printer return and I report.
- The 22-label drawer backlog is QA'd, manifested, and queued in
  `cache/print_backlog.sh` (waits-for-IDLE between jobs, stops on failure). I will
  **not** auto-run the batch — I'll print one, you confirm orientation/clipping,
  then I run the rest on your go-ahead.
