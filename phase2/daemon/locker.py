"""Autonomous target lock: slew the designator onto a chosen contact,
lock, verify, correct if not.

7/21 rebuild after the first instrumented hunts. What the flight data taught:

  * DCS does NOT populate contact azimuth/elevation for FC3 — az_rad/el_rad
    arrived 0.0 every packet, so v1 "slewed" 0 deg and pressed lock at the
    designator reset position four times. Aim now comes from
    telemetry.contact_geometry(): contact world position vs ownship world
    position (self-validated against DCS's own exported distance).
  * s.locked (LoGetLockedTargetInformation) has never been observed
    populated; sight.LaunchAuthorized DID flip true on a hand lock. Verify
    is now defense-in-depth: s.locked id match (gold) -> LaunchAuthorized
    rising edge (silver, id unverified) -> fail.
  * v1 opened EVERY attempt with `unlock` to reset the designator, which
    would destroy any lock the verifier was merely blind to. Now: check for
    an existing success BEFORE resetting, and a silver-tier lock ends the
    hunt rather than being stomped.

Calibration: slew_rate_dps is per-jet, per-DCS-settings. Use the REPL:
`slew right 1.0` moves the designator for exactly 1 s; watch how many
degrees it travels on the HUD and `set` the rate accordingly. Persisted
in gains.json alongside everything else.

Interface (thread-safe, runs on its own worker):
    lm.lock_contact(target_id)   -> starts the hunt, returns immediately
    lm.status                    -> human-readable state string
    lm.abort()                   -> release slews, stop
"""
from __future__ import annotations

import math
import threading
import time

try:
    from .telemetry import contact_geometry
except ImportError:                      # flat-layout fallback
    from telemetry import contact_geometry


class LockManager:
    def __init__(self, sysx, tele, gains: dict):
        self.sysx = sysx
        self.tele = tele
        self.g = gains          # live reference to Autopilot.g (shares set/save)
        self.g.setdefault("slew_rate_dps", 12.0)   # calibrate in flight
        self.g.setdefault("slew_settle_s", 0.30)   # pause after slew before lock
        self.g.setdefault("lock_verify_s", 0.80)   # radar needs time to report
        self.g.setdefault("lock_max_attempts", 4)
        self.status = "idle"
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()

    # ---------- public ----------
    def lock_contact(self, target_id) -> bool:
        if self._thread and self._thread.is_alive():
            self.status = "busy — abort first"
            return False
        self._abort.clear()
        self._thread = threading.Thread(
            target=self._run, args=(target_id,), daemon=True)
        self._thread.start()
        return True

    def abort(self):
        self._abort.set()
        self.sysx.release_all_slew()
        self.status = "aborted"

    # ---------- internals ----------
    def _find(self, target_id):
        s = self.tele.latest()
        for c in s.contacts:
            if c.get("id") == target_id:
                return c
        return None

    def _locked_id(self):
        s = self.tele.latest()
        return s.locked[0].get("id") if s.locked else None

    def _launch_auth(self) -> bool:
        s = self.tele.latest()
        return bool(s.sight.get("LaunchAuthorized")) if s.sight else False

    def _aim(self, c):
        """(az_deg, el_deg, source) for a contact, or None.

        Geometry first (trustworthy); exported az/el only as a fallback and
        only if they are actually nonzero (0.0/0.0 is the known dead value).
        """
        s = self.tele.latest()
        geo = contact_geometry(s, c)
        if geo is not None:
            return geo                       # (az, el, frame_label)
        az = math.degrees(c.get("az_rad") or 0.0)
        el = math.degrees(c.get("el_rad") or 0.0)
        if abs(az) > 0.01 or abs(el) > 0.01:
            return az, el, "export"
        return None

    def _slew_axis(self, pos_cmd: str, neg_cmd: str, angle_deg: float,
                   scale: float):
        """Hold one slew direction for angle/rate seconds (scaled)."""
        if abs(angle_deg) < 0.5:
            return
        cmd = pos_cmd if angle_deg > 0 else neg_cmd
        dur = abs(angle_deg) / max(self.g["slew_rate_dps"], 0.5) * scale
        dur = min(dur, 6.0)      # sanity cap: never hold a slew > 6 s
        self.sysx.hold(cmd)
        t0 = time.monotonic()
        while time.monotonic() - t0 < dur:
            if self._abort.is_set():
                break
            time.sleep(0.02)
        self.sysx.release(cmd)

    def _run(self, target_id):
        try:
            for attempt in range(1, int(self.g["lock_max_attempts"]) + 1):
                if self._abort.is_set():
                    return

                # 0) do we ALREADY have it? (also protects a lock the id-
                #    verifier is blind to from being stomped on retry)
                if self._locked_id() == target_id:
                    self.status = f"LOCKED {target_id} (pre-existing)"
                    return

                c = self._find(target_id)
                if c is None:
                    self.status = f"contact {target_id} not in radar picture"
                    return

                aim = self._aim(c)
                if aim is None:
                    self.status = ("no aim data: geometry unavailable "
                                   "(missing px/pz or ownpos — old "
                                   "Export.lua?) and exported az/el are dead")
                    return
                az, el, src = aim

                # baseline for the LaunchAuthorized rising-edge check
                auth_before = self._launch_auth()

                # 1) known designator state: unlock resets to center
                self.status = f"attempt {attempt}: resetting designator"
                self.sysx.execute("unlock")
                time.sleep(0.4)

                # widen timing slightly each retry to bracket calibration error
                scale = 1.0 + 0.25 * (attempt - 1)

                # 2) slew: azimuth then elevation (sequential > diagonal for
                #    timing accuracy)
                self.status = (f"attempt {attempt}: slewing az {az:+.1f} "
                               f"el {el:+.1f} [{src}] (x{scale:.2f})")
                self._slew_axis("slew_right", "slew_left", az, scale)
                self._slew_axis("slew_up", "slew_down", el, scale)
                if self._abort.is_set():
                    return
                time.sleep(self.g["slew_settle_s"])

                # 3) lock and verify (defense in depth)
                self.status = f"attempt {attempt}: locking"
                self.sysx.execute("lock")
                time.sleep(self.g["lock_verify_s"])

                got = self._locked_id()
                if got == target_id:
                    self.status = f"LOCKED {target_id} (attempt {attempt})"
                    return
                if got is None and not auth_before and self._launch_auth():
                    # id channel blind, but launch authority appeared after
                    # our press: treat as success, do NOT stomp it.
                    self.status = (f"LOCKED (id unverified — LaunchAuthorized "
                                   f"rose on attempt {attempt})")
                    return
                self.status = (f"attempt {attempt}: locked "
                               f"{got if got is not None else 'nothing'}, "
                               f"wanted {target_id}")
            self.status = f"FAILED to lock {target_id} after " \
                          f"{int(self.g['lock_max_attempts'])} attempts"
        finally:
            self.sysx.release_all_slew()
