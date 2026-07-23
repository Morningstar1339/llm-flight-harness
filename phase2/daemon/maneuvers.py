"""Canned maneuvers — the primitives the model will eventually call.

Each maneuver is a *setpoint program*: it stores context (e.g. the commit
heading), rewrites the autopilot's targets, and lets the PID cascade do
the flying. In Phase 2 the reference bearing comes from the REPL user;
in Phase 3 the model supplies it from the contact picture.

Doctrine notes baked in:
  crank    — offset ~50 deg from the reference bearing: keeps the target
             near the radar gimbal limit while reducing closure.
  notch    — put the threat on the beam (90 deg off) and unload;
             basic version holds altitude rather than descending.
  pump     — turn cold (reciprocal of threat bearing), descend 2000 m
             floor-limited, run at high mach.
  recommit — turn back to the stored commit heading, resume commit alt/mach.
"""
from __future__ import annotations

import math

from .controllers import wrap_pi

DEG = math.pi / 180.0


class ManeuverManager:
    def __init__(self, ap):
        self.ap = ap
        self.active = "none"
        self.commit_hdg = None    # heading to return to on recommit
        self.commit_alt = None
        self.commit_mach = None

    def _remember_commit(self, state):
        if self.commit_hdg is None:
            self.commit_hdg = self.ap.tgt_hdg if self.ap.tgt_hdg is not None else state.hdg
            self.commit_alt = self.ap.tgt_alt if self.ap.tgt_alt is not None else state.alt
            self.commit_mach = self.ap.tgt_mach if self.ap.tgt_mach is not None else max(0.8, state.mach)

    def fly(self, hdg_rad=None, alt_m=None, mach=None):
        """Direct steer — clears any maneuver and (re)sets targets."""
        self.active = "fly"
        if hdg_rad is not None:
            self.ap.tgt_hdg = hdg_rad % (2 * math.pi)
            self.commit_hdg = self.ap.tgt_hdg
        if alt_m is not None:
            self.ap.tgt_alt = alt_m
            self.commit_alt = alt_m
        if mach is not None:
            self.ap.tgt_mach = mach
            self.commit_mach = mach

    def crank(self, state, direction: str, ref_hdg_rad: float, offset_deg: float = 50.0):
        self._remember_commit(state)
        sign = -1.0 if direction.startswith("l") else 1.0
        self.ap.tgt_hdg = (ref_hdg_rad + sign * offset_deg * DEG) % (2 * math.pi)
        self.active = f"crank_{direction[0]}"

    def notch(self, state, direction: str, threat_bearing_rad: float):
        self._remember_commit(state)
        sign = -1.0 if direction.startswith("l") else 1.0
        self.ap.tgt_hdg = (threat_bearing_rad + sign * 90.0 * DEG) % (2 * math.pi)
        self.active = f"notch_{direction[0]}"

    def pump(self, state, threat_bearing_rad: float | None = None):
        self._remember_commit(state)
        ref = threat_bearing_rad if threat_bearing_rad is not None else (
            self.commit_hdg if self.commit_hdg is not None else state.hdg)
        self.ap.tgt_hdg = (ref + math.pi) % (2 * math.pi)
        floor = 1500.0
        self.ap.tgt_alt = max(floor, (self.ap.tgt_alt or state.alt) - 2000.0)
        self.ap.tgt_mach = max(self.ap.tgt_mach or 0.0, 0.95)
        self.active = "pump"

    def recommit(self, state):
        if self.commit_hdg is None:
            return False
        self.ap.tgt_hdg = self.commit_hdg
        if self.commit_alt is not None:
            self.ap.tgt_alt = self.commit_alt
        if self.commit_mach is not None:
            self.ap.tgt_mach = self.commit_mach
        self.active = "recommit"
        return True

    def describe(self, state):
        def fmt_hdg(r):
            return f"{(math.degrees(r) % 360):03.0f}" if r is not None else "---"
        parts = [f"mode={self.active}",
                 f"tgt hdg {fmt_hdg(self.ap.tgt_hdg)}",
                 f"alt {self.ap.tgt_alt:.0f}m" if self.ap.tgt_alt is not None else "alt ---",
                 f"mach {self.ap.tgt_mach:.2f}" if self.ap.tgt_mach is not None else "mach ---"]
        if self.commit_hdg is not None and self.active not in ("fly", "none"):
            parts.append(f"(commit {fmt_hdg(self.commit_hdg)})")
        err = ""
        if self.ap.tgt_hdg is not None:
            err = f"  hdg_err {math.degrees(wrap_pi(self.ap.tgt_hdg - state.hdg)):+.1f}"
        return "  ".join(parts) + err
