# Brother VC-500W network protocol (AppSocket / port 9100)

The VC-500W ("Wedge") speaks raw XML over TCP port 9100 — no HTTP wrapper. Small
XML command blocks and large binary image payloads share one connection. This is
what `bin/brother_print.py` implements natively, replacing Brother's ARM32
`zsocket` backend (which we used to run under `qemu-arm`).

Every command is prefixed with:

```
<?xml version="1.0" encoding="UTF-8"?>\n
```

## Reading state (no lock needed)

```xml
<read>
<path>/status.xml</path>
<nokeepawake>1</nokeepawake>
</read>
```

- `/status.xml` → `print_state`, `print_job_stage`, `print_job_error`, `print_num`, `remain`, `online`, `capacity`.
- `/config.xml` → model, serial, firmware, MAC, DPI, cassette media features, `media_mode` table.
- `<nokeepawake>1</nokeepawake>` tells the printer **not** to stay awake. The
  keepalive timer omits this flag on purpose (`keep_awake=True`) to hold the WiFi
  association.

## Printing (header → image → ack → **close socket to cut**)

```
1. <print>
   <mode>vivid</mode><speed>0</speed><lpi>317</lpi>
   <width>0</width><height>0</height>
   <dataformat>jpeg</dataformat><autofit>1</autofit>
   <datasize>N</datasize>
   <cutmode>full</cutmode>
   </print>
      → response: <status><code>0</code></status>
        (sometimes with <comment>ready to receive</comment>; gate on code 0,
         NOT on the comment text. code 2 = busy/locked, code 3 = no media —
         any non-zero code: abort WITHOUT sending image data.)

2. <N bytes of JPEG>
      → response: <status><code>0</code></status>  ("print data received")

3. CLOSE THE TCP CONNECTION.
      The printer will NOT feed or cut until the socket closes; keeping it
      open leaves the device "waiting" with blinking lights. After close:
      printing → feeding → cutting → IDLE, ~8-12 s total.

4. Reconnect and poll /status.xml until <print_state> is IDLE.
```

### Key facts

- **No `<lock>` for printing.** A lock mechanism exists (`<lock><op>set</op>…`
  returns a `job_token`), but if you take the lock and then send a `<print>`
  header *without* embedding `<job_token>` inside it, the printer rejects your
  own header with code 2 "Printer busy" — you block yourself. This is exactly
  the bug that stalled the first native driver. The working zsocket backend and
  the USB path both print lockless; so do we. (sgrimee's driver shows the other
  valid variant: lock + token inside the `<print>` block.)
- **The socket close is part of the protocol** — it is what triggers the feed +
  cut cycle. This is also why prints from the old CUPS path only cut when the
  backend exited cleanly, and why IPP prints (port 631) never cut.
- **The metadata block and the image are separate writes.** The block ends with
  `</print>` and is < 50 KB; the JPEG follows as its own (large) write. This is
  proven by the old cups-proxy: it injected `<cutmode>full</cutmode>` immediately
  before `</print>` on a *small* chunk, then logged "Picture received" for the
  large chunk. `build_print_header()` therefore ends exactly in
  `…<cutmode>full</cutmode>\n</print>`.
- **`dataformat=jpeg` + `autofit=1`** lets the printer scale the image to the
  tape. We send a clean sRGB JPEG and let the firmware fit it — no rawrgb math,
  no netpbm tools, no qemu. (The zsocket backend instead sends pre-rasterized
  `dataformat=gray`/raw data with explicit padded `<width>/<height>` — both
  dataformats are accepted; jpeg+autofit is far simpler for us.)
- **`cutmode`**: `full` (cut), or `none`/omit for no cut. Auto-cut is now native;
  the cups-proxy is no longer required.
- **IPP (port 631) can print but never cut.** The printer is AirPrint-capable
  (`image/jpeg`/`image/png`/`image/urf` accepted via Print-Job), but
  `finishings-supported = none` — there is no cut control. Useful as a fallback
  path; not used by the driver.

### Mode table (from the device's own `config.xml`)

| mode  | speed | lpi |
|-------|-------|-----|
| vivid | 0     | 317 |
| color | 1     | 264 |
| bw    | 2     | 400 |

## Clearing a stuck job

`label reset` (→ `brother_print.reset()`) connects, acquires the lock, and
immediately releases it. If the firmware is wedged badly enough that this
doesn't help, a physical power-cycle is the only recovery.

## Sources & verification status

- **cups-proxy journal capture (2026-06-10,
  `docs/captures/zsocket-print-header-20260610.txt`)** — ground-truth bytes of
  the working zsocket path: lockless `<print>` header with raw `gray` data and
  explicit dims. This capture is what exposed the self-lock bug.
- `Sunburn-Schematics/brother-vc500w-driver` (`docs/protocol-captures.md`) —
  live captures documenting the code semantics (0 ok / 2 busy / 3 no-media) and
  the **close-socket-to-cut** behavior, plus the USB variant.
- `sgrimee/labelprinter-vc500w` (fork of the m7i.org reverse-engineering work) —
  command templates; jpeg+autofit header; the lock variant with `job_token`
  embedded in the `<print>` block.
- `corentin-soriano/vc-500w_autocut` — the auto-cut proxy that injects
  `<cutmode>full</cutmode>` before `</print>`, establishing the header/image
  framing this driver follows.
- **Verified on hardware 2026-06-10:** this native driver printed and auto-cut
  a real label end-to-end (header → JPEG → ack → close → cut → IDLE/SUCCESS),
  orientation correct without rotation. The IPP path was also exercised live:
  it prints but cannot cut.
