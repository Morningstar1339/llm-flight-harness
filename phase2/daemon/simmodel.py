"""Crude point-mass flight model for testing the control cascade.

NOT a Flanker. It exists so the container can prove the *logic* converges:
cascade wiring, heading wrap, maneuver sequencing, REPL. Gains tuned here
are starting points only — the real Su-27 retune happens on the box.

Dynamics (SI): bank commanded by aileron with lag; turn rate = g*tan(bank)/TAS;
VV commanded by elevator with lag and damping; mach from throttle minus
drag with a bank penalty.
"""
from __future__ import annotations

import math

G = 9.81


class MockAircraft:
    def __init__(self, hdg_deg=90.0, alt_m=3000.0, mach=0.7):
        self.hdg = math.radians(hdg_deg)
        self.bank = 0.0
        self.alt = alt_m
        self.vv = 0.0
        self.mach = mach
        self.t = 0.0
        self.fuel = 5500.0

    @property
    def tas(self):
        return self.mach * 320.0   # rough a at altitude

    def step(self, aileron, elevator, throttle, dt):
        # roll: aileron commands bank rate, up to ~150 deg/s
        bank_rate_cmd = aileron * math.radians(150.0)
        self.bank += (bank_rate_cmd - 0.0) * dt
        self.bank = max(-math.radians(80), min(math.radians(80), self.bank))
        # natural roll damping toward zero when no input
        self.bank *= (1.0 - 0.02 * dt)

        # heading from coordinated turn
        self.hdg = (self.hdg + G * math.tan(self.bank) / max(80.0, self.tas) * dt) % (2 * math.pi)

        # pitch: elevator commands VV with first-order response
        vv_cmd = elevator * 80.0                  # m/s at full deflection
        self.vv += (vv_cmd - self.vv) * min(1.0, 1.2 * dt)
        # turning eats lift: uncompensated bank causes sink
        self.vv -= (1.0 / max(0.3, math.cos(self.bank)) - 1.0) * 18.0 * dt
        self.alt += self.vv * dt

        # speed: thrust vs drag, banking adds induced drag
        accel = throttle * 0.045 - 0.022 - abs(math.sin(self.bank)) * 0.010 \
            - abs(self.vv) * 0.00025 * (1 if self.vv > 0 else -1)
        self.mach = max(0.3, min(1.6, self.mach + accel * dt))

        self.fuel = max(0.0, self.fuel - (0.4 + throttle * 1.2) * dt)
        self.t += dt

    def packet(self, seq):
        """Emit a Phase 1 style packet dict."""
        return {
            "v": 1, "seq": seq, "t": self.t,
            "ownship": {"name": "MockFlanker", "hdg_rad": self.hdg,
                        "pitch": 0.0, "bank": self.bank},
            "asl_m": self.alt, "agl_m": self.alt - 200.0,
            "vv_ms": self.vv, "mach": self.mach,
            "ias_ms": self.tas * 0.75, "tas_ms": self.tas,
            "g": 1.0 / max(0.3, math.cos(self.bank)),
            "fuel": {"internal": self.fuel, "external": 0},
            "contacts": [], "rwr": [],
        }
