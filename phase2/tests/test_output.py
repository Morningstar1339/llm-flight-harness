"""Output-layer tests — the axis mapping that nearly lost the jet on 7/19.

The vJoy path had no test at all. Two of this project's worst days came
from it: a reversed elevator sign (pitch-up departure) and a Z axis
parked at its low end, which under the inverted throttle mapping is a
hidden FULL-THROTTLE contribution. Both are one-line regressions and
both are now gated here.

No vJoy driver required: pyvjoy is imported inside VJoyOutput.__init__,
so a stub in sys.modules is enough to exercise the real code.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon import output as out_mod
from daemon.output import (
    AIL_SIGN,
    ELEV_SIGN,
    VJOY_MAX,
    VJOY_MIN,
    MockOutput,
    _to_axis,
    make_output,
)

X, Y, Z = 0x30, 0x31, 0x32
MID = _to_axis(0.0)


# ------------------------------------------------------------ fake device --
class FakeVJoyDevice:
    def __init__(self, device_id=1):
        self.device_id = device_id
        self.axes = {}
        self.buttons = {}

    def set_axis(self, axis, value):
        self.axes[axis] = value

    def set_button(self, n, value):
        self.buttons[n] = value


@pytest.fixture
def vjoy(monkeypatch):
    """Install a stub pyvjoy and hand back a constructed VJoyOutput."""
    import types
    mod = types.ModuleType("pyvjoy")
    mod.VJoyDevice = FakeVJoyDevice
    monkeypatch.setitem(sys.modules, "pyvjoy", mod)
    return out_mod.VJoyOutput()


# ------------------------------------------------------------ axis mapping --
def test_axis_endpoints_and_midpoint():
    assert _to_axis(-1.0) == VJOY_MIN
    assert _to_axis(1.0) == VJOY_MAX
    assert abs(MID - (VJOY_MIN + VJOY_MAX) // 2) <= 1


def test_axis_clamps_out_of_range_input():
    assert _to_axis(-5.0) == VJOY_MIN
    assert _to_axis(5.0) == VJOY_MAX


def test_axis_is_monotonic():
    vals = [_to_axis(v / 10.0) for v in range(-10, 11)]
    assert vals == sorted(vals)


def test_axis_output_is_always_a_valid_vjoy_int():
    for v in (-99.0, -1.0, -0.3, 0.0, 0.3, 1.0, 99.0, 0.0001):
        a = _to_axis(v)
        assert isinstance(a, int)
        assert VJOY_MIN <= a <= VJOY_MAX


# ------------------------------------------------------------- 7/19 bugs ---
def test_center_parks_the_throttle_axis_at_neutral_not_low(vjoy):
    """THE 7/19 bug. Z low means FULL THROTTLE under the inverted mapping.

    Under DCS device summing, mid = adds nothing to the pilot's inputs.
    Anything else on Z is a silent throttle command.
    """
    vjoy.j.set_axis(Z, VJOY_MIN)          # simulate the old parked-low state
    vjoy.center()
    assert vjoy.j.axes[Z] == MID
    assert vjoy.j.axes[Z] != VJOY_MIN


def test_center_neutralises_all_three_axes(vjoy):
    vjoy.send(0.9, -0.7, 0.9)
    vjoy.center()
    assert vjoy.j.axes[X] == vjoy.j.axes[Y] == vjoy.j.axes[Z] == MID


def test_throttle_mapping_is_inverted_and_half_is_neutral(vjoy):
    """Z = 1 - 2*throttle: 0.5 throttle must land exactly on centre, or
    `center()` and `send(..., 0.5)` would disagree about neutral."""
    vjoy.send(0.0, 0.0, 0.5)
    assert vjoy.j.axes[Z] == MID
    vjoy.send(0.0, 0.0, 0.0)
    assert vjoy.j.axes[Z] == VJOY_MAX, "zero throttle is the HIGH axis end"
    vjoy.send(0.0, 0.0, 1.0)
    assert vjoy.j.axes[Z] == VJOY_MIN


def test_elevator_pull_is_positive_through_the_sign_constant(vjoy):
    """+elevator means PULL. If this flips, the jet dives on a climb command
    (or departs on engage, as it did on 7/19)."""
    vjoy.send(0.0, 1.0, 0.5)
    up = vjoy.j.axes[Y]
    vjoy.send(0.0, -1.0, 0.5)
    down = vjoy.j.axes[Y]
    assert up == _to_axis(ELEV_SIGN * 1.0)
    assert down == _to_axis(ELEV_SIGN * -1.0)
    assert up != down


def test_aileron_right_is_positive_through_the_sign_constant(vjoy):
    vjoy.send(1.0, 0.0, 0.5)
    assert vjoy.j.axes[X] == _to_axis(AIL_SIGN * 1.0)


def test_sign_constants_are_the_documented_values():
    """A bare-faced tripwire: flipping a sign is a flight-test decision,
    not a refactor. If this fails, someone changed it — confirm in the jet."""
    assert (ELEV_SIGN, AIL_SIGN) == (1.0, 1.0)


def test_close_centers_the_device(vjoy):
    vjoy.send(1.0, 1.0, 1.0)
    vjoy.close()
    assert vjoy.j.axes[Z] == MID


def test_send_records_throttle_for_the_bench_test(vjoy):
    """bench_test() reads out.throttle to hold it while sweeping a stick axis."""
    vjoy.send(0.0, 0.0, 0.7)
    assert vjoy.throttle == 0.7


def test_buttons_are_one_based_passthrough(vjoy):
    vjoy.set_button(6, True)
    vjoy.set_button(6, False)
    assert vjoy.j.buttons[6] == 0


def test_constructor_centers_before_anything_else(vjoy):
    """A daemon that came up with a stale axis state would command it."""
    assert vjoy.j.axes[X] == vjoy.j.axes[Y] == vjoy.j.axes[Z] == MID


# ------------------------------------------------------------ mock output --
def test_mock_output_records_and_centers():
    m = MockOutput()
    m.send(0.3, -0.4, 0.8)
    assert (m.aileron, m.elevator, m.throttle) == (0.3, -0.4, 0.8)
    m.center()
    assert (m.aileron, m.elevator) == (0.0, 0.0)
    assert m.throttle == 0.8, "mock centre holds throttle for the sim model"


def test_mock_output_logs_button_edges():
    m = MockOutput()
    m.set_button(3, True)
    m.set_button(3, False)
    assert m.button_log == [(3, True), (3, False)]


def test_make_output_mock_needs_no_driver():
    assert isinstance(make_output(True), MockOutput)
