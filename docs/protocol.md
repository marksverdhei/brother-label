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

## Printing (lock → print → image → release)

```
1. <lock><op>set</op><page_count>-1</page_count><job_timeout>99</job_timeout></lock>
      → response contains <job_token>…</job_token>

2. <print>
   <mode>vivid</mode><speed>0</speed><lpi>317</lpi>
   <width>0</width><height>0</height>
   <dataformat>jpeg</dataformat><autofit>1</autofit>
   <datasize>N</datasize>
   <cutmode>full</cutmode>
   </print>
      → response: <status><code>0</code><comment>ready to receive</comment></status>

3. <N bytes of JPEG>
      → response: "print data received" / "Picture received"

4. <lock><op>cancel</op><job_token>…</job_token></lock>      ← ALWAYS send this
```

### Key facts

- **The metadata block and the image are separate writes.** The block ends with
  `</print>` and is < 50 KB; the JPEG follows as its own (large) write. This is
  proven by the old cups-proxy: it injected `<cutmode>full</cutmode>` immediately
  before `</print>` on a *small* chunk, then logged "Picture received" for the
  large chunk. `build_print_header()` therefore ends exactly in
  `…<cutmode>full</cutmode>\n</print>`.
- **`dataformat=jpeg` + `autofit=1`** lets the printer scale the image to the
  tape. We send a clean sRGB JPEG and let the firmware fit it — no rawrgb math,
  no netpbm tools, no qemu.
- **The lock is why the printer used to get stuck.** `job_timeout=99` holds the
  lock; if the client dies before step 4, the printer stays `BUSY/PROCESSING`
  until the lock times out or it's power-cycled. The native driver releases the
  lock in a `finally`, so interrupting a job no longer wedges the device.
- **`cutmode`**: `full` (cut), or `none`/omit for no cut. Auto-cut is now native;
  the cups-proxy is no longer required.

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

## Sources

- `sgrimee/labelprinter-vc500w` (fork of the m7i.org reverse-engineering work) —
  exact command templates and the lock/token mechanism.
- `corentin-soriano/vc-500w_autocut` — the auto-cut proxy whose journal proves
  the header/image framing.
- Confirmed against a live capture of the working `zsocket` traffic via the
  cups-proxy journal (see the verification notes in `README.md`).
