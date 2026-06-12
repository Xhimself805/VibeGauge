#!/usr/bin/env python3
"""Tiny serial reader for diagnostics. Prints whatever the board sends.

    python serial_monitor.py --port COM5 --seconds 6
"""
import argparse
import time
from datetime import datetime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="How long to read before exiting (0 = forever)")
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    end = None if args.seconds == 0 else time.monotonic() + args.seconds
    buf = b""
    try:
        while end is None or time.monotonic() < end:
            data = ser.read(256)
            if data:
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
