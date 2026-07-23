#!/usr/bin/env python3
"""Simulates the Export.lua stream, including failure modes."""
import json, socket, time

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
DEST = ("127.0.0.1", 27015)

def send(obj):
    s.sendto(json.dumps(obj).encode(), DEST)

def pkt(seq, t, **kw):
    base = {
        "v": 1, "seq": seq, "t": t,
        "ownship": {"name": "Su-27", "lat": 42.1, "lon": -104.8,
                    "alt_m": 5000, "hdg_rad": 1.571, "pitch": 0.02, "bank": -0.01},
        "ias_ms": 180.0, "tas_ms": 210.0, "mach": 0.72, "aoa_rad": 0.06,
        "vv_ms": 1.2, "asl_m": 5000, "agl_m": 3800, "g": 1.0,
        "fuel": {"internal": 5300, "external": 0, "rpm_l": 82, "rpm_r": 83},
        "contacts": [], "rwr": [],
    }
    base.update(kw)
    return base

send({"v": 1, "seq": 0, "hello": True}); time.sleep(0.3)

# normal flow
for i in range(1, 5):
    send(pkt(i, 10.0 + i * 0.5)); time.sleep(0.4)

# a packet carrying lua-side errors + a contact + lock
send(pkt(5, 12.5,
         errors=["LoGetTWSInfo: attempt to index a nil value"],
         contacts=[{"id": 42, "dist_m": 68000, "az_rad": -0.12,
                    "mach": 0.9, "alt_m": 7000}],
         locked=[{"id": 42, "dist_m": 68000}],
         g=3.2, mach=0.95))
time.sleep(0.4)

# sequence gap (packets 6-8 "lost")
send(pkt(9, 14.5)); time.sleep(0.4)

# stall: silence > 2s, then resume
time.sleep(3.0)
send(pkt(10, 17.5)); time.sleep(0.3)

send({"v": 1, "goodbye": True})
print("test stream complete")
