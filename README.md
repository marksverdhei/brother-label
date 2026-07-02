# brother-label

Tooling to drive a **Brother VC-500W** color label printer over the LAN, plus an
icon/text label pipeline and a status TUI. Maintained by the "brother-label
clerk" agent; other agents send natural-language print requests.

## Architecture

Native AppSocket — one Python module talks XML-over-TCP directly to the printer
on port 9100. No CUPS, no qemu, no proxy in the print path.

```
label {print|icon|drawer|text|tag}  →  brother_print.send(jpeg, mode, cut)  →  printer:9100
                                          (print header → JPEG → ack → socket close ⇒ cut)
label {status|waybar} / lazy-brother →  brother_print.query("/status.xml")   →  printer:9100
```

This replaced the previous 5-layer stack (CUPS → ARM32 `zsocket` under `qemu-arm`
→ `cups-proxy` injecting auto-cut → printer), which was slow (~60 s/label),
fragile, and prone to leaving the printer stuck `BUSY`. See `docs/protocol.md`
for the wire protocol and why the old stack misbehaved.

## CLI

```
label print FILE            # print an image file (auto-cut)
label send FILE             # alias for print
label text "TEXT"           # render + print a text label
label icon "PROMPT"         # generate an icon (OpenRouter → comfy-openai → SearXNG) + print
label drawer "NAME"         # icon + caption drawer label
label tag NAME|UUID         # inventory shelf sticker
label tags                  # list inventory tags
label status                # printer + cassette + device status
label status --json         # one-line JSON for agents/scripts; exit 0 iff idle
label reset                 # clear a stuck job; reports whether the jammed
                            #   job's raster flushed as a delayed print
                            #   ("do NOT reprint") or not ("reprint it")
label cut                   # explain auto-cut behavior
label waybar                # JSON for the waybar module

Print options (print/send/text/icon/drawer/tag):
  --no-cut                  don't auto-cut after printing
  --mode {vivid,color,bw}   print quality (default vivid)
```

Environment:
- `OPENAI_API_KEY` / `OPENROUTER_API_KEY` — OpenRouter key for icon generation.
- `INVENTORY_API_BASE` — inventory tags API (default `http://centurion:30191/api`).
- `SEARXNG_BASE` — image-search fallback (default `http://centurion:30502`).
- `LABEL_USE_CUPS=1` — route through the dormant CUPS/qemu/proxy fallback instead
  of the native sender.
- `BROTHER_FLIP=1` — rotate labels 180° before printing. Verified 2026-06-10:
  native prints come out correctly oriented, so this stays off by default.
- `BROTHER_MARGIN=N` — white safe-margin (% per side, default 4) added around
  each image so edge content isn't clipped by the printable-area inset; set `0`
  to disable or raise for a wider margin.

## Components

- `bin/brother_print.py` — native driver: `query`, `send` (lockless print +
  close-to-cut), `wait_for_idle`, `convert_to_jpeg`, `reset`, `xml_field`,
  mode table.
- `bin/label` — the CLI; all printing flows through `lp()` → `brother_print.send`.
- `bin/lazy-brother` — btop-style TUI; live status + native print-event log
  (`cache/print.log`).

  <br><img src="docs/lazy-brother.gif" width="600" alt="lazy-brother TUI demo">

- `bin/label-sheet` — POC: pack multiple pictograms onto one strip
  (scissor-cut along printed guides) to amortize the ~0.4–0.55"/job feed+cut
  overhead; renders a true-physical-size preview (320 DPI → screen PPI,
  default 94.07 = Lenovo LT2452pwC) via `show-me`. Not wired into `label` yet.
- `bin/brother-keepalive` — pokes the printer (no `nokeepawake`) to hold WiFi.
- `bin/brother-watchdog` — re-enables the CUPS queue if it disables (fallback only).
- `systemd/` — keepalive + watchdog units. `install.sh` copies them into
  `/etc/systemd/system` (copies, not symlinks: symlinked units into `/home`
  dangle at boot before `/home` mounts and silently stay dead) and enables them.
- `docs/protocol.md` — the VC-500W wire protocol.
- `docs/RFC-001-landing-and-open-decisions.md` — open decisions blocked on Markus
  (hardware verify, establishing `main`, OpenRouter key, scratch-PNG cleanup, DHCP).
- `test/` — hardware-free unit tests: `test_protocol.py` (driver: XML builders,
  framed reads, send-safety, JPEG margin), `test_label.py` (CLI: icon-gen
  fallback, image validation), and `test_sheet.py` (label-sheet: packing
  geometry, ruler reservation, fit_tile/dashed_line). Run all:
  `python3 -m unittest discover -s test`.

## Setup

```bash
./install.sh        # symlink CLI, copy + enable systemd timers, set CUPS error policy
```

`bin/label` and `bin/lazy-brother` are symlinked onto `~/.local/bin` so any
agent or shell can call `label …` without knowing the repo path. The systemd
units are **copied** into `/etc/systemd/system` (not symlinked — a symlink into
`/home` dangles at boot before `/home` mounts, leaving the timers dead).

Installs:
- `brother-keepalive.timer` — every 3 min, holds the printer on the network.
- `brother-watchdog.timer` — every 30 s, re-enables the CUPS queue (fallback path).
- `lpadmin -p brother -o printer-error-policy=retry-job` — keeps the fallback
  CUPS queue from auto-disabling on a transient drop.

## Reliability notes

- **Speed:** data transfer is seconds; a full print + cut cycle is ~15-30 s
  end-to-end (vs ~60 s+ under qemu emulation, which also routinely wedged the
  device).
- **Stuck-BUSY:** print jobs take no `<lock>` at all — an orphaned lock was the
  classic wedge, and holding a lock without embedding its token in the print
  header makes the printer reject *your own* job as "busy". Aborts never send
  partial image data and the socket always closes, so an interrupted print no
  longer wedges the printer. Use `label reset` if it ever does get stuck;
  power-cycle as a last resort.
- **Auto-cut** is triggered by closing the data socket after the printer acks
  the image; `send()` does this on every job. IPP (port 631) can also print
  but offers no cut control (`finishings-supported = none`), which is why the
  driver uses port 9100.
- **SUCCESS can lie** (observed live): the firmware sometimes reports
  `IDLE/SUCCESS` for a label that never physically fed. The `<remain>` tape
  gauge is the only honest signal, so `send()` reads it before and after every
  job and raises if it didn't move ("label likely never fed; reprint it").
- **Eject jams on consecutive labels are intrinsic** (5 jams across 3 batches,
  2026-06-10/11): every ~2nd–3rd back-to-back auto-cut job throws `EJECT JAM`.
  Ruled out: label length (0.5–1.5" jams like 2"+), inter-job pauses (25s and
  45s both jammed), and exit-tray pile-up (jammed even with a human pulling
  each label as it dropped). Suspected cut-scrap/wear in the eject mechanism
  or a firmware bug — the 2026-03-15-04 firmware update (RFC-001 D6) is the
  leading fix candidate. **Plan ≤2 consecutive jobs unattended**; larger
  batches need a human on jam-clear duty. `label reset` does not clear these —
  only physically freeing the slot does, after which the printer returns to
  IDLE on its own. The tape gauge tells you whether the jammed job fed (gauge
  moved → don't reprint; unmoved → reprint) — but a label that fed *into* a
  jam may still come out mangled, so eyeball it.
- **Power-on routine (human step):** the printer boots into **Wireless Direct
  mode (white WiFi LED)** — its own AP, unreachable from the LAN. Hold the WiFi
  button ~2 s per step to cycle **white → off → blue**; blue = Infrastructure
  mode. *Blinking* blue means it's still associating; once **solid blue**,
  `label status` answers and any agent can print immediately — no software
  ritual, reset, or warm-up needed.
  - **Rare edge:** if it blinks blue indefinitely and never settles, the saved
    WiFi credentials are gone and it must be re-paired (Brother iPrint&Label
    app or WPS) — there is no headless path to re-enter credentials.
- **WiFi drops:** the printer falls off WiFi when idle. Mitigations:
  - `brother-keepalive.timer` holds the association.
  - The driver resolves `VC-500W3904.local` via **`avahi-resolve`** (this host
    has no nss-mdns, so `getaddrinfo` can't resolve `.local`), cached 30s, with
    `192.168.8.249` as the last-resort fallback — so a DHCP IP change is tolerated.
  - **Recommended (manual):** add a DHCP reservation on the router binding MAC
    `94:8C:D7:A3:C4:BF` → `192.168.8.249` so the IP never changes.

## Device

- Brother VC-500W, mDNS `VC-500W3904.local`, last-known IP `192.168.8.249`.
- 1" continuous color ZINK roll, 320 DPI, AppSocket XML on port 9100.
- Registered in CUPS as `brother` (used only by the `LABEL_USE_CUPS=1` fallback).

## Fallback (old stack, kept dormant)

The qemu/zsocket/proxy stack remains installed but unused:
`/opt/vc-500w/proxy.py` (`cups-proxy.service`), `/opt/vc-500w/zsocket_arm64`,
`/opt/zsb/` (ARM netpbm tools), and the `brother` CUPS queue. Set
`LABEL_USE_CUPS=1` to use it.
