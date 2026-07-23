#!/usr/bin/env python3
"""
ClaudeHarness Phase 1 receiver.

Listens for telemetry from the DCS Export.lua, prints a live status line,
and — critically — makes failure VISIBLE instead of silent:
  * STALL warnings when the stream stops (no packet for STALL_SEC)
  * GAP warnings when sequence numbers skip (packets lost)
  * Lua-side errors shipped in the packet's "errors" field, printed in red
Everything received is appended to telemetry.jsonl for later inspection.

Usage:  python receiver.py [--port 27015] [--raw]
  --raw  print full JSON of every packet instead of the status line
"""

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime

RED = "\033[91m"
YEL = "\033[93m"
GRN = "\033[92m"
DIM = "\033[2m"
RST = "\033[0m"

STALL_SEC = 2.0

MS_TO_KT = 1.943844
M_TO_FT = 3.28084
RAD_TO_DEG = 57.29578


def fmt(v, spec, scale=1.0):
    if v is None:
        return "--"
    try:
        return format(v * scale, spec)
    except (TypeError, ValueError):
        return "?"


def status_line(p):
    own = p.get("ownship") or {}
    hdg = fmt(own.get("hdg_rad"), "03.0f", RAD_TO_DEG)
    alt = fmt(p.get("asl_m"), ",.0f", M_TO_FT)
    ias = fmt(p.get("ias_ms"), ".0f", MS_TO_KT)
    mach = fmt(p.get("mach"), ".2f")
    g = fmt(p.get("g"), ".1f")
    fuel = "--"
    if isinstance(p.get("fuel"), dict):
        fi = p["fuel"].get("internal")
        fe = p["fuel"].get("external") or 0
        if fi is not None:
            fuel = f"{(fi + fe):,.0f}kg"
    n_con = len(p.get("contacts") or [])
    n_rwr = len(p.get("rwr") or [])
    n_lock = len(p.get("locked") or [])
    lock_s = f" {YEL}LOCK{RST}" if n_lock else ""
    return (
        f"seq {p.get('seq', '?'):>6}  t={fmt(p.get('t'), '7.1f')}s  "
        f"hdg {hdg}\N{DEGREE SIGN}  alt {alt}ft  {ias}kt  M{mach}  {g}G  "
        f"fuel {fuel}  con:{n_con} rwr:{n_rwr}{lock_s}"
    )


def main():
    if os.name == "nt":
        os.system("")  # enables ANSI colors in Windows cmd
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=27015)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--log", default="telemetry.jsonl")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", args.port))
    sock.settimeout(0.25)

    print(f"{GRN}listening on 127.0.0.1:{args.port} — start/unpause a DCS mission{RST}")
    print(f"{DIM}logging every packet to {args.log}{RST}")

    log = open(args.log, "a", buffering=1)
    last_seq = None
    last_rx = None
    stalled = False
    n_pkts = 0
    n_errs = 0

    while True:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            now = time.monotonic()
            if last_rx is not None and not stalled and now - last_rx > STALL_SEC:
                stalled = True
                print(
                    f"\n{RED}*** STALL: no packets for {now - last_rx:.1f}s "
                    f"(last seq {last_seq}). If the mission is still running, "
                    f"the export died — check errors above and "
                    f"Saved Games\\DCS\\Logs\\dcs.log ***{RST}"
                )
            continue
        except KeyboardInterrupt:
            break

        now = time.monotonic()
        wall = datetime.now().strftime("%H:%M:%S")
        if stalled:
            print(f"{GRN}*** stream resumed after stall ***{RST}")
        stalled = False
        last_rx = now
        n_pkts += 1

        text = data.decode("utf-8", errors="replace")
        log.write(f'{{"rx":"{wall}","pkt":{text}}}\n')

        try:
            p = json.loads(text)
        except json.JSONDecodeError as e:
            n_errs += 1
            print(f"{RED}[{wall}] bad JSON ({e}): {text[:120]}{RST}")
            continue

        if p.get("hello"):
            print(f"{GRN}[{wall}] mission export started (hello){RST}")
            last_seq = 0
            continue
        if p.get("goodbye"):
            print(f"{YEL}[{wall}] mission export stopped cleanly (goodbye){RST}")
            last_seq = None
            last_rx = None  # clean exit: don't warn about silence after this
            continue

        seq = p.get("seq")
        if isinstance(seq, int) and isinstance(last_seq, int) and seq > last_seq + 1:
            print(f"{YEL}[{wall}] GAP: seq jumped {last_seq} -> {seq} "
                  f"({seq - last_seq - 1} packets lost){RST}")
        last_seq = seq if isinstance(seq, int) else last_seq

        errs = p.get("errors")
        if errs:
            n_errs += 1
            for e in errs:
                print(f"{RED}[{wall}] lua: {e}{RST}")

        if args.raw:
            print(json.dumps(p, indent=None))
        else:
            print(status_line(p))

    print(f"\n{n_pkts} packets received, {n_errs} with errors. Log: {args.log}")


if __name__ == "__main__":
    main()
