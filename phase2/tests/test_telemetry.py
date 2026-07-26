"""Telemetry tests — packet parsing and the 7/21 contact geometry.

Two things here are load-bearing and were untested:

  * The FC3 AoA quirk. LoGetAngleOfAttack returns DEGREES for the Su-27,
    not radians. The conversion happens once, at the door, in
    parse_packet. If it is ever "cleaned up", the pitch cascade gets an
    AoA feedforward ~57x too large and the jet diverges.
  * contact_geometry. DCS does not populate contact azimuth/elevation
    for FC3 (0.0 every packet, all night on 7/21), so aim comes from
    world positions instead. Ownship world position has an ambiguous
    API, so the export ships every candidate frame and we pick the one
    that reproduces the distance DCS itself reported. That selection is
    what makes the autonomous lock possible, and it is pure arithmetic.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon.telemetry import State, TelemetryListener, contact_geometry, parse_packet


def base_packet(**kw):
    p = {"v": 1, "seq": 7, "t": 12.5,
         "ownship": {"hdg_rad": math.radians(90), "pitch": 0.1, "bank": -0.2},
         "asl_m": 6000.0, "agl_m": 5800.0, "vv_ms": 3.0,
         "ias_ms": 200.0, "tas_ms": 300.0, "mach": 0.9, "g": 1.2,
         "aoa_rad": 6.0,                     # DEGREES on the wire (FC3 quirk)
         "fuel": {"internal": 4000, "external": 500}}
    p.update(kw)
    return p


# ============================================================ parse_packet
def test_parses_a_full_packet():
    s = parse_packet(base_packet(), State())
    assert abs(math.degrees(s.hdg) - 90) < 1e-9
    assert (s.pitch, s.bank) == (0.1, -0.2)
    assert (s.alt, s.agl, s.vv) == (6000.0, 5800.0, 3.0)
    assert (s.ias, s.tas, s.mach, s.g) == (200.0, 300.0, 0.9, 1.2)
    assert s.seq == 7 and s.t == 12.5


def test_aoa_arrives_in_degrees_and_is_stored_in_radians():
    """The FC3 quirk, converted once at the door. Do not 'simplify' this."""
    s = parse_packet(base_packet(aoa_rad=6.0), State())
    assert abs(math.degrees(s.aoa) - 6.0) < 1e-9
    assert abs(s.aoa - math.radians(6.0)) < 1e-12


def test_heading_is_normalised_to_one_turn():
    s = parse_packet(base_packet(ownship={"hdg_rad": math.radians(730)}), State())
    assert 0 <= s.hdg < 2 * math.pi
    assert abs(math.degrees(s.hdg) - 10) < 1e-6


def test_negative_heading_normalises_positive():
    s = parse_packet(base_packet(ownship={"hdg_rad": math.radians(-90)}), State())
    assert abs(math.degrees(s.hdg) - 270) < 1e-6


def test_fuel_sums_internal_and_external():
    s = parse_packet(base_packet(), State())
    assert s.fuel_kg == 4500


def test_fuel_tolerates_a_missing_external_tank():
    s = parse_packet(base_packet(fuel={"internal": 3000}), State())
    assert s.fuel_kg == 3000


def test_missing_scalars_carry_forward():
    """A pcall-guarded field that failed this frame must not zero the state."""
    prev = parse_packet(base_packet(), State())
    s = parse_packet({"seq": 8}, prev)
    assert s.alt == prev.alt and s.mach == prev.mach and s.hdg == prev.hdg
    assert s.seq == 8


def test_none_valued_fields_carry_forward():
    prev = parse_packet(base_packet(), State())
    s = parse_packet({"asl_m": None, "mach": None}, prev)
    assert s.alt == prev.alt and s.mach == prev.mach


def test_tactical_lists_default_to_empty_not_none():
    s = parse_packet({"contacts": None, "rwr": None, "locked": None}, State())
    assert s.contacts == [] and s.rwr == [] and s.locked == []


def test_contacts_are_replaced_not_merged():
    """A cleared radar picture must actually clear."""
    prev = parse_packet({"contacts": [{"id": 1}]}, State())
    s = parse_packet({"contacts": []}, prev)
    assert s.contacts == []


def test_non_dict_sight_and_ownpos_are_ignored():
    s = parse_packet({"sight": "nope", "ownpos": 42}, State())
    assert s.sight == {} and s.ownpos == {}


def test_non_int_seq_is_ignored():
    prev = parse_packet(base_packet(), State())
    s = parse_packet({"seq": "eight"}, prev)
    assert s.seq == 7


def test_errors_ship_in_packet():
    s = parse_packet({"errors": ["LoGetSightingSystemInfo failed"]}, State())
    assert s.errors == ["LoGetSightingSystemInfo failed"]


def test_freshness_is_stamped_on_receipt():
    s = parse_packet(base_packet(), State())
    assert s.fresh
    assert time.monotonic() - s.rx_time < 1.0


def test_a_never_updated_state_is_stale():
    assert State().fresh is False


def test_listener_inject_updates_latest():
    t = TelemetryListener()
    t.inject(base_packet())
    assert abs(math.degrees(t.latest().hdg) - 90) < 1e-9


def test_listener_inject_chains_state():
    t = TelemetryListener()
    t.inject(base_packet())
    t.inject({"seq": 9})
    assert t.latest().seq == 9 and t.latest().mach == 0.9


# ======================================================== contact_geometry
# DCS world frame: x = north, z = east, y = up.
def own(**candidates):
    return State(hdg=0.0, alt=0.0, ownpos=candidates)


def cnt(px, py, pz, dist=None, **kw):
    c = {"px": px, "py": py, "pz": pz}
    if dist is not None:
        c["dist_m"] = dist
    c.update(kw)
    return c


def test_none_without_world_position():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    assert contact_geometry(s, {"id": 1}) is None
    assert contact_geometry(s, cnt(1000, None, 0)) is None, "no py and no alt_m"


def test_altitude_falls_back_to_contact_alt_m():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    got = contact_geometry(s, cnt(1000, None, 0, dist=1000, alt_m=0.0))
    assert got is not None


def test_none_without_ownship_candidates():
    assert contact_geometry(State(hdg=0.0), cnt(1000, 0, 0, dist=1000)) is None


def test_bearing_due_north_is_zero_azimuth():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    az, el, src = contact_geometry(s, cnt(1000, 0, 0, dist=1000))
    assert abs(az) < 1e-6 and abs(el) < 1e-6 and src == "pp"


def test_bearing_due_east_is_plus_ninety():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    az, _, _ = contact_geometry(s, cnt(0, 0, 1000, dist=1000))
    assert abs(az - 90) < 1e-6


def test_bearing_due_west_is_minus_ninety():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    az, _, _ = contact_geometry(s, cnt(0, 0, -1000, dist=1000))
    assert abs(az + 90) < 1e-6


def test_azimuth_is_relative_to_the_nose():
    """Same contact, jet pointing at it: azimuth must be zero, not 90."""
    s = State(hdg=math.radians(90), alt=0.0, ownpos={"pp": {"x": 0, "y": 0, "z": 0}})
    az, _, _ = contact_geometry(s, cnt(0, 0, 1000, dist=1000))
    assert abs(az) < 1e-6


def test_azimuth_wraps_to_the_short_side():
    s = State(hdg=math.radians(10), alt=0.0, ownpos={"pp": {"x": 0, "y": 0, "z": 0}})
    az, _, _ = contact_geometry(s, cnt(0, 0, -1000, dist=1000))
    assert abs(az + 100) < 1e-6, "west of a 010 nose is -100, not +260"


def test_elevation_is_positive_above_the_horizon():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    _, el, _ = contact_geometry(s, cnt(1000, 1000, 0, dist=1414.2))
    assert abs(el - 45) < 0.1


def test_elevation_is_negative_below():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    _, el, _ = contact_geometry(s, cnt(1000, -1000, 0, dist=1414.2))
    assert el < -44


def test_the_frame_that_reproduces_dcs_distance_wins():
    """The whole point of shipping every candidate: no guessing."""
    s = own(pp={"x": 0, "y": 0, "z": 0},
            geo_lonlat={"x": 50000, "y": 0, "z": 50000})
    az, _, src = contact_geometry(s, cnt(10000, 0, 0, dist=10000))
    assert src == "pp", "the wrong frame must be rejected, not averaged in"
    assert abs(az) < 1e-6


def test_a_wrong_frame_is_rejected_even_when_it_is_the_only_one():
    s = own(geo_latlon={"x": 900000, "y": 0, "z": 900000})
    assert contact_geometry(s, cnt(1000, 0, 0, dist=1000)) is None


def test_fifteen_percent_is_the_rejection_boundary():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    assert contact_geometry(s, cnt(1000, 0, 0, dist=1000 / 1.10)) is not None
    assert contact_geometry(s, cnt(1000, 0, 0, dist=1000 / 1.30)) is None


def test_without_a_distance_reference_the_first_candidate_is_a_last_resort():
    s = own(pp={"x": 0, "y": 0, "z": 0},
            geo_lonlat={"x": 5, "y": 0, "z": 5})
    az, _, src = contact_geometry(s, cnt(1000, 0, 0))
    assert src == "pp"


def test_a_colocated_contact_is_skipped_not_divided_by():
    s = own(pp={"x": 0, "y": 0, "z": 0})
    assert contact_geometry(s, cnt(0.1, 0, 0.1, dist=0.14)) is None


def test_candidate_altitude_falls_back_to_telemetry_asl():
    """A candidate frame with no y uses the exported ASL instead."""
    s = State(hdg=0.0, alt=1000.0, ownpos={"pp": {"x": 0, "z": 0}})
    _, el, _ = contact_geometry(s, cnt(1000, 1000, 0, dist=1000))
    assert abs(el) < 1e-6, "contact at 1000 m, ownship at 1000 m -> level"


def test_incomplete_candidates_are_ignored():
    s = State(hdg=0.0, alt=0.0,
              ownpos={"pp": {"x": 0}, "geo_lonlat": {"x": 0, "y": 0, "z": 0}})
    _, _, src = contact_geometry(s, cnt(1000, 0, 0, dist=1000))
    assert src == "geo_lonlat"
