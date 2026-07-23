"""Cascaded autopilot.

Roll channel:   heading error -> target bank (capped) -> PD on bank -> aileron
Pitch channel:  altitude error -> target VV (capped) -> target PITCH attitude
                (asin(vv/TAS) + AoA) -> PID on pitch -> elevator,
                plus bank compensation (pull harder in turns)
Throttle:       mach error -> throttle, integrator-dominant. No mach target
                => latch and hold current mach (never open-loop).

Both fast loops are ATTITUDE loops. This is deliberate: on the real jet,
stick commands pitch rate, so elevator-to-VV behaves like an integrator
through lags — a PID wrapped directly around VV limit-cycles at any
airspeed (flight-tested 7/20: bank attitude PD held +-0.1 deg while the
direct-VV PID porpoised continuously). Do not "simplify" pitch back to a
direct VV loop; it will porpoise again.

PID extras (used by the pitch loop): filtered derivative-on-measurement
(no setpoint kick, kd damps instead of amplifying telemetry noise) and
conditional integration with a persistence escape (brief transients don't
wind the integrator; persistent deficits still accumulate authority).

All gains live in gains.json, tuned live from the REPL, persisted with
savegains.
"""
from __future__ import annotations

import json
import math
import os


def wrap_pi(a: float) -> float:
    """Wrap angle to [-pi, pi] — shortest-way heading errors."""
    return (a + math.pi) % (2 * math.pi) - math.pi


class PID:
    """PID with optional filtered derivative-on-measurement and
    conditional integration.

    d_tau:  derivative low-pass time constant, seconds. 0 = unfiltered
            (legacy behavior).
    i_band: only integrate when |err| <= i_band. None = always integrate
            (legacy behavior).
    update(err, dt, meas=None): if meas is provided, the derivative term
            uses -d(meas)/dt (measurement derivative); otherwise d(err)/dt
            as before.
    """

    def __init__(self, kp=0.0, ki=0.0, kd=0.0, out_lo=-1.0, out_hi=1.0,
                 i_lo=None, i_hi=None, d_tau=0.0, i_band=None,
                 i_leak=0.25, i_delay=4.0):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_lo, self.out_hi = out_lo, out_hi
        self.i_lo = i_lo if i_lo is not None else out_lo
        self.i_hi = i_hi if i_hi is not None else out_hi
        self.d_tau = d_tau
        self.i_band = i_band
        self.i_leak = i_leak
        self.i_delay = i_delay
        self._oob_t = 0.0
        self._oob_sign = 0
        self.i = 0.0              # integral *contribution* (ki pre-multiplied)
        self.prev_err = None
        self.prev_meas = None
        self.d_f = 0.0            # filtered derivative state

    def reset(self):
        self.i = 0.0
        self.prev_err = None
        self.prev_meas = None
        self.d_f = 0.0
        self._oob_t = 0.0
        self._oob_sign = 0

    def update(self, err: float, dt: float, meas: float | None = None) -> float:
        if dt <= 0:
            dt = 1e-3

        # ---- derivative ----
        d_raw = 0.0
        if self.kd:
            if meas is not None:
                if self.prev_meas is not None:
                    # error = tgt - meas, so d(err)/dt ~ -d(meas)/dt when
                    # the target holds still; using -d(meas) drops the
                    # setpoint-step kick entirely.
                    d_raw = -(meas - self.prev_meas) / dt
            elif self.prev_err is not None:
                d_raw = (err - self.prev_err) / dt
        self.prev_err = err
        self.prev_meas = meas
        if self.d_tau > 0.0:
            self.d_f += (d_raw - self.d_f) * (dt / (self.d_tau + dt))
            d = self.d_f
        else:
            d = d_raw

        # ---- integrator ----
        # anti-windup bounds the integrator's OUTPUT authority, not raw state.
        # Conditional integration with a persistence escape hatch:
        #   inside i_band            -> full ki (steady-state trim as normal)
        #   outside i_band, briefly  -> frozen (capture transients don't wind)
        #   outside i_band, SAME-SIGN error for > i_delay seconds
        #                            -> leak (i_leak * ki): a persistent
        #      deficit the P-term can't cover still accumulates authority.
        # Rationale: a hard freeze flew 7/20 and let the jet drift above
        # target supersonic with I pinned at 0; an unconditional leak wound
        # up on every capture. Captures are out-of-band for seconds; trim
        # deficits for minutes. Time separates them.
        if self.i_band is None or abs(err) <= self.i_band:
            rate = self.ki
            self._oob_t = 0.0
            self._oob_sign = 0
        else:
            sgn = 1 if err > 0 else -1
            self._oob_t = self._oob_t + dt if sgn == self._oob_sign else 0.0
            self._oob_sign = sgn
            rate = self.ki * self.i_leak if self._oob_t >= self.i_delay else 0.0
        self.i = max(self.i_lo, min(self.i_hi, self.i + rate * err * dt))

        out = self.kp * err + self.i + self.kd * d
        return max(self.out_lo, min(self.out_hi, out))


DEFAULT_GAINS = {
    # heading -> bank: deg of bank per deg of heading error, and bank cap
    "hdg_kp": 2.5, "bank_cap_deg": 60.0,
    # bank -> aileron
    "bank_kp": 1.2, "bank_kd": 0.25,
    # alt -> vv: (m/s of VV) per m of alt error, and VV cap
    "alt_kp": 0.25, "vv_cap": 40.0,
    # pitch cascade (mirrors roll: attitude PD is what the jet wants;
    # proven by bank holding +-0.1 deg on 7/20 while direct-VV PID porpoised)
    # target pitch = asin(tgt_vv/TAS) + measured AoA + vv2p_kp * VV error
    "vv2p_kp": 0.004,          # rad of extra pitch per m/s of VV error
    "aoa_tau": 8.0,            # s — low-pass on the AoA feedforward. RAW
                               # AoA in the target is POSITIVE FEEDBACK
                               # (aoa ~ pitch - gamma cancels the attitude
                               # term out of the error) — flight-proven
                               # divergent 7/20. Only the slow trim
                               # component may feed forward.
    "pitch_cap_deg": 30.0,     # target pitch clamp
    "pitch_kp": 1.2, "pitch_kd": 0.35, "pitch_ki": 0.0,
    "pitch_d_tau": 0.15,       # low-pass on pitch derivative (s)
    # turn compensation: extra pull ~ (1/cos(bank) - 1)
    "turn_comp": 0.45,
    # mach -> throttle
    "mach_kp": 3.0, "mach_ki": 0.6,
}


class Autopilot:
    """Holds setpoints; each update() maps state -> control surfaces.

    Outputs: aileron [-1, 1] (+right), elevator [-1, 1] (+pull), throttle [0, 1].
    """

    def __init__(self, gains_path: str | None = None):
        self.gains_path = gains_path
        self.g = dict(DEFAULT_GAINS)
        if gains_path and os.path.exists(gains_path):
            with open(gains_path) as f:
                self.g.update(json.load(f))
        self._aoa_f = None       # slow-filtered AoA feedforward state
        self.tgt_hdg = None      # radians, None = hold wings level
        self.tgt_alt = None      # m
        self.tgt_mach = None
        self._rebuild()

    def _rebuild(self):
        g = self.g
        self.pid_bank = PID(kp=g["bank_kp"], kd=g["bank_kd"])
        self.pid_pitch = PID(kp=g["pitch_kp"], kd=g["pitch_kd"],
                             ki=g["pitch_ki"], i_lo=-0.25, i_hi=0.25,
                             d_tau=g["pitch_d_tau"])
        self.pid_mach = PID(kp=g["mach_kp"], ki=g["mach_ki"],
                            out_lo=0.0, out_hi=1.0, i_lo=0.0, i_hi=1.0)

    def set_gain(self, name: str, value: float):
        if name not in self.g:
            raise KeyError(f"unknown gain {name!r}; have {sorted(self.g)}")
        self.g[name] = value
        self._rebuild()

    def save_gains(self):
        if self.gains_path:
            with open(self.gains_path, "w") as f:
                json.dump(self.g, f, indent=2, sort_keys=True)

    def reset(self):
        self._aoa_f = None
        self.pid_bank.reset()
        self.pid_pitch.reset()
        self.pid_mach.reset()

    def update(self, s, dt: float):
        g = self.g

        # ---- roll channel ----
        if self.tgt_hdg is not None:
            hdg_err = wrap_pi(self.tgt_hdg - s.hdg)          # rad
            bank_cap = math.radians(g["bank_cap_deg"])
            tgt_bank = max(-bank_cap, min(bank_cap, g["hdg_kp"] * hdg_err))
        else:
            tgt_bank = 0.0
        aileron = self.pid_bank.update(wrap_pi(tgt_bank - s.bank), dt)

        # ---- pitch channel: alt -> VV -> PITCH ATTITUDE -> elevator ----
        # Elevator commands pitch rate on the real jet, so VV responds to
        # elevator like an integrator through lags — a PID directly on VV
        # limit-cycles at any airspeed (the 7/20 porpoise). Attitude is the
        # variable elevator actually commands; close the fast loop there,
        # exactly like the roll channel, and let VV/alt close around it.
        # No altitude target -> latch current altitude and HOLD it (mirrors
        # the mach latch). "Hold VV 0" had no closure through the ACS
        # auto-trim: a constant correction gets trimmed away, so a bare
        # `auto` engage would drift at whatever VV it had (flown 7/20:
        # serene, uncommanded +5,700 fpm). The altitude outer loop is the
        # integrator this jet respects. `fly alt` overrides as always.
        if self.tgt_alt is None:
            self.tgt_alt = s.alt
        alt_err = self.tgt_alt - s.alt                        # m
        tgt_vv = max(-g["vv_cap"], min(g["vv_cap"], g["alt_kp"] * alt_err))
        tas = max(getattr(s, "tas", 0.0) or 0.0, 80.0)        # guard div0
        sin_g = max(-0.6, min(0.6, tgt_vv / tas))
        aoa = getattr(s, "aoa", 0.0) or 0.0
        if self._aoa_f is None:
            self._aoa_f = aoa
        self._aoa_f += (aoa - self._aoa_f) * (dt / (g["aoa_tau"] + dt))
        tgt_pitch = (math.asin(sin_g)                          # flight path for tgt_vv
                     + self._aoa_f                             # + SLOW AoA trim only
                     + g["vv2p_kp"] * (tgt_vv - s.vv))         # + residual VV nudge
        cap = math.radians(g["pitch_cap_deg"])
        tgt_pitch = max(-cap, min(cap, tgt_pitch))
        self.dbg_tgt_vv, self.dbg_tgt_pitch = tgt_vv, tgt_pitch   # for the recorder
        elevator = self.pid_pitch.update(tgt_pitch - s.pitch, dt, meas=s.pitch)
        # pull harder in a turn to hold the nose up
        comp = g["turn_comp"] * (1.0 / max(0.3, math.cos(s.bank)) - 1.0)
        elevator = max(-1.0, min(1.0, elevator + comp))

        # ---- throttle ----
        # No mach target -> latch current mach and HOLD it. The old
        # behavior (open-loop throttle 0.5) let the jet accelerate through
        # M1.0 unsupervised on 7/20. An explicit `fly mach` overrides.
        if self.tgt_mach is None:
            self.tgt_mach = s.mach
        throttle = self.pid_mach.update(self.tgt_mach - s.mach, dt)

        return aileron, elevator, throttle
