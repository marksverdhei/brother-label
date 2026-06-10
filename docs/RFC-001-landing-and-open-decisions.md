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

## D2 — Establish `main` & land the work — ✅ RESOLVED 2026-06-11 (local)

`main` established from the hardware-verified `feat/native-driver` HEAD and
checked out as the working branch, under Markus's blanket go-ahead
("do the rest"). Future work: feature branches off `main`.

**Still open (outward action, needs explicit say-so):** whether to add a GitHub
remote and push.

## D3 — OpenRouter key is dead

**State.** `OPENAI_API_KEY` / `OPENROUTER_API_KEY` (both `sk-or-v1…`) now return
**401 "User not found"** even on `/api/v1/key` — invalid/revoked (was a 402
out-of-credits before). Image generation via OpenRouter is down.

**Mitigation in place:** `gen_image` now falls back to SearXNG on 401/402/403
instead of crashing, and validates downloaded images; but SearXNG results are
unverified (this is how the captcha + AARCH64 junk got in), so they need a human
eyeball. I refetched + visually verified the two junk drawer icons by hand.

**Needs from you:** rotate/replace the OpenRouter key so clean image-gen returns.

## D4 — Scratch PNGs committed to `test/` (≈768K) — ✅ RESOLVED 2026-06-11

All 5 unreferenced PNGs removed (`git rm`) with Markus's blanket go-ahead
("do the rest"); verified nothing outside this RFC referenced them and the
test suite passes without them. `test/*.png` added to `.gitignore` so scratch
prints can't sneak back in.

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

## D6 — Firmware update available: 2026-03-15-04 (we run 2022-07-13-22)

**State (found 2026-06-11).** The printer's own updater
(`/cgi-bin/get-versions`) offers `brotherupgrade-2026031504.tgz.gpg`
("Latest Ver:2026-03-15-04") — nearly 4 years newer than our build. No public
changelog exists.

**Why it's tempting:** could fix the boot-into-Wireless-Direct annoyance (D-/
task #18) and 4 years of unknown fixes.

**Why it's risky:** every verified behavior of the native driver — lockless
print acceptance, close-socket-to-cut, status codes, jpeg+autofit — was
validated against 2022071322. New firmware could change any of it, and there
is no documented downgrade path.

**Recommendation:** do it, but deliberately: pick a moment when reprinting is
cheap, update via the web UI (AirPrint page → CHOOSE FIRMWARE UPDATE, needs
blue/Infrastructure mode), then re-run the verification suite (test-card print
→ cut → gauge → IDLE) before trusting batches again. I won't trigger it
without your go-ahead.

**Needs from you:** yes/no, and when.
