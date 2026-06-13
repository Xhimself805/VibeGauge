#!/usr/bin/env python3
"""
vibegauge_app.py - VibeGauge status window + background serial streamer.

Closing the window hides it to the system tray. Right-click the tray icon
to show the window again or quit entirely.  Run with `pythonw vibegauge_app.py`
to hide the console.
"""

import json
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

USAGE_URL   = "https://api.anthropic.com/api/oauth/usage"
CRED_PATH   = Path.home() / ".claude" / ".credentials.json"
CONFIG_PATH = Path.home() / ".claude" / "vibegauge_config.json"
MIN_FETCH_INTERVAL = 180


# ── helpers ───────────────────────────────────────────────────────────────────

def detect_cc_version(default="2.1.168"):
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
        raise RuntimeError(f"{CRED_PATH} not found — are you logged into Claude Code?")
    data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("No claudeAiOauth.accessToken in credentials.")
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


def build_display(usage, now):
    fh   = usage.get("five_hour") or {}
    sd   = usage.get("seven_day") or {}
    fh_u = fh.get("utilization") or 0.0
    sd_u = sd.get("utilization") or 0.0
    fh_reset = fmt_reset(fh.get("resets_at"), now)
    wk_reset = fmt_reset(sd.get("resets_at"), now)
    return int(round(fh_u)), fh_reset, int(round(sd_u)), wk_reset


def encode_packet(fh_pct, fh_reset, wk_pct, wk_reset):
    # Protocol: fh_pct|fh_reset|wk_pct|wk_reset|HH:MM\n
    time_str = datetime.now().strftime("%H:%M")
    return f"{fh_pct}|{fh_reset}|{wk_pct}|{wk_reset}|{time_str}\n".encode("utf-8")


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def make_tray_image():
    """64×64 RGBA icon: dark circle with a small bar-chart."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    # Background disc
    d.ellipse([2, 2, size - 2, size - 2], fill=(30, 30, 46))
    # Three bars (blue / blue / pink)
    bar_color  = (137, 180, 250)
    peak_color = (243, 139, 168)
    d.rectangle([10, 42, 20, 54], fill=bar_color)
    d.rectangle([26, 30, 36, 54], fill=bar_color)
    d.rectangle([42, 16, 52, 54], fill=peak_color)
    return img


# ── GUI ───────────────────────────────────────────────────────────────────────

BG   = "#1e1e2e"
FG   = "#cdd6f4"
GRAY = "#6c7086"
ACC1 = "#f38ba8"
ACC2 = "#89b4fa"


class VibeGaugeApp:
    POLL_MS = 2000

    def __init__(self):
        self.cfg         = load_config()
        self.stop_event  = threading.Event()
        self._usage      = None
        self._usage_lock = threading.Lock()
        self._ser        = None
        self._status     = "Starting…"
        self._tray       = None

        self.root = tk.Tk()
        self.sv_fh_pct  = tk.StringVar(value="—")
        self.sv_fh_in   = tk.StringVar(value="—")
        self.sv_wk_pct  = tk.StringVar(value="—")
        self.sv_wk_in   = tk.StringVar(value="—")
        self.sv_serial  = tk.StringVar(value="—")
        self.sv_status  = tk.StringVar(value="Starting…")

        self._build_window()
        self._start_tray()

        # First-run: ask for COM port
        if "port" not in self.cfg:
            self._show_settings(first_run=True)
            if "port" not in self.cfg:
                self._quit_app()
                return

        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._worker.start()

        self.root.after(self.POLL_MS, self._tick)
        self.root.mainloop()

    # ── tray ─────────────────────────────────────────────────────────────────

    def _start_tray(self):
        icon_img = make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("Show VibeGauge", self._show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon("VibeGauge", icon_img, "VibeGauge", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _show_window(self, icon=None, item=None):
        self.root.after(0, self._do_show_window)

    def _do_show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_app(self, icon=None, item=None):
        self.stop_event.set()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.root.after(0, self.root.destroy)

    # ── window ────────────────────────────────────────────────────────────────

    def _build_window(self):
        r = self.root
        r.title("VibeGauge")
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self._on_close)
        r.attributes("-topmost", True)
        r.geometry("290x210")
        r.eval("tk::PlaceWindow . center")
        r.configure(bg=BG)

        tk.Label(r, text="VibeGauge", font=("Segoe UI", 13, "bold"),
                 bg=BG, fg=FG).pack(pady=(14, 0))
        tk.Label(r, text="Claude Max → STM32 OLED",
                 font=("Segoe UI", 8), bg=BG, fg=GRAY).pack()

        ttk.Separator(r).pack(fill="x", padx=14, pady=8)

        grid = tk.Frame(r, bg=BG)
        grid.pack(padx=18, fill="x")

        def stat_row(row, label, sv_pct, sv_in, accent):
            tk.Label(grid, text=label, font=("Segoe UI", 9), bg=BG, fg=FG,
                     anchor="w", width=8).grid(row=row, column=0, sticky="w")
            tk.Label(grid, textvariable=sv_pct, font=("Segoe UI", 9, "bold"),
                     bg=BG, fg=accent, width=6, anchor="e").grid(row=row, column=1)
            tk.Label(grid, text="resets in", font=("Segoe UI", 9),
                     bg=BG, fg=GRAY).grid(row=row, column=2, padx=(6, 2))
            tk.Label(grid, textvariable=sv_in, font=("Segoe UI", 9),
                     bg=BG, fg=GRAY, anchor="w", width=8).grid(row=row, column=3, sticky="w")

        stat_row(0, "5h usage:", self.sv_fh_pct, self.sv_fh_in, ACC1)
        stat_row(1, "7d usage:", self.sv_wk_pct, self.sv_wk_in, ACC2)

        ttk.Separator(r).pack(fill="x", padx=14, pady=8)

        bot = tk.Frame(r, bg=BG)
        bot.pack(padx=18, fill="x")
        tk.Label(bot, text="Serial:", font=("Segoe UI", 9), bg=BG, fg=GRAY).pack(side="left")
        tk.Label(bot, textvariable=self.sv_serial, font=("Segoe UI", 9),
                 bg=BG, fg=GRAY).pack(side="left", padx=4)
        tk.Button(bot, text="⚙ Settings", font=("Segoe UI", 8),
                  command=self._show_settings,
                  bg=BG, fg=ACC2, bd=0, cursor="hand2",
                  activebackground=BG, activeforeground=FG).pack(side="right")

        tk.Label(r, textvariable=self.sv_status, font=("Segoe UI", 7),
                 bg=BG, fg=GRAY).pack(pady=(2, 10))

    def _on_close(self):
        """Hide to tray instead of quitting."""
        self.root.withdraw()

    # ── settings dialog ───────────────────────────────────────────────────────

    def _show_settings(self, first_run=False):
        # Make sure the window is visible before opening the dialog
        self._do_show_window()

        dlg = tk.Toplevel(self.root)
        dlg.title("Settings")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.geometry("320x180")
        dlg.configure(bg=BG)

        tk.Label(dlg, text="VibeGauge Settings", font=("Segoe UI", 11, "bold"),
                 bg=BG, fg=FG).pack(pady=(14, 4))

        frm = tk.Frame(dlg, bg=BG)
        frm.pack(padx=20, fill="x")

        tk.Label(frm, text="COM port:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=12, anchor="w").grid(row=0, column=0, pady=4)
        port_var   = tk.StringVar(value=self.cfg.get("port") or "")
        port_entry = tk.Entry(frm, textvariable=port_var, width=12,
                              bg="#313244", fg=FG, insertbackground=FG, relief="flat")
        port_entry.grid(row=0, column=1, sticky="w", padx=4)
        tk.Label(frm, text="(e.g. COM5; blank = print only)",
                 font=("Segoe UI", 8), bg=BG, fg=GRAY).grid(row=0, column=2, padx=6)

        if first_run:
            tk.Label(dlg, text="Enter the COM port your STM32 is on, then click Save.",
                     font=("Segoe UI", 8), bg=BG, fg=GRAY, wraplength=280).pack(pady=4)

        def _save():
            p = port_var.get().strip()
            if p and p.isdigit():
                p = f"COM{p}"
            self.cfg["port"] = p if p else None
            save_config(self.cfg)
            self._kick_serial()
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(pady=10)
        tk.Button(btn_row, text="Save", command=_save, font=("Segoe UI", 9),
                  bg="#313244", fg=FG, relief="flat", padx=12).pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, font=("Segoe UI", 9),
                  bg="#313244", fg=FG, relief="flat", padx=12).pack(side="left", padx=4)

        port_entry.focus()
        dlg.wait_window()

    def _kick_serial(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ── UI tick ───────────────────────────────────────────────────────────────

    def _tick(self):
        with self._usage_lock:
            usage = self._usage

        if usage is not None:
            now = datetime.now(timezone.utc)
            fh_pct, fh_reset, wk_pct, wk_reset = build_display(usage, now)
            self.sv_fh_pct.set(f"{fh_pct}%")
            self.sv_wk_pct.set(f"{wk_pct}%")
            self.sv_fh_in.set(fh_reset)
            self.sv_wk_in.set(wk_reset)

        port = self.cfg.get("port")
        if self._ser is not None:
            self.sv_serial.set(f"Connected ({port})")
        elif port:
            self.sv_serial.set(f"Waiting for {port}…")
        else:
            self.sv_serial.set("No port — print only")

        self.sv_status.set(self._status)

        if not self.stop_event.is_set():
            self.root.after(self.POLL_MS, self._tick)

    # ── background serial/fetch loop ──────────────────────────────────────────

    def _run_loop(self):
        version          = detect_cc_version()
        fetch_interval   = max(MIN_FETCH_INTERVAL, self.cfg.get("fetch_interval", 300))
        display_interval = self.cfg.get("display_interval", 20)
        last_fetch = 0.0

        while not self.stop_event.is_set():
            port = self.cfg.get("port")

            if port and self._ser is None:
                try:
                    import serial
                    self._ser = serial.Serial(port, self.cfg.get("baud", 115200), timeout=1)
                    time.sleep(2)
                    self._status = f"Connected {port} at {datetime.now():%H:%M:%S}"
                except Exception as e:
                    self._status = f"{port}: {e}"
                    self._sleep_interruptible(5)
                    continue

            mono = time.monotonic()
            if last_fetch == 0 or (mono - last_fetch) >= fetch_interval:
                try:
                    token = read_token()  # re-read each time; picks up Claude Code token refreshes
                    data = fetch_usage(token, version)
                    with self._usage_lock:
                        self._usage = data
                    last_fetch = mono
                    self._status = f"Fetched at {datetime.now():%H:%M:%S}"
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        self._status = "429 rate-limited; using last value"
                        last_fetch = mono  # back off; don't retry every 20s
                    elif e.code == 401:
                        self._status = "401 unauthorized — run Claude Code to refresh token"
                    else:
                        self._status = f"HTTP {e.code}"
                except urllib.error.URLError as e:
                    self._status = f"Network: {e.reason}"
                except Exception as e:
                    self._status = f"Fetch error: {e}"

            with self._usage_lock:
                usage = self._usage
            if usage is not None:
                now = datetime.now(timezone.utc)
                fh_pct, fh_reset, wk_pct, wk_reset = build_display(usage, now)
                packet = encode_packet(fh_pct, fh_reset, wk_pct, wk_reset)
                if self._ser is not None:
                    try:
                        self._ser.write(packet)
                        self._ser.flush()
                    except Exception as e:
                        self._status = f"Serial error: {e}"
                        try:
                            self._ser.close()
                        except Exception:
                            pass
                        self._ser = None

            self._sleep_interruptible(display_interval)

        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass

    def _sleep_interruptible(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))


def main():
    VibeGaugeApp()


if __name__ == "__main__":
    main()
