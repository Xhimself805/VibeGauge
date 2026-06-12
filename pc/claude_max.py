#!/usr/bin/env python3
"""
claude_max.py - Stream your REAL Claude Max-plan usage to the STM32 OLED.

Data source: Anthropic's OAuth usage endpoint, the same one Claude Code's
`/usage` command uses:

    GET https://api.anthropic.com/api/oauth/usage

Auth is your Claude Code OAuth token, read at runtime from
~/.claude/.credentials.json (claudeAiOauth.accessToken). The token never leaves
your machine and is never printed.

Response shape:
    {
      "five_hour":  {"utilization": 33.0, "resets_at": "2026-..+00:00"},
      "seven_day":  {"utilization": 13.0, "resets_at": "2026-..+00:00"},
      "seven_day_opus": null,
      "seven_day_sonnet": {"utilization": 1.0, "resets_at": "..."},
      "extra_usage": {"is_enabled": false, ...}
    }
  utilization = percent consumed (0-100); resets_at = ISO-8601 UTC.

The endpoint rate-limits HARD (and per-token), so we fetch at most every
--fetch-interval seconds (>=180 enforced) and the critical
`User-Agent: claude-code/<version>` header is sent to avoid the throttled
bucket. Between fetches we just recompute the live "resets in" countdown and
re-send to the display every --display-interval seconds.

Wire protocol (shared with the firmware):  line1|line2|...|barPct\n

Usage:
    python claude_max.py --print --once          # fetch once, print, no serial
    python claude_max.py --port COM5             # stream to the OLED
    python claude_max.py --list-ports
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CRED_PATH = Path.home() / ".claude" / ".credentials.json"
MIN_FETCH_INTERVAL = 180  # endpoint throttles below this


def detect_cc_version(default="2.1.168"):
    """Read the Claude Code version from the newest session log so the
    User-Agent matches the real client."""
    proj = Path.home() / ".claude" / "projects"
    try:
        files = sorted(proj.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return default
    for f in files[:5]:
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"version"' not in line:
                        continue
                    try:
                        v = json.loads(line).get("version")
                    except json.JSONDecodeError:
                        continue
                    if v:
                        return v
        except OSError:
            continue
    return default


def read_token():
    if not CRED_PATH.exists():
        raise RuntimeError(f"{CRED_PATH} not found - are you logged into Claude Code?")
    data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("No claudeAiOauth.accessToken in credentials (log into Claude Code first).")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        print("WARNING: OAuth token looks expired; run any Claude Code command to refresh it.",
              file=sys.stderr)
    return token


def fetch_usage(token, version):
    req = urllib.request.Request(USAGE_URL, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    req.add_header("User-Agent", f"claude-code/{version}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_reset(resets_at, now):
    if not resets_at:
        return "?"
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    secs = max(0, int((dt - now).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def build_lines(usage, now):
    fh = usage.get("five_hour") or {}
    sd = usage.get("seven_day") or {}
    fh_u = fh.get("utilization") or 0.0
    sd_u = sd.get("utilization") or 0.0
    lines = [
        "Claude MAX usage",
        f"5h  {fh_u:3.0f}%  in {fmt_reset(fh.get('resets_at'), now)}",
        f"Wk  {sd_u:3.0f}%  in {fmt_reset(sd.get('resets_at'), now)}",
    ]
    bar = int(max(0, min(100, round(fh_u))))
    return lines, bar


def encode_packet(lines, pct):
    safe = [str(x).replace("|", "/")[:21] for x in lines]
    return ("|".join(safe) + f"|{int(pct)}\n").encode("utf-8")


def open_serial(port, baud):
    import serial
    s = serial.Serial(port, baud, timeout=1)
    time.sleep(2)  # let the board settle after the port opens
    return s


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("pyserial not installed. Run: pip install pyserial", file=sys.stderr)
        return
    for p in lp.comports():
        print(f"{p.device:10} {p.description}")


def main():
    ap = argparse.ArgumentParser(description="Stream real Claude Max usage to an STM32 OLED.")
    ap.add_argument("--port", help="Serial port, e.g. COM5 (omit with --print)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--fetch-interval", type=float, default=300.0,
                    help="Seconds between API fetches (clamped to >=180).")
    ap.add_argument("--display-interval", type=float, default=20.0,
                    help="Seconds between display refreshes (countdown stays live between fetches).")
    ap.add_argument("--version-tag", default=None,
                    help="Override the claude-code/<ver> User-Agent (auto-detected by default).")
    ap.add_argument("--print", action="store_true", help="Print to console instead of serial")
    ap.add_argument("--once", action="store_true", help="One fetch + send/print, then exit")
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args()

    if args.list_ports:
        list_ports()
        return

    fetch_interval = max(MIN_FETCH_INTERVAL, args.fetch_interval)
    version = args.version_tag or detect_cc_version()
    token = read_token()

    need_serial = not args.print
    if need_serial and not args.port:
        ap.error("--port is required (or use --print). Try --list-ports.")
    if need_serial:
        try:
            import serial  # noqa: F401  (fail fast if missing)
        except ImportError:
            print("pyserial not installed. Run: pip install pyserial", file=sys.stderr)
            sys.exit(1)

    ser = None
    usage = None
    last_fetch = 0.0
    try:
        while True:
            # (Re)connect serial if needed -- survives a not-yet-ready or
            # unplugged adapter so the script can auto-start at boot.
            if need_serial and ser is None:
                try:
                    ser = open_serial(args.port, args.baud)
                    print(f"[{datetime.now():%H:%M:%S}] serial connected on {args.port}", flush=True)
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] waiting for {args.port} ({e})", file=sys.stderr)
                    if args.once:
                        sys.exit(1)
                    time.sleep(5)
                    continue

            mono = time.monotonic()
            if usage is None or (mono - last_fetch) >= fetch_interval:
                try:
                    usage = fetch_usage(token, version)
                    last_fetch = mono
                except urllib.error.HTTPError as e:
                    body = ""
                    try:
                        body = e.read().decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                    if e.code == 429:
                        print(f"[{datetime.now():%H:%M:%S}] 429 rate-limited; keeping last value. {body}",
                              file=sys.stderr)
                    elif e.code == 401:
                        print(f"[{datetime.now():%H:%M:%S}] 401 unauthorized - token expired? "
                              f"Run a Claude Code command to refresh. {body}", file=sys.stderr)
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] HTTP {e.code}: {body}", file=sys.stderr)
                except urllib.error.URLError as e:
                    print(f"[{datetime.now():%H:%M:%S}] network error: {e.reason}", file=sys.stderr)

            if usage is not None:
                lines, bar = build_lines(usage, datetime.now(timezone.utc))
                packet = encode_packet(lines, bar)
                if ser is not None:
                    try:
                        ser.write(packet)
                        ser.flush()
                    except Exception as e:
                        print(f"[{datetime.now():%H:%M:%S}] serial write failed, reconnecting ({e})",
                              file=sys.stderr)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                print(f"[{datetime.now():%H:%M:%S}] {packet.decode('utf-8').strip()}", flush=True)

            if args.once:
                break
            time.sleep(args.display_interval)
    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            ser.close()


if __name__ == "__main__":
    main()
