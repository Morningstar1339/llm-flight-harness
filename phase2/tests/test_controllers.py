"""Controller tests — the PID subtleties that were flight-diagnosed.

The convergence tests prove the cascade *converges*. They do not prove
*why* it converges, so every one of the fixes below could be reverted
without turning a test red:

  7/20  a hard integrator freeze let the jet sit above target supersonic
        with I pinned at 0; an unconditional leak wound up on every
        capture. The conditional-integration-with-persistence-escape is
        what separates the two, and it is pure timing logic.
  7/20  raw measured-AoA feedforward is POSITIVE FEEDBACK in the pitch
        target. Only the slow-filtered component may feed forward.
  7/20  no altitude target used to mean "hold VV 0", which the ACS
        auto-trim quietly absorbed - a bare `auto` engage drifted at
        +5,700 fpm. Latching the current altitude is the fix.
  7/20  no mach target used to mean open-loop throttle 0.5, and the jet
        accelerated through M1.0 unsupervised.

Each of those is now a test that fails if the fix is removed.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon.controllers import DEFAULT_GAINS, PID, Autopilot, wrap_pi
from daemon.telemetry import State

DT = 0.05


def state(**kw):
    base = dict(hdg=0.0, pitch=0.0, bank=0.0, alt=6000.0, vv=0.0,
                ias=200.0, tas=300.0, mach=0.85, g=1.0, aoa=math.radians(6))
    base.update(kw)
    return State(**base)


# ================================================================= wrap_pi
@pytest.mark.parametrize("deg,expect", [
    (0, 0), (10, 10), (-10, -10), (190, -170), (-190, 170), (360, 0), (540, -180),
])
def test_wrap_pi_takes_the_short_way(deg, expect):
    assert abs(math.degrees(wrap_pi(math.radians(deg))) - expect) < 1e-6


def test_wrap_pi_range():
    for deg in range(-720, 721, 7):
        assert -math.pi - 1e-9 <= wrap_pi(math.radians(deg)) <= math.pi + 1e-9


# ===================================================================== PID
def test_proportional_only():
    p = PID(kp=2.0)
    assert abs(p.update(0.25, DT) - 0.5) < 1e-9


def test_output_is_clamped():
    p = PID(kp=100.0)
    assert p.update(1.0, DT) == 1.0
    assert p.update(-1.0, DT) == -1.0
    p2 = PID(kp=100.0, out_lo=0.0, out_hi=1.0)
    assert p2.update(-1.0, DT) == 0.0


def test_zero_dt_does_not_divide_by_zero():
    p = PID(kp=1.0, kd=1.0)
    p.update(0.1, DT)
    assert isinstance(p.update(0.2, 0.0), float)
    assert isinstance(p.update(0.2, -1.0), float)


def test_integrator_accumulates_and_is_clamped_by_authority():
    """Anti-windup bounds the integrator's OUTPUT authority, not raw state."""
    p = PID(ki=1.0, i_lo=-0.5, i_hi=0.5)
    for _ in range(1000):
        p.update(1.0, DT)
    assert abs(p.i - 0.5) < 1e-9


def test_conditional_integration_freezes_on_a_brief_excursion():
    """A capture transient is out-of-band for seconds. It must not wind."""
    p = PID(ki=1.0, i_band=0.1, i_delay=4.0)
    for _ in range(int(2.0 / DT)):          # 2 s out of band — under i_delay
        p.update(5.0, DT)
    assert p.i == 0.0, "brief excursions must not accumulate authority"


def test_persistence_escape_lets_a_real_deficit_accumulate():
    """A trim deficit is out-of-band for minutes. It must eventually leak."""
    p = PID(ki=1.0, i_band=0.1, i_delay=4.0, i_leak=0.25, i_hi=10.0, i_lo=-10.0)
    for _ in range(int(3.9 / DT)):
        p.update(5.0, DT)
    assert p.i == 0.0
    for _ in range(int(2.0 / DT)):          # now past i_delay
        p.update(5.0, DT)
    assert p.i > 0.0, "a persistent same-sign deficit must escape the freeze"


def test_leak_rate_is_slower_than_in_band_integration():
    fast = PID(ki=1.0, i_band=10.0, i_hi=99.0)                  # always in band
    slow = PID(ki=1.0, i_band=0.1, i_delay=0.0, i_leak=0.25, i_hi=99.0)
    for _ in range(int(1.0 / DT)):
        fast.update(5.0, DT)
        slow.update(5.0, DT)
    assert 0 < slow.i < fast.i
    assert abs(slow.i / fast.i - 0.25) < 1e-6


def test_sign_flip_resets_the_persistence_timer():
    """Oscillating around the band is not a persistent deficit."""
    p = PID(ki=1.0, i_band=0.1, i_delay=4.0, i_leak=0.25, i_hi=99.0, i_lo=-99.0)
    for _ in range(int(20.0 / DT)):
        p.update(5.0 if (int(_ * DT) % 2 == 0) else -5.0, DT)
    assert abs(p.i) < 1e-9, "alternating excursions must never escape the freeze"


def test_in_band_error_integrates_normally():
    p = PID(ki=1.0, i_band=1.0, i_hi=99.0)
    for _ in range(int(1.0 / DT)):
        p.update(0.5, DT)
    assert abs(p.i - 0.5) < 1e-6


def test_derivative_on_measurement_has_no_setpoint_kick():
    """The whole point: a step in the TARGET must not spike the derivative."""
    # wide output limits, or the clamp hides the very spike we're measuring
    kick = PID(kd=1.0, out_lo=-100, out_hi=100)    # derivative on error (legacy)
    quiet = PID(kd=1.0, out_lo=-100, out_hi=100)   # derivative on measurement
    meas = 0.0
    kick.update(0.0, DT)
    quiet.update(0.0, DT, meas=meas)
    # target jumps by 1.0; measurement has not moved yet
    kicked = kick.update(1.0, DT)
    calm = quiet.update(1.0, DT, meas=meas)
    assert abs(kicked) > 1.0, "error-derivative spikes on a setpoint step"
    assert abs(calm) < 1e-9, "measurement-derivative must not"


def test_derivative_on_measurement_still_damps_real_motion():
    p = PID(kd=1.0)
    p.update(0.0, DT, meas=0.0)
    out = p.update(0.0, DT, meas=0.1)      # measurement rising, target still
    assert out < 0, "kd must oppose the measurement's motion"


def test_derivative_filter_attenuates_a_noise_spike():
    raw = PID(kd=1.0, d_tau=0.0, out_lo=-100, out_hi=100)
    filt = PID(kd=1.0, d_tau=0.5, out_lo=-100, out_hi=100)
    for p in (raw, filt):
        p.update(0.0, DT, meas=0.0)
    assert abs(filt.update(0.0, DT, meas=1.0)) < abs(raw.update(0.0, DT, meas=1.0))


def test_reset_clears_every_piece_of_state():
    p = PID(kp=1.0, ki=1.0, kd=1.0, d_tau=0.2, i_band=0.1, i_delay=0.0)
    for _ in range(20):
        p.update(5.0, DT, meas=1.0)
    p.reset()
    assert (p.i, p.prev_err, p.prev_meas, p.d_f) == (0.0, None, None, 0.0)
    assert p._oob_t == 0.0 and p._oob_sign == 0


# =============================================================== Autopilot
def test_defaults_load_without_a_gains_file():
    ap = Autopilot()
    assert ap.g == DEFAULT_GAINS
    assert (ap.tgt_hdg, ap.tgt_alt, ap.tgt_mach) == (None, None, None)


def test_gains_file_overrides_defaults(tmp_path):
    path = tmp_path / "gains.json"
    path.write_text(json.dumps({"bank_kp": 9.9}))
    ap = Autopilot(str(path))
    assert ap.g["bank_kp"] == 9.9
    assert ap.g["hdg_kp"] == DEFAULT_GAINS["hdg_kp"], "others keep their defaults"


def test_set_gain_rejects_unknown_names():
    ap = Autopilot()
    with pytest.raises(KeyError):
        ap.set_gain("bank_kx", 1.0)


def test_set_gain_rebuilds_the_controllers():
    """Live tuning is useless if the PID keeps the old constant."""
    ap = Autopilot()
    ap.set_gain("bank_kp", 4.2)
    assert ap.pid_bank.kp == 4.2


def test_save_gains_round_trips(tmp_path):
    path = tmp_path / "gains.json"
    ap = Autopilot(str(path))
    ap.set_gain("vv_cap", 33.0)
    ap.save_gains()
    assert Autopilot(str(path)).g["vv_cap"] == 33.0


def test_save_gains_without_a_path_is_a_noop():
    Autopilot().save_gains()


def test_no_altitude_target_latches_current_altitude():
    """7/20: 'hold VV 0' was trimmed away and the jet climbed at +5,700 fpm."""
    ap = Autopilot()
    assert ap.tgt_alt is None
    ap.update(state(alt=7123.0), DT)
    assert ap.tgt_alt == 7123.0


def test_no_mach_target_latches_current_mach():
    """7/20: open-loop throttle 0.5 let it accelerate through M1.0."""
    ap = Autopilot()
    ap.update(state(mach=0.83), DT)
    assert ap.tgt_mach == 0.83


def test_latched_altitude_is_not_re_latched_every_tick():
    ap = Autopilot()
    ap.update(state(alt=6000.0), DT)
    ap.update(state(alt=6200.0), DT)
    assert ap.tgt_alt == 6000.0, "it holds the latch, it does not chase"


def test_aoa_feedforward_is_slow_not_raw():
    """7/20 divergence: raw AoA in the pitch target is positive feedback."""
    ap = Autopilot()
    s = state(aoa=math.radians(12))
    ap.update(s, DT)
    first = ap._aoa_f
    ap.update(state(aoa=math.radians(0)), DT)
    moved = abs(ap._aoa_f - first)
    assert moved < math.radians(12) * 0.05, \
        "the AoA feedforward must lag hard; raw passthrough is divergent"


def test_aoa_filter_converges_over_its_time_constant():
    ap = Autopilot()
    ap.update(state(aoa=0.0), DT)
    for _ in range(int(60.0 / DT)):
        ap.update(state(aoa=math.radians(10)), DT)
    assert abs(math.degrees(ap._aoa_f) - 10) < 1.0


def test_bank_target_is_capped():
    ap = Autopilot()
    ap.tgt_hdg = math.radians(179)
    ap.update(state(hdg=0.0), DT)
    ap.update(state(hdg=0.0), DT)
    # a 179 deg error times hdg_kp would be enormous without the cap
    assert abs(ap.pid_bank.prev_err) <= math.radians(DEFAULT_GAINS["bank_cap_deg"]) + 1e-9


def test_heading_error_takes_the_short_way_round():
    ap = Autopilot()
    ap.tgt_hdg = math.radians(10)
    a, _, _ = ap.update(state(hdg=math.radians(350)), DT)
    assert a > 0, "350 -> 010 is a RIGHT turn"


def test_turn_compensation_adds_pull_with_bank():
    ap_level = Autopilot()
    ap_bank = Autopilot()
    _, e_level, _ = ap_level.update(state(bank=0.0), DT)
    _, e_bank, _ = ap_bank.update(state(bank=math.radians(60)), DT)
    assert e_bank > e_level, "it must pull harder in a turn or it sinks"


def test_vv_target_is_capped_by_vv_cap():
    ap = Autopilot()
    ap.tgt_alt = 60000.0                       # absurd climb demand
    ap.update(state(alt=1000.0), DT)
    assert abs(ap.dbg_tgt_vv) <= DEFAULT_GAINS["vv_cap"] + 1e-9


def test_target_pitch_is_capped():
    ap = Autopilot()
    ap.tgt_alt = 60000.0
    ap.update(state(alt=1000.0, tas=90.0), DT)
    assert abs(ap.dbg_tgt_pitch) <= math.radians(DEFAULT_GAINS["pitch_cap_deg"]) + 1e-9


def test_zero_tas_does_not_divide_by_zero():
    ap = Autopilot()
    ap.tgt_alt = 9000.0
    a, e, t = ap.update(state(tas=0.0, alt=6000.0), DT)
    assert all(math.isfinite(v) for v in (a, e, t))


def test_outputs_are_always_in_range():
    ap = Autopilot()
    ap.tgt_hdg, ap.tgt_alt, ap.tgt_mach = math.radians(180), 12000.0, 2.0
    for bank in (-80, -30, 0, 30, 80):
        a, e, t = ap.update(state(bank=math.radians(bank), alt=1000.0, mach=0.4), DT)
        assert -1.0 <= a <= 1.0 and -1.0 <= e <= 1.0 and 0.0 <= t <= 1.0


def test_reset_clears_the_aoa_filter_and_the_pids():
    ap = Autopilot()
    for _ in range(20):
        ap.update(state(aoa=math.radians(10)), DT)
    ap.reset()
    assert ap._aoa_f is None
    assert ap.pid_pitch.i == 0.0 and ap.pid_mach.i == 0.0


def test_reset_does_not_drop_the_setpoints():
    """`auto` calls reset(). Losing the targets there would be a surprise."""
    ap = Autopilot()
    ap.tgt_hdg, ap.tgt_alt, ap.tgt_mach = 1.0, 6000.0, 0.9
    ap.reset()
    assert (ap.tgt_hdg, ap.tgt_alt, ap.tgt_mach) == (1.0, 6000.0, 0.9)


def test_throttle_integrator_cannot_command_negative():
    ap = Autopilot()
    ap.tgt_mach = 0.5
    for _ in range(200):
        _, _, t = ap.update(state(mach=1.2), DT)
    assert t == 0.0, "mach PID is clamped to [0, 1]"
