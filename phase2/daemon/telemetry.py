"""Telemetry: parse Phase 1 packets into a state object; UDP listener thread.

7/21 additions (autonomous-lock fix):
  * State.ownpos       — ownship world-position candidates shipped by Export.lua
  * contacts now carry px/py/pz (world coords) when the export provides them
  * contact_geometry() — self-validating az/el computation. DCS's contact
    `azimuth`/`elevation` fields are unpopulated for FC3 (observed 0.0 all
    night 7/21), but contact world position IS exported. We compute bearing
    geometrically. Ownship world position has an ambiguous API (argument
    order of the geo conversion is folklore), so the export ships every
    candidate and we pick the one that reproduces the `distance` DCS itself
    reports for the contact. No guessing.
"""
from __future__ import annotations
import json
import math
import socket
import threading
import time
from dataclasses import dataclass, field


@dataclass
class State:
    """Ownship + tactical state, SI units internally (rad, m, m/s)."""
    t: float = 0.0            # sim model time
    hdg: float = 0.0          # radians, 0..2pi
    pitch: float = 0.0        # radians
    bank: float = 0.0         # radians, +right
    alt: float = 0.0          # m ASL
    agl: float = 0.0          # m AGL
    vv: float = 0.0           # m/s vertical velocity
    ias: float = 0.0          # m/s
    tas: float = 0.0          # m/s
    mach: float = 0.0
    g: float = 1.0
    aoa: float = 0.0          # radians
    fuel_kg: float = 0.0
    contacts: list = field(default_factory=list)
    sight: dict = field(default_factory=dict)   # sighting-system probe (may be empty)
    rwr: list = field(default_factory=list)
    locked: list = field(default_factory=list)
    ownpos: dict = field(default_factory=dict)  # world-pos candidates (7/21)
    seq: int = 0
    rx_time: float = 0.0      # wall clock of last packet
    errors: list = field(default_factory=list)

    @property
    def fresh(self) -> bool:
        return (time.monotonic() - self.rx_time) < 1.0


def parse_packet(p: dict, prev: State) -> State:
    """Build a State from a Phase 1 packet, carrying forward missing fields."""
    s = State(**{k: getattr(prev, k) for k in prev.__dataclass_fields__})
    own = p.get("ownship") or {}
    if own.get("hdg_rad") is not None:
        s.hdg = own["hdg_rad"] % (2 * math.pi)
    if own.get("pitch") is not None:
        s.pitch = own["pitch"]
    if own.get("bank") is not None:
        s.bank = own["bank"]
    for src, dst in (("asl_m", "alt"), ("agl_m", "agl"), ("vv_ms", "vv"),
                     ("ias_ms", "ias"), ("tas_ms", "tas"), ("mach", "mach"),
                     ("g", "g"), ("t", "t")):
        if p.get(src) is not None:
            setattr(s, dst, p[src])
    # FC3 quirk (validated in-jet 7/19): LoGetAngleOfAttack returns DEGREES
    # for the Su-27, not radians as nominal. Convert at the door.
    if p.get("aoa_rad") is not None:
        s.aoa = math.radians(p["aoa_rad"])
    fuel = p.get("fuel")
    if isinstance(fuel, dict) and fuel.get("internal") is not None:
        s.fuel_kg = (fuel.get("internal") or 0) + (fuel.get("external") or 0)
    s.contacts = p.get("contacts") or []
    s.sight = p.get("sight") if isinstance(p.get("sight"), dict) else {}
    s.rwr = p.get("rwr") or []
    s.locked = p.get("locked") or []
    s.ownpos = p.get("ownpos") if isinstance(p.get("ownpos"), dict) else {}
    if isinstance(p.get("seq"), int):
        s.seq = p["seq"]
    s.errors = p.get("errors") or []
    s.rx_time = time.monotonic()
    return s


# ---------------------------------------------------------------- geometry --
def _candidate_positions(s: State):
    """Yield (label, x, y, z) ownship world-position candidates.

    y (altitude, m) falls back to telemetry ASL when the candidate lacks it.
    """
    for label in ("pp", "geo_lonlat", "geo_latlon"):
        c = s.ownpos.get(label)
        if isinstance(c, dict) and c.get("x") is not None and c.get("z") is not None:
            y = c.get("y") if c.get("y") is not None else s.alt
            yield label, float(c["x"]), float(y), float(c["z"])


def contact_geometry(s: State, c: dict):
    """Relative az/el (degrees) to a contact, or None if not computable.

    Validation: for each ownship-position candidate, recompute the slant
    range to the contact and compare against the `dist_m` DCS exported for
    it. The candidate that agrees (within 15%) is the true frame. Returns
    (az_deg, el_deg, source_label) or None.

    Conventions (DCS world frame): x = north, z = east, y = up.
    az: +right of the nose. el: +above the horizon line.
    """
    px, py, pz = c.get("px"), c.get("py"), c.get("pz")
    if px is None or pz is None:
        return None
    if py is None:
        py = c.get("alt_m")
    if py is None:
        return None
    ref = c.get("dist_m")
    best = None  # (err_ratio, az_deg, el_deg, label)
    for label, ox, oy, oz in _candidate_positions(s):
        dx, dy, dz = float(px) - ox, float(py) - oy, float(pz) - oz
        ground = math.hypot(dx, dz)
        if ground < 1.0:
            continue
        slant = math.sqrt(ground * ground + dy * dy)
        if ref and ref > 1.0:
            err = abs(slant - ref) / ref
            if err > 0.15:
                continue                      # wrong frame — reject
        else:
            err = 1.0                         # no reference: last resort only
        bearing = math.atan2(dz, dx)          # world bearing, rad
        az = (bearing - s.hdg + math.pi) % (2 * math.pi) - math.pi
        el = math.atan2(dy, ground)
        if best is None or err < best[0]:
            best = (err, math.degrees(az), math.degrees(el), label)
    if best is None:
        return None
    return best[1], best[2], best[3]


class TelemetryListener:
    """Background UDP listener maintaining the latest State."""

    def __init__(self, port: int = 27015):
        self.port = port
        self.state = State()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def latest(self) -> State:
        with self._lock:
            return self.state

    def inject(self, packet: dict):
        """Feed a packet directly (mock mode / tests)."""
        with self._lock:
            self.state = parse_packet(packet, self.state)

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", self.port))
        sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                p = json.loads(data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if p.get("hello") or p.get("goodbye"):
                continue
            self.inject(p)
        sock.close()
