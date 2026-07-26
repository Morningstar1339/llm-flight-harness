"""Lock-manager tests — the 7/21 rebuild, made regression-proof.

Three behaviours here were learned the hard way in flight and none of
them was pinned by a test:

  * Aim comes from GEOMETRY, not the exported az/el. DCS ships 0.0/0.0
    for FC3, and v1 dutifully "slewed" 0 degrees and pressed lock at the
    designator reset position four times in a row.
  * Verification is defence in depth: an s.locked id match is gold, a
    LaunchAuthorized rising edge is silver (lock confirmed, id unknown),
    anything else is failure.
  * v1 opened EVERY attempt with `unlock` to reset the designator, which
    destroyed any lock the verifier merely could not see. A pre-existing
    lock must now short-circuit the hunt BEFORE anything is reset.

The hunt is driven synchronously here (_run rather than lock_contact) so
the tests are deterministic.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon.locker import LockManager
from daemon.telemetry import State

OWNPOS = {"pp": {"x": 0.0, "y": 0.0, "z": 0.0}}


def contact_at(cid, north, east, up=0.0):
    dist = math.sqrt(north ** 2 + east ** 2 + up ** 2)
    return {"id": cid, "px": north, "py": up, "pz": east, "dist_m": dist}


class Rig:
    """Stands in for both the systems executor and the telemetry listener."""

    def __init__(self, state, on_lock=None):
        self.state = state
        self.events = []
        self.on_lock = on_lock

    # --- systems executor surface ---
    def execute(self, name):
        self.events.append(("execute", name))
        if name == "lock" and self.on_lock:
            self.on_lock(self.state)

    def hold(self, name):
        self.events.append(("hold", name))

    def release(self, name):
        self.events.append(("release", name))

    def release_all_slew(self):
        self.events.append(("release_all_slew", None))

    # --- telemetry surface ---
    def latest(self):
        return self.state

    # --- helpers ---
    def executed(self, name):
        return ("execute", name) in self.events

    def held(self):
        return [n for kind, n in self.events if kind == "hold"]


def make(rig, **gains):
    g = {"slew_rate_dps": 1000.0, "slew_settle_s": 0.0,
         "lock_verify_s": 0.0, "lock_max_attempts": 1}
    g.update(gains)
    return LockManager(rig, rig, g)


# ================================================================== gains
def test_calibration_gains_get_defaults():
    g = {}
    LockManager(Rig(State()), Rig(State()), g)
    for k in ("slew_rate_dps", "slew_settle_s", "lock_verify_s",
              "lock_max_attempts"):
        assert k in g, "must land in the shared gains dict for set/savegains"


def test_existing_gains_are_not_overwritten():
    g = {"slew_rate_dps": 7.5}
    LockManager(Rig(State()), Rig(State()), g)
    assert g["slew_rate_dps"] == 7.5


# ==================================================================== aim
def test_aim_prefers_geometry_over_the_dead_exported_fields():
    """az_rad/el_rad arrive 0.0 for FC3. Geometry must win."""
    c = contact_at(3, north=0.0, east=10000.0)
    c["az_rad"] = 0.0
    c["el_rad"] = 0.0
    lm = make(Rig(State(hdg=0.0, ownpos=OWNPOS, contacts=[c])))
    az, el, src = lm._aim(c)
    assert src == "pp"
    assert abs(az - 90) < 1e-6, "geometry says the contact is on the beam"


def test_aim_falls_back_to_exported_fields_only_when_nonzero():
    c = {"id": 3, "az_rad": math.radians(20), "el_rad": math.radians(-5)}
    lm = make(Rig(State(hdg=0.0, contacts=[c])))
    az, el, src = lm._aim(c)
    assert src == "export"
    assert abs(az - 20) < 1e-6 and abs(el + 5) < 1e-6


def test_aim_returns_none_when_both_sources_are_dead():
    """The exact 7/21 failure: no geometry, and 0.0/0.0 exported."""
    c = {"id": 3, "az_rad": 0.0, "el_rad": 0.0}
    assert make(Rig(State(hdg=0.0, contacts=[c])))._aim(c) is None


# =================================================================== slew
def test_slew_holds_the_positive_direction_for_a_positive_angle():
    rig = Rig(State())
    lm = make(rig)
    lm._slew_axis("slew_right", "slew_left", 30.0, 1.0)
    assert rig.held() == ["slew_right"]
    assert ("release", "slew_right") in rig.events


def test_slew_holds_the_negative_direction_for_a_negative_angle():
    rig = Rig(State())
    make(rig)._slew_axis("slew_right", "slew_left", -30.0, 1.0)
    assert rig.held() == ["slew_left"]


def test_tiny_angles_are_not_slewed():
    """Below half a degree the designator move is noise, not aim."""
    rig = Rig(State())
    make(rig)._slew_axis("slew_up", "slew_down", 0.2, 1.0)
    assert rig.events == []


def test_abort_breaks_a_slew_and_still_releases():
    rig = Rig(State())
    lm = make(rig, slew_rate_dps=0.5)      # would otherwise hold for seconds
    lm._abort.set()
    lm._slew_axis("slew_right", "slew_left", 90.0, 1.0)
    assert ("release", "slew_right") in rig.events


# ================================================================== hunts
def test_a_preexisting_lock_short_circuits_before_any_reset():
    """v1 stomped locks it could not see. This is that fix."""
    rig = Rig(State(locked=[{"id": 3}], contacts=[contact_at(3, 10000, 0)],
                    ownpos=OWNPOS))
    lm = make(rig)
    lm._run(3)
    assert "pre-existing" in lm.status
    assert not rig.executed("unlock"), "must not reset a lock we already have"
    assert not rig.executed("lock")


def test_missing_contact_is_reported():
    rig = Rig(State(contacts=[], ownpos=OWNPOS))
    lm = make(rig)
    lm._run(3)
    assert "not in radar picture" in lm.status
    assert not rig.executed("unlock")


def test_no_aim_data_names_the_likely_cause():
    rig = Rig(State(contacts=[{"id": 3, "az_rad": 0.0, "el_rad": 0.0}]))
    lm = make(rig)
    lm._run(3)
    assert "no aim data" in lm.status
    assert "Export.lua" in lm.status, "point the operator at the likely cause"
    assert not rig.executed("lock"), "never press lock with no aim"


def test_gold_verification_locked_id_matches():
    def on_lock(state):
        state.locked = [{"id": 3}]

    rig = Rig(State(hdg=0.0, ownpos=OWNPOS,
                    contacts=[contact_at(3, 10000, 3000)]), on_lock=on_lock)
    lm = make(rig)
    lm._run(3)
    assert lm.status.startswith("LOCKED 3")
    assert rig.executed("unlock") and rig.executed("lock")
    assert "slew_right" in rig.held(), "it aimed before it pressed"


def test_silver_verification_launch_authorized_rising_edge():
    """s.locked has never once been observed populated in flight; the
    LaunchAuthorized edge is the only confirmation we actually get."""
    def on_lock(state):
        state.sight = {"LaunchAuthorized": True}

    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, sight={"LaunchAuthorized": False},
                    contacts=[contact_at(3, 10000, 0)]), on_lock=on_lock)
    lm = make(rig)
    lm._run(3)
    assert "LOCKED (id unverified" in lm.status


def test_launch_authorized_already_true_is_not_a_rising_edge():
    """Otherwise a lock the previous engagement left behind reads as ours."""
    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, sight={"LaunchAuthorized": True},
                    contacts=[contact_at(3, 10000, 0)]))
    lm = make(rig)
    lm._run(3)
    assert "FAILED" in lm.status


def test_failure_after_the_attempt_budget():
    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, contacts=[contact_at(3, 10000, 0)]))
    lm = make(rig, lock_max_attempts=2)
    lm._run(3)
    assert "FAILED to lock 3 after 2 attempts" in lm.status
    assert [n for k, n in rig.events if k == "execute"].count("lock") == 2


def test_wrong_target_locked_is_reported_not_claimed():
    def on_lock(state):
        state.locked = [{"id": 9}]

    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, contacts=[contact_at(3, 10000, 0)]),
              on_lock=on_lock)
    lm = make(rig)
    lm._run(3)
    assert "FAILED" in lm.status


def test_retries_widen_the_slew_timing():
    """Each retry brackets calibration error rather than repeating it."""
    holds = []
    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, contacts=[contact_at(3, 10000, 5000)]))
    lm = make(rig, lock_max_attempts=3)
    original = lm._slew_axis
    lm._slew_axis = lambda p, n, a, scale: holds.append(scale) or original(p, n, a, scale)
    lm._run(3)
    scales = sorted(set(holds))
    assert scales == [1.0, 1.25, 1.5]


def test_slews_are_always_released_even_on_an_early_return():
    rig = Rig(State(contacts=[]))
    lm = make(rig)
    lm._run(3)
    assert rig.events[-1] == ("release_all_slew", None)


def test_slews_are_released_when_the_hunt_raises():
    rig = Rig(State(hdg=0.0, ownpos=OWNPOS, contacts=[contact_at(3, 10000, 0)]))
    lm = make(rig)
    lm._find = lambda tid: (_ for _ in ()).throw(RuntimeError("telemetry gone"))
    with pytest.raises(RuntimeError):
        lm._run(3)
    assert ("release_all_slew", None) in rig.events


# ============================================================== public api
def test_abort_releases_and_marks_status():
    rig = Rig(State())
    lm = make(rig)
    lm.abort()
    assert lm.status == "aborted"
    assert ("release_all_slew", None) in rig.events
    assert lm._abort.is_set()


def test_a_second_hunt_is_refused_while_one_is_running():
    lm = make(Rig(State()))

    class Alive:
        def is_alive(self):
            return True

    lm._thread = Alive()
    assert lm.lock_contact(3) is False
    assert "busy" in lm.status


def test_lock_contact_starts_a_hunt_and_returns_immediately():
    rig = Rig(State(contacts=[]))
    lm = make(rig)
    assert lm.lock_contact(3) is True
    lm._thread.join(timeout=2.0)
    assert "not in radar picture" in lm.status
