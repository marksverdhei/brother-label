# RFC-001 — Landing the native driver & open decisions

**Status:** Open items are Markus-only calls (router config, firmware, key, remote)
**Author:** brother-label clerk (Claude)
**Branch:** `main` (the native-driver work landed here; local-only, no remote yet)

This RFC consolidates the decisions I can't make alone, each with a recommendation,
so they're resolvable in one pass. Everything software-side that I *can* do without
the hardware is already done and verified (native driver, 30 hardware-free tests,
hardened image pipeline, docs, deployed reliability timers). Hardware verification
(D1) and landing `main` (D2) are now done; what remains below is genuinely
blocked on you.

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

**Update (2026-06-12) — a working keyless backend exists.** The on-cluster
`comfy-openai` service (`http://192.168.8.158:30385`, i.e. centurion NodePort
30385, LAN-only, no auth) generates clean pictograms via an OpenAI-shaped
endpoint, verified end-to-end this week on the RX470 icon and the 7-icon
PC-parts set:

```
POST /v1/images/generations
{"model":"z-image-turbo","prompt":"…","n":1,"size":"1024x1024","response_format":"b64_json"}
→ data[0].b64_json   (NOT OpenRouter's images[] shape)
```

Cold start ~50–60 s (model goes cold after ~600 s idle or a titan-gemma
reload); ~3 s warm. Avoid `qwen-image-edit` (~10 min/job). Coordinate GPU
windows with snoop-kube.

**Recommendation:** wire `z-image-turbo` as the **primary** `label icon`
backend, demote dead OpenRouter to an optional first-try (only if a key is
present), keep SearXNG as the unverified last resort. This resolves D3 without
a paid key and is a contained patch to `bin/label`'s `gen_image`. I've held off
because re-pointing the default generator is a behaviour change I'd rather you
greenlight.

**Needs from you:** either (a) say "wire comfy-openai" and I land the patch
(+ a test), or (b) rotate/replace the OpenRouter key if you'd rather keep that
the default. Option (a) needs no secret and no recurring cost.

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

**Evidence strengthening the case (2026-06-11):** the consecutive-job EJECT
JAM is now confirmed intrinsic — 5 jams across 3 batches with length, pauses,
and exit-tray pile-up all ruled out (it jammed with a human pulling each label
as it dropped). Batching is capped at ~2 unattended jobs until this is fixed,
and a firmware eject bug is the leading suspect. This moves the update from
"tempting" to "probably necessary for unattended batch printing".

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
