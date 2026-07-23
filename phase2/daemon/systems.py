"""Systems executor — discrete combat commands (radar, lock, fire, ...).

Two backends per command, chosen by systems.json:
  {"btn": 3}   -> pulse vJoy button 3 (focus-independent; PRIMARY path)
  {"key": "i"} -> inject a keystroke via pydirectinput (lands in the
                  FOCUSED window — overflow/fallback commands only)

Semantic names are what the REPL (and in Phase 3, the model) speaks; the
json maps them to hardware so rebinding never touches code. Buttons must
also be bound to the matching cockpit command in DCS's Controls UI, BY
HAND (doctrine: nothing writes to Saved Games\\Config\\Input, ever).

Default map fits vJoy's stock 8 buttons on purpose — do not reconfigure
vJoy's button count casually: a new HID descriptor makes DCS treat it as
a new device and auto-bind defaults (see 7/20 debugging doctrine).
"""
from __future__ import annotations

import json
import os
import threading
import time

PULSE_S = 0.15   # press duration for a button tap

DEFAULT_MAP = {
    # semantic       binding     bind in DCS Su-27 controls to:
    "radar":     {"btn": 1},   # Radar on/off            (kbd 'I')
    "eos":       {"btn": 2},   # EOS on/off              (kbd 'O')
    "lock":      {"btn": 3},   # Lock target             (kbd 'Enter')
    "unlock":    {"btn": 4},   # Unlock/reset target     (kbd 'Backspace')
    "wpn":       {"btn": 5},   # Cycle weapons           (kbd 'D')
    "fire":      {"btn": 6},   # Weapon release          (kbd 'Space')
    "dispense":  {"btn": 7},   # Countermeasures release
    "airbrake":  {"btn": 8},   # Airbrake                (kbd 'B')
    # hold-type commands (press/release, not pulse) — need the 32-button
    # vJoy reconfigure; see checklist before binding these
    "slew_up":    {"btn": 9,  "hold": True},   # TDC/designator up
    "slew_down":  {"btn": 10, "hold": True},   # TDC/designator down
    "slew_left":  {"btn": 11, "hold": True},   # TDC/designator left
    "slew_right": {"btn": 12, "hold": True},   # TDC/designator right
}


class SystemsExecutor:
    """Executes semantic commands through the output layer.

    Pulses run on a worker thread so a 150 ms button hold never stalls
    the 20 Hz control loop or the REPL.
    """

    def __init__(self, out, map_path: str = "systems.json"):
        self.out = out
        self.map_path = map_path
        self.map = dict(DEFAULT_MAP)
        if os.path.exists(map_path):
            with open(map_path) as f:
                self.map.update(json.load(f))   # user's file wins per command
        try:
            with open(map_path, "w") as f:
                json.dump(self.map, f, indent=2)  # persist merged map
        except OSError:
            pass
        self._lock = threading.Lock()   # one pulse at a time per device

    def names(self):
        return sorted(self.map)

    def describe(self) -> str:
        lines = []
        for name in self.names():
            b = self.map[name]
            how = f"btn {b['btn']}" if "btn" in b else f"key {b['key']!r} (focus!)"
            lines.append(f"  {name:10s} -> {how}")
        return "\n".join(lines)

    def hold(self, name: str):
        """Press and keep pressed a hold-type command. Pair with release()."""
        b = self.map.get(name)
        if b is None or "btn" not in b:
            raise KeyError(f"{name!r} is not a holdable button command")
        self.out.set_button(int(b["btn"]), True)

    def release(self, name: str):
        b = self.map.get(name)
        if b is None or "btn" not in b:
            raise KeyError(f"{name!r} is not a holdable button command")
        self.out.set_button(int(b["btn"]), False)

    def release_all_slew(self):
        for name, b in self.map.items():
            if b.get("hold") and "btn" in b:
                self.out.set_button(int(b["btn"]), False)

    def execute(self, name: str):
        b = self.map.get(name)
        if b is None:
            raise KeyError(f"unknown system command {name!r}; have {self.names()}")
        if "btn" in b:
            threading.Thread(target=self._pulse_btn, args=(int(b["btn"]),),
                             daemon=True).start()
        elif "key" in b:
            threading.Thread(target=self._press_key, args=(str(b["key"]),),
                             daemon=True).start()
        else:
            raise ValueError(f"{name!r} has neither 'btn' nor 'key'")

    def pulse_raw(self, n: int):
        threading.Thread(target=self._pulse_btn, args=(n,), daemon=True).start()

    def _pulse_btn(self, n: int):
        with self._lock:
            try:
                self.out.set_button(n, True)
                time.sleep(PULSE_S)
            finally:
                self.out.set_button(n, False)

    def _press_key(self, key: str):
        with self._lock:
            try:
                import pydirectinput
                pydirectinput.press(key)
            except ImportError:
                print("(pydirectinput not installed — `pip install pydirectinput`; "
                      "keystroke fallback unavailable, button path unaffected)")
