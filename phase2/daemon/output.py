"""Control output layer.

VJoyOutput drives a vJoy virtual joystick (Windows, requires vJoy driver
+ pyvjoy). MockOutput just records the last commands — used by the
container tests and --mock mode.

Axis convention out of the autopilot:
    aileron  [-1, 1]  (+ right roll)
    elevator [-1, 1]  (+ pull / nose up)
    throttle [ 0, 1]

vJoy axes are 1..0x8000. IMPORTANT (see CLAUDE.md): DCS's default axis
curve maps joystick-forward to push. We invert elevator here so +pull
means pull; if the jet dives when it should climb, flip ELEV_SIGN.
"""
from __future__ import annotations

VJOY_MIN, VJOY_MAX = 0x1, 0x8000
ELEV_SIGN = 1.0    # was -1.0: caused pitch-up departure on first engage 7/19.
                   # Verify with 'test elev' (watch cockpit stick) before flight.
AIL_SIGN = 1.0


def _to_axis(v: float) -> int:
    v = max(-1.0, min(1.0, v))
    return int(VJOY_MIN + (v + 1.0) * 0.5 * (VJOY_MAX - VJOY_MIN))


class MockOutput:
    def __init__(self):
        self.aileron = 0.0
        self.elevator = 0.0
        self.throttle = 0.5
        self.button_log = []          # (button, state) history for tests

    def send(self, aileron: float, elevator: float, throttle: float):
        self.aileron, self.elevator, self.throttle = aileron, elevator, throttle

    def set_button(self, n: int, state: bool):
        self.button_log.append((n, state))

    def center(self):
        self.send(0.0, 0.0, self.throttle)

    def close(self):
        pass


class VJoyOutput:
    """Real output via vJoy device 1: X=aileron, Y=elevator, Z=throttle."""

    def __init__(self, device_id: int = 1):
        import pyvjoy  # deferred: only needed on the Windows box
        self.j = pyvjoy.VJoyDevice(device_id)
        self.throttle = 0.5
        self.center()

    def send(self, aileron: float, elevator: float, throttle: float):
        self.throttle = throttle
        self.j.set_axis(0x30, _to_axis(AIL_SIGN * aileron))          # X
        self.j.set_axis(0x31, _to_axis(ELEV_SIGN * elevator))        # Y
        self.j.set_axis(0x32, _to_axis(1.0 - throttle * 2.0))        # Z  (inverted: ramp test 7/19)

    def set_button(self, n: int, state: bool):
        """vJoy buttons are 1-based, up to the count in Configure vJoy."""
        self.j.set_button(n, 1 if state else 0)

    def center(self):
        """All three axes to their zero-contribution midpoints. Under DCS's
        device summing, mid = adds nothing to the pilot's physical inputs.
        Z included — leaving Z parked low under the inverted mapping was a
        hidden FULL-THROTTLE contribution (the 7/19 'unstable jet' bug)."""
        self.j.set_axis(0x30, _to_axis(0.0))
        self.j.set_axis(0x31, _to_axis(0.0))
        self.j.set_axis(0x32, _to_axis(0.0))

    def close(self):
        self.center()


def make_output(mock: bool):
    if mock:
        return MockOutput()
    return VJoyOutput()
