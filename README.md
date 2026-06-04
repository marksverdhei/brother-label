# brother-label

Tooling to drive a **Brother VC-500W** color label printer over the LAN, plus an
icon/text label pipeline and a status TUI. Maintained by the "brother-label
clerk" agent; other agents send natural-language print requests.

## Architecture

Native AppSocket — one Python module talks XML-over-TCP directly to the printer
on port 9100. No CUPS, no qemu, no proxy in the print path.

```
label {print|icon|drawer|text|tag}  →  brother_print.send(jpeg, mode, cut)  →  printer:9100
                                          (lock → print header → JPEG → release lock)
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
label icon "PROMPT"         # generate an icon (OpenRouter → SearXNG fallback) + print
label drawer "NAME"         # icon + caption drawer label
label tag NAME|UUID         # inventory shelf sticker
label tags                  # list inventory tags
label status                # printer + cassette + device status
label reset                 # clear a stuck job (releases the printer lock)
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
- `BROTHER_FLIP=1` — rotate labels 180° before printing (if they come out
  upside-down; orientation unverified until the first native print is checked).
- `BROTHER_MARGIN=N` — white safe-margin (% per side, default 4) added around
  each image so edge content isn't clipped by the printable-area inset; set `0`
  to disable or raise for a wider margin.

## Components

- `bin/brother_print.py` — native driver: `query`, `lock`/`unlock`, `send`,
  `convert_to_jpeg`, `reset`, `xml_field`, mode table.
- `bin/label` — the CLI; all printing flows through `lp()` → `brother_print.send`.
- `bin/lazy-brother` — btop-style TUI; live status + native print-event log
  (`cache/print.log`).
- `bin/brother-keepalive` — pokes the printer (no `nokeepawake`) to hold WiFi.
- `bin/brother-watchdog` — re-enables the CUPS queue if it disables (fallback only).
- `systemd/` — keepalive + watchdog units. `install.sh` symlinks and enables them.
- `docs/protocol.md` — the VC-500W wire protocol.
- `test/test_protocol.py` — hardware-free unit tests (`python3 test/test_protocol.py`).

## Setup

```bash
./install.sh        # symlink + enable systemd timers; set CUPS error policy
```

Installs:
- `brother-keepalive.timer` — every 3 min, holds the printer on the network.
- `brother-watchdog.timer` — every 30 s, re-enables the CUPS queue (fallback path).
- `lpadmin -p brother -o printer-error-policy=retry-job` — keeps the fallback
  CUPS queue from auto-disabling on a transient drop.

## Reliability notes

- **Speed:** native prints in ~3 s vs ~60 s under qemu emulation.
- **Stuck-BUSY:** the driver always releases the job lock in a `finally`, so
  interrupting a print no longer wedges the printer. Use `label reset` if it ever
  does get stuck; power-cycle only as a last resort.
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
