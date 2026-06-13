#!/usr/bin/env python3
"""
install_startup.py — Add (or remove) VibeGauge from Windows Startup.

Creates a hidden VBScript launcher in:
  %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VibeGauge.vbs

The VBS launcher calls `pythonw.exe` so no terminal window appears.
Run this once; re-run with --remove to uninstall from Startup.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

VBS_NAME = "VibeGauge.vbs"
APP_NAME = "vibegauge_app.py"


def find_pythonw():
    """Return the absolute path to pythonw.exe, best-effort."""
    # Same directory as the current python interpreter
    candidate = Path(sys.executable).parent / "pythonw.exe"
    if candidate.exists():
        return str(candidate)
    # Try a known common location
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "pythonw.exe"
    if local.exists():
        return str(local)
    return "pythonw"   # fallback — hope it's on PATH


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA environment variable not set.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install():
    script_path = Path(__file__).resolve().parent / APP_NAME
    if not script_path.exists():
        print(f"ERROR: {script_path} not found. Run this from the pc/ directory.", file=sys.stderr)
        sys.exit(1)

    pythonw = find_pythonw()
    vbs_path = startup_dir() / VBS_NAME

    vbs_content = f'''\
Set WShell = CreateObject("WScript.Shell")
WShell.Run """{pythonw}"" ""{script_path}""", 0, False
'''

    vbs_path.write_text(vbs_content, encoding="utf-8")
    print(f"Installed: {vbs_path}")
    print(f"Launcher:  {pythonw}")
    print(f"Script:    {script_path}")
    print()
    print("VibeGauge will start automatically at next Windows login.")
    print("Run with --remove to uninstall from Startup.")


def remove():
    vbs_path = startup_dir() / VBS_NAME
    if vbs_path.exists():
        vbs_path.unlink()
        print(f"Removed: {vbs_path}")
    else:
        print(f"Not installed ({vbs_path} not found).")


def main():
    ap = argparse.ArgumentParser(description="Install or remove VibeGauge from Windows Startup.")
    ap.add_argument("--remove", action="store_true", help="Remove from Startup instead of installing.")
    args = ap.parse_args()

    if args.remove:
        remove()
    else:
        install()


if __name__ == "__main__":
    main()
