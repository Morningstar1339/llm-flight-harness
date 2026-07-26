"""Maneuver tests — the commit-reference semantics recommit depends on.

test_convergence.py proves a crank -> pump -> recommit cycle re-converges.
It does not pin down *what* recommit returns to, and that is the subtle
part: the commit reference is captured ONCE, on the first maneuver after
a `fly`. If it were re-captured on every maneuver, recommit after a
crank-then-pump would return to the pump heading — cold, low and fast,
away from the fight — instead of back to the commit.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon.controllers import Autopilot
from daemon.maneuvers import ManeuverManager
from daemon.telemetry import State

FT = 0.3048


@pytest.fixture
def mm():
    return ManeuverManager(Autopilot())


def st(hdg_deg=0.0, alt=6000.0, mach=0.85):
    return State(hdg=math.radians(hdg_deg), alt=alt, mach=mach, tas=300.0)


def hdg_deg(mm):
    return math.degrees(mm.ap.tgt_hdg) % 360


# ============================================================ commit memory
def test_fly_sets_the_commit_reference(mm):
    mm.fly(hdg_rad=math.radians(90), alt_m=6000, mach=0.85)
    assert abs(math.degrees(mm.commit_hdg) - 90) < 1e-9
    assert mm.commit_alt == 6000 and mm.commit_mach == 0.85


def test_commit_is_captured_once_not_on_every_maneuver(mm):
    """crank -> pump -> recommit must return to the COMMIT, not the pump."""
    mm.fly(hdg_rad=0.0, alt_m=6000, mach=0.85)
    mm.crank(st(), "left", 0.0)
    assert hdg_deg(mm) == pytest.approx(310)
    mm.pump(st(), 0.0)
    assert hdg_deg(mm) == pytest.approx(180)
    assert mm.recommit(st())
    assert hdg_deg(mm) == pytest.approx(0)
    assert mm.ap.tgt_alt == 6000 and mm.ap.tgt_mach == 0.85


def test_a_new_fly_moves_the_commit_reference(mm):
    mm.fly(hdg_rad=0.0, alt_m=6000, mach=0.85)
    mm.crank(st(), "left", 0.0)
    mm.fly(hdg_rad=math.radians(120), alt_m=7000, mach=0.9)
    mm.pump(st(), math.radians(120))
    mm.recommit(st())
    assert hdg_deg(mm) == pytest.approx(120)
    assert mm.ap.tgt_alt == 7000


def test_first_maneuver_without_a_fly_captures_live_state(mm):
    """Engaging straight into a crank still gives recommit something real."""
    mm.crank(st(hdg_deg=45.0, alt=7500.0), "right", math.radians(45))
    assert abs(math.degrees(mm.commit_hdg) - 45) < 1e-6
    assert mm.commit_alt == 7500.0
    assert mm.commit_mach >= 0.8, "commit mach floors at 0.8, not cruise-slow"


def test_recommit_without_a_reference_is_refused(mm):
    assert mm.recommit(st()) is False
    assert mm.active == "none"


# ================================================================ geometry
@pytest.mark.parametrize("direction,ref,offset,expect", [
    ("left", 0, 50, 310), ("right", 0, 50, 50),
    ("left", 350, 50, 300), ("right", 350, 50, 40),
    ("l", 90, 30, 60), ("r", 90, 30, 120),
])
def test_crank_geometry(mm, direction, ref, offset, expect):
    mm.crank(st(), direction, math.radians(ref), offset)
    assert hdg_deg(mm) == pytest.approx(expect, abs=1e-6)


@pytest.mark.parametrize("direction,threat,expect", [
    ("left", 0, 270), ("right", 0, 90), ("left", 45, 315), ("right", 300, 30),
])
def test_notch_puts_the_threat_abeam(mm, direction, threat, expect):
    mm.notch(st(), direction, math.radians(threat))
    assert hdg_deg(mm) == pytest.approx(expect, abs=1e-6)


def test_targets_stay_normalised_to_one_turn(mm):
    mm.crank(st(), "right", math.radians(350), 50.0)
    assert 0 <= mm.ap.tgt_hdg < 2 * math.pi


# ==================================================================== pump
def test_pump_turns_cold_descends_and_runs_fast(mm):
    mm.fly(hdg_rad=math.radians(90), alt_m=8000, mach=0.8)
    mm.pump(st(hdg_deg=90), math.radians(90))
    assert hdg_deg(mm) == pytest.approx(270)
    assert mm.ap.tgt_alt == 6000
    assert mm.ap.tgt_mach == pytest.approx(0.95)


def test_pump_respects_the_altitude_floor(mm):
    """2000 m below 2500 m is not 500 m."""
    mm.fly(hdg_rad=0.0, alt_m=2500, mach=0.85)
    mm.pump(st(alt=2500), 0.0)
    assert mm.ap.tgt_alt == 1500


def test_pump_never_slows_the_jet_down(mm):
    mm.fly(hdg_rad=0.0, alt_m=8000, mach=1.2)
    mm.pump(st(), 0.0)
    assert mm.ap.tgt_mach == 1.2


def test_pump_without_a_bearing_uses_the_commit_heading(mm):
    mm.fly(hdg_rad=math.radians(30), alt_m=8000, mach=0.85)
    mm.pump(st(hdg_deg=30))
    assert hdg_deg(mm) == pytest.approx(210)


# ================================================================ describe
def test_describe_reports_mode_targets_and_error(mm):
    mm.fly(hdg_rad=math.radians(90), alt_m=6000, mach=0.9)
    text = mm.describe(st(hdg_deg=80))
    assert "mode=fly" in text and "tgt hdg 090" in text
    assert "alt 6000m" in text and "mach 0.90" in text
    assert "hdg_err +10.0" in text


def test_describe_survives_unset_targets(mm):
    text = mm.describe(st())
    assert "tgt hdg ---" in text and "alt ---" in text and "mach ---" in text


def test_describe_shows_the_commit_only_during_a_maneuver(mm):
    mm.fly(hdg_rad=0.0, alt_m=6000, mach=0.85)
    assert "commit" not in mm.describe(st())
    mm.crank(st(), "left", 0.0)
    assert "(commit 000)" in mm.describe(st())


def test_active_mode_tracks_the_last_maneuver(mm):
    mm.fly(hdg_rad=0.0)
    assert mm.active == "fly"
    mm.crank(st(), "left", 0.0)
    assert mm.active == "crank_l"
    mm.notch(st(), "right", 0.0)
    assert mm.active == "notch_r"
    mm.pump(st(), 0.0)
    assert mm.active == "pump"
    mm.recommit(st())
    assert mm.active == "recommit"


def test_fly_accepts_partial_targets(mm):
    mm.fly(hdg_rad=math.radians(45))
    assert mm.ap.tgt_alt is None and mm.ap.tgt_mach is None
    mm.fly(mach=0.95)
    assert hdg_deg(mm) == pytest.approx(45), "an unset field must not clear"
