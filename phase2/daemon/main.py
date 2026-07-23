"""ClaudeHarness Phase 2 daemon — inner-loop autopilot + REPL.

Live mode (on the box, DCS running, vJoy installed):
    python -m daemon.main
Mock mode (anywhere — flies the built-in point-mass model):
    python -m daemon.main --mock

REPL (aviation units in, SI internally):
    fly hdg 270 alt 20000 mach 0.9     any subset of the three
    crank l [ref_brg] [deg]            default ref = current tgt hdg, 50 deg
    notch r <threat_brg>
    pump [threat_brg]
    recommit
    test <ail|elev|thr>                bench sweep one axis (MANUAL only) — verify
                                       stick direction in cockpit before engaging
    manual                             center vJoy; your stick has authority
    auto                               resume autopilot output
    status                             one-line state + setpoints
    watch [n]                          print status every second (n secs, default 10)
    sys / btn <n> / contacts           list systems map / raw button pulse / radar picture\n    radar eos lock unlock wpn fire dispense airbrake\n    lock <id> / lockstat / lockabort   autonomous designator hunt onto a contact\n    slew <dir> [secs]                  manual slew (calibrate slew_rate_dps)\n                                       combat systems (fire has a lock interlock)\n    gains / set <gain> <value> / savegains
    quit
"""
from __future__ import annotations

import argparse
import math
import shlex
import sys
import threading
import time

from .controllers import Autopilot, wrap_pi
from .maneuvers import ManeuverManager
from .output import make_output
from .systems import SystemsExecutor
from .locker import LockManager
from .simmodel import MockAircraft
from .telemetry import TelemetryListener

FT = 0.3048
LOOP_HZ = 20.0


def envelope_violation(s) -> str | None:
    """Return a reason string if the jet is outside controlled flight.
    The PID cascade assumes attached airflow; outside these bounds its
    output is garbage (often pro-spin). Guard fires -> instant MANUAL."""
    import math as _m
    if abs(s.bank) > _m.radians(100):
        return f"bank {abs(_m.degrees(s.bank)):.0f}deg — inverted/departed"
    if s.ias and s.ias < 90.0:            # ~175 kt IAS
        return f"IAS {s.ias * 1.944:.0f}kt — below stall margin"
    if abs(s.vv) > 70.0:                  # ~13,800 fpm
        return f"VV {s.vv * 196.85:+.0f}fpm — outside envelope"
    if s.aoa and abs(s.aoa) > _m.radians(20):
        return f"AoA {_m.degrees(s.aoa):.0f}deg — stall regime"
    return None


class Daemon:
    def __init__(self, mock: bool, gains_path: str):
        self.mock = mock
        self.tele = TelemetryListener()
        self.ap = Autopilot(gains_path)
        self.mm = ManeuverManager(self.ap)
        self.out = make_output(mock)
        self.sysx = SystemsExecutor(self.out)
        self.lm = LockManager(self.sysx, self.tele, self.ap.g)
        self.engaged = False   # SAFETY: starts in MANUAL. Engaging is explicit.
        self._stop = threading.Event()
        self._last_seq = -1     # last telemetry seq we acted on
        self._last_simt = None  # sim time of that packet
        self._rec = None        # CSV flight recorder file handle
        self._sim = MockAircraft() if mock else None

    # ---------- control loop ----------
    def start(self):
        if not self.mock:
            self.tele.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        dt = 1.0 / LOOP_HZ
        seq = 0
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            step = now - last if not self.mock else dt
            last = now
            if self.mock:
                seq += 1
                self.tele.inject(self._sim.packet(seq))
            s = self.tele.latest()
            if self.engaged and not self.mock:
                reason = envelope_violation(s)
                if reason:
                    self.engaged = False
                    self.out.center()
                    print(f"\n*** ENVELOPE GUARD: {reason} ***"
                          f"\n*** AUTOPILOT DISENGAGED — YOUR JET. Recover, then 'auto'. ***")
                    continue
            if self.engaged and (self.mock or s.fresh):
                if not self.mock:
                    # Act exactly once per telemetry packet. The loop and the
                    # exporter are both ~20 Hz but unsynchronized; free-running
                    # meant ~half the ticks re-processed a stale packet and
                    # derivative terms saw doubled/zeroed spikes.
                    if s.seq == self._last_seq:
                        time.sleep(0.005)
                        continue
                    if (self._last_simt is not None
                            and 0.0 < s.t - self._last_simt < 0.5):
                        step = s.t - self._last_simt   # true dt, sim clock
                    self._last_seq, self._last_simt = s.seq, s.t
                a, e, th = self.ap.update(s, step)
                self.out.send(a, e, th)
                self._record(s, step, a, e, th)
                if self.mock:
                    self._sim.step(a, e, th, dt)
            elif self.mock:
                self._sim.step(0.0, 0.0, 0.5, dt)
            time.sleep(dt if self.mock else 0.002)

    # ---------- flight recorder ----------
    def rec_start(self):
        import os
        os.makedirs("logs", exist_ok=True)
        path = time.strftime("logs/flight_%Y%m%d_%H%M%S.csv")
        self._rec = open(path, "w", buffering=1)
        self._rec.write("wall_t,sim_t,seq,dt,alt_m,vv_ms,pitch_deg,aoa_deg,"
                        "bank_deg,mach,tas_ms,tgt_vv,tgt_pitch_deg,"
                        "ail,elev,thr,pitch_i\n")
        print(f"recording -> {path}")

    def rec_stop(self):
        if self._rec:
            self._rec.close()
            self._rec = None

    def _record(self, s, dt, a, e, th):
        if not self._rec:
            return
        try:
            pid = getattr(self.ap, "pid_pitch", None) or getattr(self.ap, "pid_vv", None)
            i_state = getattr(pid, "i", 0.0) if pid else 0.0
            self._rec.write(
                f"{time.monotonic():.3f},{s.t:.3f},{s.seq},{dt:.4f},"
                f"{s.alt:.1f},{s.vv:.2f},{math.degrees(s.pitch):.2f},"
                f"{math.degrees(s.aoa):.2f},{math.degrees(s.bank):.2f},"
                f"{s.mach:.3f},{s.tas:.1f},"
                f"{getattr(self.ap, 'dbg_tgt_vv', 0.0):.2f},"
                f"{math.degrees(getattr(self.ap, 'dbg_tgt_pitch', 0.0)):.2f},"
                f"{a:.4f},{e:.4f},{th:.4f},{i_state:.4f}\n")
        except Exception:
            pass   # the recorder must never take down the control loop

    def stop(self):
        self.rec_stop()
        self._stop.set()
        time.sleep(0.1)
        self.out.close()
        self.tele.stop()

    # ---------- bench test: verify surface signs without flying ----------
    def bench_test(self, axis: str):
        """Slow single-axis sweep while the pilot watches the cockpit stick /
        control surfaces (F2 view). Only callable in MANUAL."""
        plans = {
            "ail": [("commanding RIGHT roll — stick should move RIGHT", (0.5, 0.0)),
                    ("commanding LEFT roll — stick should move LEFT", (-0.5, 0.0)),
                    ("centering", (0.0, 0.0))],
            "elev": [("commanding PULL — stick should move AFT (nose-up)", (0.0, 0.5)),
                     ("commanding PUSH — stick should move FORWARD", (0.0, -0.5)),
                     ("centering", (0.0, 0.0))],
            "thr": None,
        }
        if axis.startswith("thr"):
            print("throttle sweep: 30%% -> 70%% -> 50%%  (watch RPM / throttle quadrant)")
            for tgt in (0.3, 0.7, 0.5):
                print(f"  throttle {tgt:.0%}")
                self.out.send(0.0, 0.0, tgt)
                time.sleep(2.5)
            self.out.center()
            return
        key = "ail" if axis.startswith("a") else "elev"
        for label, (a, e) in plans[key]:
            print(f"  {label}")
            # ramp in over ~1.5 s, hold ~2 s
            for step in range(15):
                self.out.send(a * (step + 1) / 15.0, e * (step + 1) / 15.0,
                              self.out.throttle)
                time.sleep(0.1)
            time.sleep(2.0)
        self.out.center()
        print("sweep done, vJoy centered. If any direction was backwards, "
              "flip the matching sign constant in daemon/output.py.")

    # ---------- status ----------
    def status_line(self) -> str:
        s = self.tele.latest()
        stale = "" if (self.mock or s.fresh) else "  [STALE TELEMETRY]"
        eng = "AUTO" if self.engaged else "MANUAL"
        return (f"[{eng}] hdg {math.degrees(s.hdg) % 360:05.1f}  "
                f"alt {s.alt / FT:7,.0f}ft  M{s.mach:.2f}  "
                f"vv {s.vv * 196.85:+5.0f}fpm  bank {math.degrees(s.bank):+5.1f}  "
                f"fuel {s.fuel_kg:,.0f}kg{stale}\n"
                f"       {self.mm.describe(s)}")


def parse_kv(tokens):
    """['hdg','270','alt','20000'] -> {'hdg': 270.0, 'alt': 20000.0}"""
    out = {}
    i = 0
    while i + 1 < len(tokens) + 1 and i < len(tokens):
        if i + 1 >= len(tokens):
            raise ValueError(f"missing value for {tokens[i]!r}")
        out[tokens[i].lower()] = float(tokens[i + 1])
        i += 2
    return out


def repl(d: Daemon):
    print(__doc__)
    print("daemon running in MANUAL." + (" (MOCK MODE — flying the point-mass model)" if d.mock else " Bench-test surfaces with 'test elev' / 'test ail' / 'test thr', then 'auto' to engage."))
    while True:
        try:
            line = input("ap> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "quit"
        if not line:
            continue
        try:
            toks = shlex.split(line)
            cmd, args = toks[0].lower(), toks[1:]
            s = d.tele.latest()

            if cmd == "fly":
                kv = parse_kv(args)
                d.mm.fly(
                    hdg_rad=math.radians(kv["hdg"]) if "hdg" in kv else None,
                    alt_m=kv["alt"] * FT if "alt" in kv else None,
                    mach=kv["mach"] if "mach" in kv else None)
                if not d.engaged:
                    print("targets set — daemon is in MANUAL. Type 'auto' to engage.")
                print(d.status_line())
            elif cmd == "test":
                if d.engaged:
                    print("refusing: bench test only runs in MANUAL. Type 'manual' first.")
                    continue
                d.bench_test(args[0] if args else "elev")
            elif cmd == "crank":
                direction = args[0]
                ref = math.radians(float(args[1])) if len(args) > 1 else (
                    d.ap.tgt_hdg if d.ap.tgt_hdg is not None else s.hdg)
                off = float(args[2]) if len(args) > 2 else 50.0
                d.mm.crank(s, direction, ref, off)
                print(d.status_line())
            elif cmd == "notch":
                d.mm.notch(s, args[0], math.radians(float(args[1])))
                print(d.status_line())
            elif cmd == "pump":
                brg = math.radians(float(args[0])) if args else None
                d.mm.pump(s, brg)
                print(d.status_line())
            elif cmd == "recommit":
                ok = d.mm.recommit(s)
                print(d.status_line() if ok else "no commit reference stored yet")
            elif cmd == "manual":
                d.engaged = False
                d.out.center()
                d.rec_stop()
                print("autopilot OFF — vJoy centered, your stick has the jet")
            elif cmd == "auto":
                bad = None if d.mock else envelope_violation(s)
                if bad:
                    print(f"refusing to engage: {bad}. Recover to level-ish flight first.")
                    continue
                d.ap.reset()
                d.rec_start()
                d.engaged = True
                print("autopilot ON")
            elif cmd == "status":
                print(d.status_line())
            elif cmd == "watch":
                secs = int(args[0]) if args else 10
                for _ in range(secs):
                    print(d.status_line())
                    time.sleep(1.0)
            elif cmd == "gains":
                for k in sorted(d.ap.g):
                    print(f"  {k:14s} {d.ap.g[k]}")
            elif cmd == "set":
                d.ap.set_gain(args[0], float(args[1]))
                print(f"{args[0]} = {args[1]} (live; 'savegains' to persist)")
            elif cmd == "savegains":
                d.ap.save_gains()
                print("gains saved")
            elif cmd in ("quit", "exit"):
                d.stop()
                print("daemon stopped, vJoy centered.")
                return
            elif cmd == "sys":
                print(d.sysx.describe())
            elif cmd == "btn":
                d.sysx.pulse_raw(int(args[0]))
                print(f"pulsed vJoy button {args[0]}")
            elif cmd == "contacts":
                if not s.contacts and not s.rwr:
                    print("no radar contacts, no RWR emitters")
                for c in s.contacts:
                    az = math.degrees(c.get("az_rad") or 0.0)
                    rng = (c.get("dist_m") or 0.0) / 1852.0
                    altf = (c.get("alt_m") or 0.0) / FT
                    lockmark = " [LOCKED]" if any(
                        l.get("id") == c.get("id") for l in s.locked) else ""
                    print(f"  id {c.get('id')}  az {az:+6.1f}  {rng:5.1f} nm  "
                          f"{altf:7,.0f} ft  M{c.get('mach') or 0:.2f}{lockmark}")
                for e in s.rwr:
                    az = math.degrees(e.get("az_rad") or 0.0)
                    print(f"  RWR: type {e.get('type')} az {az:+6.1f} "
                          f"pri {e.get('priority')}")
            elif cmd == "fire":
                if not s.locked and "force" not in args:
                    print("FOX interlock: no locked target. 'fire force' to override.")
                else:
                    d.sysx.execute("fire")
                    tgt = s.locked[0].get("id") if s.locked else "(unlocked)"
                    print(f"FOX — weapon release commanded, target {tgt}. "
                          f"R-27 is SARH: keep the lock.")
            elif cmd == "lock" and args:
                tid = int(args[0])
                if d.lm.lock_contact(tid):
                    print(f"lock manager hunting contact {tid} "
                          f"('lockstat' to watch, 'lockabort' to stop)")
                else:
                    print(d.lm.status)
            elif cmd == "sight":
                print(s.sight if s.sight else
                      "no sighting-system data in telemetry "
                      "(probe not installed, or the API returns nothing for FC3)")
            elif cmd == "lockstat":
                print(f"  {d.lm.status}")
            elif cmd == "lockabort":
                d.lm.abort()
                print("lock hunt aborted, slews released")
            elif cmd == "slew":
                # calibration: slew <up|down|left|right> [seconds]
                name = f"slew_{args[0]}"
                dur = float(args[1]) if len(args) > 1 else 1.0
                d.sysx.hold(name); time.sleep(dur); d.sysx.release(name)
                print(f"held {name} for {dur:.2f} s — measure degrees moved "
                      f"on the HUD, then: set slew_rate_dps <deg/sec>")
            elif cmd in d.sysx.names():
                d.sysx.execute(cmd)
                print(f"{cmd} commanded")
                if cmd == "lock":
                    time.sleep(0.6)
                    s2 = d.tele.latest()
                    print(f"  locked: {[l.get('id') for l in s2.locked] or 'nothing yet'}")
            else:
                print(f"unknown command {cmd!r}")
        except Exception as e:  # REPL must not die on bad input
            print(f"error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="fly the built-in mock model")
    ap.add_argument("--gains", default="gains.json")
    args = ap.parse_args()
    d = Daemon(mock=args.mock, gains_path=args.gains)
    d.start()
    repl(d)


if __name__ == "__main__":
    main()
