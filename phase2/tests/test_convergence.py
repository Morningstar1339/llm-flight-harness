"""Closed-loop tests: autopilot + mock model, no threads, deterministic."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.controllers import Autopilot, wrap_pi
from daemon.maneuvers import ManeuverManager
from daemon.simmodel import MockAircraft
from daemon.telemetry import State, parse_packet

DT = 0.05
FT = 0.3048


def run(sim, ap, seconds, mm=None, callbacks=None):
    """Step sim+autopilot; callbacks = {t: fn(state, mm)} fired once each."""
    st = State()
    fired = set()
    for i in range(int(seconds / DT)):
        st = parse_packet(sim.packet(i), st)
        if callbacks:
            for ct, fn in callbacks.items():
                if sim.t >= ct and ct not in fired:
                    fired.add(ct)
                    fn(st, mm)
        a, e, th = ap.update(st, DT)
        sim.step(a, e, th, DT)
    return parse_packet(sim.packet(9_999_999), st)


def settled(sim, ap, hdg_tol=2.0, alt_tol=30.0, mach_tol=0.02):
    ok = True
    msgs = []
    if ap.tgt_hdg is not None:
        e = abs(math.degrees(wrap_pi(ap.tgt_hdg - sim.hdg)))
        ok &= e < hdg_tol
        msgs.append(f"hdg_err={e:.2f}deg")
    if ap.tgt_alt is not None:
        e = abs(ap.tgt_alt - sim.alt)
        ok &= e < alt_tol
        msgs.append(f"alt_err={e:.1f}m")
    if ap.tgt_mach is not None:
        e = abs(ap.tgt_mach - sim.mach)
        ok &= e < mach_tol
        msgs.append(f"mach_err={e:.3f}")
    return ok, "  ".join(msgs)


def test_basic_capture():
    """090/3000m/M0.7 -> 270/4500m/M0.9 and hold."""
    sim = MockAircraft(hdg_deg=90, alt_m=3000, mach=0.70)
    ap = Autopilot()
    mm = ManeuverManager(ap)
    mm.fly(hdg_rad=math.radians(270), alt_m=4500, mach=0.90)
    run(sim, ap, 180)
    ok, msg = settled(sim, ap)
    print(f"  basic_capture: {msg}")
    assert ok, f"failed to converge: {msg}"


def test_north_crossing():
    """350 -> 010 must go the short way (right), never the long way."""
    sim = MockAircraft(hdg_deg=350, alt_m=4000, mach=0.8)
    ap = Autopilot()
    mm = ManeuverManager(ap)
    mm.fly(hdg_rad=math.radians(10), alt_m=4000, mach=0.8)
    max_right_excursion = 0.0
    st = State()
    for i in range(int(60 / DT)):
        st = parse_packet(sim.packet(i), st)
        a, e, th = ap.update(st, DT)
        sim.step(a, e, th, DT)
        # if it ever turns left past ~330 it took the long way
        h = math.degrees(sim.hdg)
        assert not (200 < h < 330), f"took the long way around: hdg={h:.0f}"
        max_right_excursion = max(max_right_excursion, math.degrees(wrap_pi(sim.hdg - math.radians(10))))
    ok, msg = settled(sim, ap, alt_tol=60)
    print(f"  north_crossing: {msg}  overshoot={max_right_excursion:.1f}deg")
    assert ok, f"failed: {msg}"
    assert max_right_excursion < 15, f"overshoot {max_right_excursion:.1f}deg"


def test_maneuver_sequence():
    """Commit 360 -> crank L -> pump -> recommit restores commit refs."""
    sim = MockAircraft(hdg_deg=0, alt_m=6000, mach=0.85)
    ap = Autopilot()
    mm = ManeuverManager(ap)
    mm.fly(hdg_rad=0.0, alt_m=6000, mach=0.85)

    seq = {
        20.0: lambda st, m: m.crank(st, "left", 0.0),        # -> 310
        60.0: lambda st, m: m.pump(st, 0.0),                 # -> 180, descend
        110.0: lambda st, m: m.recommit(st),                 # -> back to 360/6000
    }
    run(sim, ap, 200, mm=mm, callbacks=seq)
    assert mm.active == "recommit"
    assert abs(math.degrees(wrap_pi(ap.tgt_hdg - 0.0))) < 0.1, "recommit heading wrong"
    assert abs(ap.tgt_alt - 6000) < 1, "recommit altitude wrong"
    ok, msg = settled(sim, ap, alt_tol=60)
    print(f"  maneuver_sequence: {msg}  final_mode={mm.active}")
    assert ok, f"failed to re-converge after recommit: {msg}"


def test_crank_geometry():
    """Crank left from ref 000 targets 310; crank right targets 050."""
    sim = MockAircraft(hdg_deg=0)
    ap = Autopilot()
    mm = ManeuverManager(ap)
    st = parse_packet(sim.packet(1), State())
    mm.fly(hdg_rad=0.0, alt_m=6000, mach=0.85)
    mm.crank(st, "left", 0.0)
    assert abs(math.degrees(ap.tgt_hdg) - 310) < 0.1
    mm.crank(st, "right", 0.0)
    assert abs(math.degrees(ap.tgt_hdg) - 50) < 0.1
    mm.notch(st, "left", math.radians(0))
    assert abs(math.degrees(ap.tgt_hdg) - 270) < 0.1
    print("  crank/notch geometry: ok")


if __name__ == "__main__":
    test_crank_geometry()
    test_basic_capture()
    test_north_crossing()
    test_maneuver_sequence()
    print("ALL PHASE 2 TESTS PASSED")
