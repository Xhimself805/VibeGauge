#!/usr/bin/env python3
"""
claude_usage.py - Read local Claude Code usage logs, reconstruct the rolling
5-hour Max-plan window (+ weekly total), and stream a formatted status line to
an STM32 Blue Pill over USB-serial for display on a 0.96" SSD1306 OLED.

The Max plan has no official "remaining %" API, so we approximate it from the
usage Claude Code already records on disk under ~/.claude/projects/**/*.jsonl.
Each assistant turn logs message.usage = {input_tokens, output_tokens,
cache_creation_input_tokens, cache_read_input_tokens}. We weight those tokens
(cache reads are far cheaper, so they count less) and compare the current
5-hour block against a calibratable CAP to produce a percentage.

Wire protocol (one line per update, '\n'-terminated, UTF-8):
    <textline1>|<textline2>|...|<barPercent>
The firmware draws every '|'-separated field except the LAST as a text row,
top to bottom, then draws a progress bar from the last field (integer 0-100).

Usage:
    python claude_usage.py --port COM5                 # stream to device
    python claude_usage.py --print                     # print to console, no serial
    python claude_usage.py --list-ports                # show available COM ports
    python claude_usage.py --port COM5 --cap 25e6 --interval 10
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Token weighting -------------------------------------------------------
# Rate limits roughly track "effective" tokens. Output and fresh input/cache
# writes are full cost; cache *reads* are ~10% cost, so weight them low.
W_INPUT = 1.0
W_OUTPUT = 1.0
W_CACHE_CREATE = 1.0
W_CACHE_READ = 0.1

BLOCK_HOURS = 5  # Max plan rolling window length


def iter_usage_entries(claude_projects: Path, since: datetime):
    """Yield (timestamp_utc, weighted_tokens) for every assistant turn newer
    than `since`, across all projects. Cheap substring pre-filter avoids
    JSON-parsing the many non-usage log lines."""
    if not claude_projects.is_dir():
        return
    cutoff_mtime = (since - timedelta(days=1)).timestamp()
    for jf in claude_projects.rglob("*.jsonl"):
        try:
            if jf.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue
        try:
            with jf.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    ts_raw = obj.get("timestamp")
                    if not isinstance(usage, dict) or not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since:
                        continue
                    weighted = (
                        usage.get("input_tokens", 0) * W_INPUT
                        + usage.get("output_tokens", 0) * W_OUTPUT
                        + usage.get("cache_creation_input_tokens", 0) * W_CACHE_CREATE
                        + usage.get("cache_read_input_tokens", 0) * W_CACHE_READ
                    )
                    yield ts, weighted
        except OSError:
            continue


def active_block(entries, now):
    """Given chronologically-sorted (ts, tokens), return (start, end, tokens)
    for the 5-hour block containing `now`, or None if the window is idle.

    Block logic mirrors ccusage: a block starts at the floor-to-hour of its
    first activity and ends 5h later; a >5h gap or crossing the end starts a
    fresh block."""
    blocks = []
    start = end = None
    last_ts = None
    total = 0.0
    for ts, tok in entries:
        floor_hour = ts.replace(minute=0, second=0, microsecond=0)
        new_block = (
            start is None
            or ts >= end
            or (last_ts is not None and (ts - last_ts) >= timedelta(hours=BLOCK_HOURS))
        )
        if new_block:
            if start is not None:
                blocks.append((start, end, total))
            start = floor_hour
            end = start + timedelta(hours=BLOCK_HOURS)
            total = 0.0
        total += tok
        last_ts = ts
    if start is not None:
        blocks.append((start, end, total))

    if not blocks:
        return None
    s, e, t = blocks[-1]
    if now < e:
        return s, e, t
    return None  # most recent block already expired -> window is fresh/idle


def human_tokens(n):
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def fmt_duration(td):
    secs = int(td.total_seconds())
    if secs < 0:
        secs = 0
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def build_status(claude_projects, cap, now=None):
    """Compute the 4 text lines + bar percent for the display."""
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    entries = sorted(iter_usage_entries(claude_projects, since=week_ago), key=lambda e: e[0])

    week_tokens = sum(t for _, t in entries)
    blk = active_block(entries, now)

    if blk is None:
        pct = 0
        line2 = "0%   idle"
        line3 = "Window: fresh"
    else:
        start, end, used = blk
        pct = int(min(100, round(used / cap * 100))) if cap > 0 else 0
        line2 = f"{pct}%   {human_tokens(used)} tok"
        line3 = f"Reset in {fmt_duration(end - now)}"

    line1 = "Claude MAX (5h)"
    line4 = f"Week {human_tokens(week_tokens)}"
    return [line1, line2, line3, line4], pct


def encode_packet(lines, pct):
    safe = [str(x).replace("|", "/")[:21] for x in lines]
    return ("|".join(safe) + f"|{int(pct)}\n").encode("utf-8")


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("pyserial not installed. Run: pip install pyserial", file=sys.stderr)
        return
    ports = list(lp.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        print(f"{p.device:10} {p.description}")


def main():
    ap = argparse.ArgumentParser(description="Stream Claude Max-plan usage to an STM32 OLED.")
    ap.add_argument("--port", help="Serial port, e.g. COM5 (omit with --print)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--interval", type=float, default=10.0, help="Seconds between updates")
    ap.add_argument("--cap", type=float, default=25e6,
                    help="Weighted-token cap for 100%% of the 5h window. CALIBRATE this "
                         "to your plan by watching when you actually hit limits.")
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude" / "projects"),
                    help="Path to ~/.claude/projects")
    ap.add_argument("--print", action="store_true", help="Print to console instead of serial")
    ap.add_argument("--once", action="store_true", help="Send/print one update then exit")
    ap.add_argument("--list-ports", action="store_true", help="List serial ports and exit")
    args = ap.parse_args()

    if args.list_ports:
        list_ports()
        return

    claude_projects = Path(args.claude_dir)
    ser = None
    if not args.print:
        if not args.port:
            ap.error("--port is required (or use --print). Try --list-ports.")
        try:
            import serial
        except ImportError:
            print("pyserial not installed. Run: pip install pyserial", file=sys.stderr)
            sys.exit(1)
        ser = serial.Serial(args.port, args.baud, timeout=1)
        time.sleep(2)  # let the board reset/settle after port open

    try:
        while True:
            lines, pct = build_status(claude_projects, args.cap)
            packet = encode_packet(lines, pct)
            if ser is not None:
                ser.write(packet)
                ser.flush()
            label = packet.decode("utf-8").strip()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}", flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            ser.close()


if __name__ == "__main__":
    main()
