# VibeGauge

**A little hardware gauge for your Claude Max usage.** ⚡

Show your Claude **Max-plan usage** on a 0.96" SSD1306 OLED driven by an
STM32F103 "Blue Pill". A Python script on your PC fetches the **real, online**
usage from Anthropic's servers and streams it over USB-serial; the firmware
just draws whatever the PC sends.

> Built for a team sharing one Max login — the gauge shows the *combined*
> account usage, live, so everyone can glance at how much of the 5-hour and
> weekly windows is left.

<!-- Tip: drop a photo of your assembled gauge here once it's running:
     ![VibeGauge](docs/vibegauge.jpg) -->

License: [MIT](LICENSE).

```
  api.anthropic.com/api/oauth/usage   (server-side: all devices, whole account)
            │  (OAuth token from ~/.claude)
            ▼
  pc/claude_max.py  ──USB-serial──►  Blue Pill (USART1)  ──I2C──►  SSD1306 OLED
```

**Why online, not local:** the usage is read from Anthropic's `/api/oauth/usage`
endpoint — the same source as Claude Code's `/usage`. Because our team shares
**one Max login**, that number is the *combined* usage of everyone on the
account, across every machine. (Parsing local `~/.claude` logs would only see
this one PC — see `claude_usage.py` for that offline-estimate fallback.)

## What it shows

```
 Claude MAX usage
 5h   49%  in 2h08m      <- 5-hour window: % used + time to reset
 Wk   19%  in 21h18m     <- weekly window: % used + time to reset
 [████████░░░░░░░░░]     <- progress bar = 5h window %
```

---

## 1. Hardware & wiring

| OLED pin | Blue Pill pin | Note            |
|----------|---------------|-----------------|
| VCC      | 3V3           |                 |
| GND      | GND           |                 |
| SCL      | PB6           | I2C1 clock      |
| SDA      | PB7           | I2C1 data       |

**Data link** — USB‑TTL serial adapter (CH340/CP2102/FT232) ↔ USART1:

| Adapter | Blue Pill |
|---------|-----------|
| TX      | PA10 (RX) |
| RX      | PA9  (TX) |
| GND     | GND       |
| 3.3V    | 3V3       |

The same USB‑TTL adapter also **flashes** the chip via the STM32 serial
bootloader (no ST‑Link needed) — see step 2.

---

## 2. Firmware (VSCode + PlatformIO)

Already built and flashed in this project, but to rebuild/reflash:

1. Install the **PlatformIO IDE** VSCode extension (bundles its own toolchain).
2. **Open Folder →** `firmware/`. PlatformIO auto-installs the U8g2 library.
3. The firmware auto-detects the OLED at I²C `0x3C` or `0x3D` and prints an
   I²C scan + heartbeat over USART1 (handy for debugging a blank screen).
4. **Flash over the USB‑TTL serial bootloader** (`upload_protocol = serial`,
   `upload_port = COM5` in `platformio.ini`):
   - Move the **BOOT0 jumper to 1**, press **RESET**.
   - PlatformIO **Upload** (→), or: `pio run -t upload`.
   - Move **BOOT0 back to 0**, press **RESET**. The OLED shows "Waiting for PC...".

---

## 3. PC side — stream the real usage

Python 3.12 is installed; `pyserial` is the only extra dependency (the OAuth
fetch uses the stdlib). To set up from scratch on another machine:

```powershell
winget install Python.Python.3.12        # if Python isn't installed
pip install -r pc\requirements.txt        # pyserial
```

**Run it** (easiest: double-click `pc\start_display.bat`), or manually:

```powershell
cd d:\Personal\STM32Display\pc
python claude_max.py --list-ports         # find your COM port (CH340 = COM5 here)
python claude_max.py --port COM5          # stream the real online usage
```

Leave it running in the background. It re-fetches from Anthropic every 5 min
(the endpoint throttles below ~3 min) and refreshes the on-screen countdown
every 20 s.

### Test without the board

```powershell
python claude_max.py --print --once       # fetch once, print the packet, no serial
```

### Auto-start on login (optional)
Put a shortcut to `pc\start_display.bat` in your Startup folder
(`Win+R` → `shell:startup`) so the display comes up whenever you log in.

---

## How it works

- **Auth:** the OAuth token is read at runtime from `~/.claude/.credentials.json`
  (`claudeAiOauth.accessToken`). It never leaves your machine and is never
  printed. The request sends `User-Agent: claude-code/<version>` — **required**,
  or the endpoint drops you in an aggressively throttled bucket.
- **Endpoint response** (`/api/oauth/usage`):
  ```json
  { "five_hour": {"utilization": 49.0, "resets_at": "...+00:00"},
    "seven_day": {"utilization": 19.0, "resets_at": "...+00:00"},
    "seven_day_opus": null, "seven_day_sonnet": {...}, "extra_usage": {...} }
  ```
- **Wire protocol** (PC → firmware, one line per update): `line1|line2|...|barPct\n`.
  The firmware draws every `|`-field except the last as a text row, then the
  last field as a 0–100 progress bar. Change what's shown by editing
  `build_lines()` in `pc/claude_max.py` — **no reflash needed**.

## Files

| File | Purpose |
|------|---------|
| `pc/claude_max.py` | **Primary** — fetch real online usage, stream to OLED |
| `pc/start_display.bat` | Double-click launcher for `claude_max.py` |
| `pc/claude_usage.py` | Offline fallback — estimates usage from local `~/.claude` logs |
| `pc/serial_monitor.py` | Read the board's serial output (I²C scan / heartbeat) for debugging |
| `firmware/` | PlatformIO project (Blue Pill + U8g2) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| OLED stuck on "Waiting for PC..." | Script not running, wrong COM port, or TX/RX swapped (USB‑TTL TX must go to PA10). |
| Blank OLED | Run `serial_monitor.py --port COM5` and read the I²C scan: "(no devices)" = wiring/power; firmware auto-handles 0x3C/0x3D. |
| `401 unauthorized` | OAuth token expired — run any Claude Code command to refresh, then restart the script. |
| `429 rate-limited` | Fetching too often; keep `--fetch-interval` ≥ 180. The script keeps showing the last value. |
| `python` opens Microsoft Store | Use `winget install Python.Python.3.12`, then a **new** terminal. |
