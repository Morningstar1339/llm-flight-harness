"""Phase 3 agent loop — model in the loop, above the inner-loop autopilot.

The model issues doctrine-level commands at ~8 s cadence; the PID cascade
does the flying. Everything that decides whether a command reaches the
aircraft lives in this file, on our side of the model seam, and is tested
with no model in the loop (see model_client.PilotClient).

## Authority — three states, and the safety word

    off       no agent. The state at boot.
    advisory  the agent decides and everything is logged, but NOTHING is
              dispatched. This is how you watch it think before you let
              it fly.
    active    commands dispatch, subject to the per-command gates below.

`manual` at the console revokes authority instantly and unconditionally
and restores exactly the MANUAL behaviour that existed before this file:
vJoy centred, human stick has the jet. It also bumps an authority epoch.
Every dispatch is gated on the epoch captured when its plan started, so a
revocation that lands mid-plan stops the remaining commands rather than
letting a stale plan keep flying. The gate and the actuation happen under
one lock, so a command is never revoked halfway through itself.

## Weapons — the agent never fires autonomously (MISSION Ruling 4)

FOX is a *request*. Release additionally requires a per-engagement
authorization typed by the human at the console:

    authorize fire <target_id> [seconds]

That token is single-use, target-specific, and expires. On top of it the
existing FOX interlock still applies: no lock on that target, no release.
An unauthorized agent fire request is refused and logged loudly — it never
reaches the systems executor. The human `fire` REPL command is untouched.

## Unit boundary

The model speaks aviation units, exactly like the REPL: degrees, feet,
mach. SI conversion happens here and nowhere else.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .controllers import wrap_pi

FT = 0.3048
NM = 1852.0

# Authority states
OFF = "off"
ADVISORY = "advisory"
ACTIVE = "active"
AUTHORITY_LEVELS = (OFF, ADVISORY, ACTIVE)

DEFAULT_CADENCE_S = 8.0
DEFAULT_FIRE_TTL_S = 120.0


# ============================================================ fire interlock
@dataclass
class FireAuthorization:
    """One human authorization for one engagement. Single-use, expiring."""
    target_id: int
    ttl_s: float
    issued_at: float
    issued_by: str = "console"
    consumed_at: float | None = None

    def remaining(self, now: float) -> float:
        return self.ttl_s - (now - self.issued_at)

    def check(self, target_id: int, now: float) -> str | None:
        """None if this authorization permits firing at target_id right now."""
        if self.consumed_at is not None:
            return (f"authorization for target {self.target_id} was already used "
                    f"— one authorization, one engagement")
        if self.remaining(now) <= 0:
            return (f"authorization for target {self.target_id} expired "
                    f"{-self.remaining(now):.0f}s ago")
        if int(target_id) != int(self.target_id):
            return (f"authorization is for target {self.target_id}, "
                    f"not target {target_id}")
        return None

    def describe(self, now: float) -> str:
        if self.consumed_at is not None:
            return f"target {self.target_id} — CONSUMED"
        rem = self.remaining(now)
        if rem <= 0:
            return f"target {self.target_id} — EXPIRED"
        return f"target {self.target_id} — valid {rem:.0f}s"


# =============================================================== command set
@dataclass(frozen=True)
class ArgSpec:
    name: str
    kind: str                       # number | integer | choice
    required: bool = False
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    unit: str = ""
    doc: str = ""

    def describe(self) -> str:
        if self.kind == "choice":
            rng = "|".join(self.choices)
        elif self.lo is not None:
            rng = f"{self.lo:g}..{self.hi:g}{(' ' + self.unit) if self.unit else ''}"
        else:
            rng = self.kind
        req = "" if self.required else ", optional"
        return f"{self.name} ({rng}{req})"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    args: tuple[ArgSpec, ...] = ()
    needs_engaged: bool = False     # requires AUTO (touches the flight path)
    needs_authorization: bool = False
    at_least_one: tuple[str, ...] = ()
    doc: str = ""


_DIRECTION = ArgSpec("direction", "choice", required=True,
                     choices=("left", "right"), doc="turn direction")
_HDG = ArgSpec("hdg", "number", lo=0, hi=360, unit="deg", doc="magnetic heading")
_ALT = ArgSpec("alt", "number", lo=500, hi=60000, unit="ft", doc="altitude MSL")
_MACH = ArgSpec("mach", "number", lo=0.2, hi=2.35, doc="target mach")
_THREAT = ArgSpec("threat_brg", "number", lo=0, hi=360, unit="deg",
                  doc="bearing TO the threat")
_TARGET = ArgSpec("target_id", "integer", required=True, lo=0, hi=9999,
                  doc="contact id from the CONTACTS block")

COMMANDS: dict[str, CommandSpec] = {
    s.name: s for s in (
        CommandSpec("FLY", (_HDG, _ALT, _MACH), needs_engaged=True,
                    at_least_one=("hdg", "alt", "mach"),
                    doc="Direct steer. Any subset of hdg/alt/mach; clears the "
                        "active maneuver and sets the commit reference."),
        CommandSpec("CRANK", (_DIRECTION,
                              ArgSpec("ref_hdg", "number", lo=0, hi=360, unit="deg",
                                      doc="reference bearing; defaults to the "
                                          "current target heading"),
                              ArgSpec("offset_deg", "number", lo=5, hi=90, unit="deg",
                                      doc="offset from the reference, default 50")),
                    needs_engaged=True,
                    doc="Offset from the reference bearing to cut closure while "
                        "holding the contact near the radar gimbal limit."),
        CommandSpec("NOTCH", (_DIRECTION,
                              ArgSpec("threat_brg", "number", required=True,
                                      lo=0, hi=360, unit="deg",
                                      doc="bearing TO the threat")),
                    needs_engaged=True,
                    doc="Put the threat on the beam (90 deg off)."),
        CommandSpec("PUMP", (_THREAT,), needs_engaged=True,
                    doc="Turn cold, descend, run fast. Defaults to the "
                        "reciprocal of the commit heading."),
        CommandSpec("RECOMMIT", (), needs_engaged=True,
                    doc="Return to the stored commit heading/altitude/mach."),
        CommandSpec("RADAR", (),
                    doc="Toggle the radar."),
        CommandSpec("LOCK", (_TARGET,),
                    doc="Start the autonomous designator hunt onto a contact."),
        CommandSpec("DROP_LOCK", (),
                    doc="Abort any lock hunt and reset the designator."),
        CommandSpec("FOX", (_TARGET,), needs_authorization=True,
                    doc="REQUEST weapon release. Refused unless the human has "
                        "authorized this exact target at the console and the "
                        "target is locked. You never fire on your own."),
        CommandSpec("DEFEND", (_THREAT,), needs_engaged=True,
                    doc="Countermeasures now, then beam the threat the short "
                        "way round if a bearing is given."),
        CommandSpec("RTB", (ArgSpec("hdg", "number", required=True, lo=0, hi=360,
                                    unit="deg", doc="heading home"),
                            _ALT, _MACH),
                    needs_engaged=True,
                    doc="Egress: steer home, default 20,000 ft / M0.85."),
        CommandSpec("HOLD", (),
                    doc="Do nothing this cycle. Use it deliberately — an empty "
                        "decision is better than a wrong one."),
    )
}


def command_reference() -> str:
    """The command table, rendered for the system prompt. Single source."""
    lines = []
    for spec in COMMANDS.values():
        args = ", ".join(a.describe() for a in spec.args) or "no args"
        flags = []
        if spec.needs_engaged:
            flags.append("requires AUTO")
        if spec.needs_authorization:
            flags.append("requires human authorization")
        if spec.at_least_one:
            flags.append("at least one of " + "/".join(spec.at_least_one))
        tail = f"  [{'; '.join(flags)}]" if flags else ""
        lines.append(f"{spec.name}: {args}{tail}\n    {spec.doc}")
    return "\n".join(lines)


# ================================================================== parsing
@dataclass
class Command:
    name: str
    args: dict
    intent: str = ""
    warnings: tuple[str, ...] = ()

    def __str__(self) -> str:
        a = " ".join(f"{k}={v}" for k, v in sorted(self.args.items()))
        return f"{self.name}{(' ' + a) if a else ''}"


# =================================================== FOX cite-or-label gate
# GT-03 measured, over two runs: 0 doctrine claims grounded in a manual
# passage, 4 asserted without citation, 2 honestly hedged. Pooling five
# samples of the shot scenario, 2 of 5 FOX requests justified themselves with
# an employment-envelope claim the manual had, that same turn, explicitly
# declined to support ("well inside employment envelope", "this is the shot
# window"). The human reads that intent when deciding whether to authorize, so
# an invented envelope is not a cosmetic problem -- it is bad evidence put in
# front of the person holding the trigger.
#
# The contract is CITE OR LABEL, not silence:
#   * assert doctrine and cite the page search_manual returned this turn, or
#   * say plainly that the reasoning is your own judgement, or
#   * do not make the claim.
# Any of those three passes. Only an unsupported assertion dressed as doctrine
# is refused.
#
# FALSE POSITIVES ON HONEST HEDGES: the hedges we measured all contain
# claim-flavoured vocabulary -- "Manual returned no doctrine on employment
# range, so this is my judgement, not a cited rule" trips every keyword a
# naive detector would use. The resolution is that a hedge IS a label: it
# self-identifies as uncited. So the detector never has to tell a hedge from a
# claim. It asks two independent questions -- is there a claim, and is there a
# label -- and a hedge answers yes to both, which passes. Every hedge observed
# in GT-03 runs 1 and 2 is pinned verbatim in the tests.

_CLAIM_PATTERNS = [
    r"\b(employment|launch|weapon'?s?|missile|firing|shot)\s+"
    r"(envelope|range|window|parameters|arc)\b",
    r"\b(inside|within|in)\s+(?:\w+\s+){0,3}(envelope|wez|shot\s+window|"
    r"launch\s+window)\b",
    r"\bwell\s+(inside|within|beyond|outside)\b",
    r"\bshot\s+window\b",
    r"\b(min|max|minimum|maximum)\s+(range|launch|abort)\b",
    r"\br-?27\w*\s+(range|envelope|shot)\b",
    r"\bdoctrine\b",
    r"\bf-?pole\b",
    r"\bgimbal\b",
    r"\bno\s+shot\s+at\b",
    r"\b(in|within|inside|outside|out\s+of)\s+range\b",
    r"\b(within|inside|outside|beyond|under|over|by)\s*~?\s*\d+(\.\d+)?\s*"
    r"(nm|nmi|nautical|miles|km|kft|ft|kts?|knots)\b",
]

# Explicit self-attribution. Any of these means the model is telling the human
# "this is me, not the book", which is exactly what the contract asks for.
_LABEL_PATTERNS = [
    r"\bmy\s+(own\s+)?(judge?ment|assessment|estimate|read|call|opinion)\b",
    r"\b(judge?ment|assessment)\s+call\b",
    r"\bnot\s+(a\s+)?cited\b",
    r"\bnot\s+cited\b",
    r"\buncited\b",
    r"\bnot\s+(manual\s+)?doctrine\b",
    r"\bmanual\s+(search\s+)?(returned|gives|give|has|had|found|provides|"
    r"offers|contains)\s+(me\s+)?no\b",
    r"\bno\s+(relevant\s+)?(manual|doctrine|passage|citation)\b",
    r"\bwithout\s+(manual|doctrine|citation)\s+support\b",
    r"\bnot\s+(from|in)\s+the\s+manual\b",
]

_CITATION_RE = re.compile(r"\bp\.\s*(\d+)")
_TOOL_REFUSAL = "MANUAL: no relevant passage found"


def detect_doctrine_claim(intent: str) -> str | None:
    """Return the matched phrase if `intent` asserts doctrine, else None."""
    if not intent:
        return None
    for pat in _CLAIM_PATTERNS:
        m = re.search(pat, intent, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def has_judgment_label(intent: str) -> bool:
    """True if `intent` explicitly attributes the reasoning to the model."""
    if not intent:
        return False
    return any(re.search(p, intent, re.IGNORECASE) for p in _LABEL_PATTERNS)


def citations_in(text: str) -> set:
    """Page references (`p.11`) mentioned in a piece of text."""
    return {m.group(1) for m in _CITATION_RE.finditer(text or "")}


def turn_citations(transcript=()) -> set:
    """Page references search_manual actually returned this turn.

    A refusal ("no relevant passage") and an errored call cite nothing.
    """
    out = set()
    for res in transcript or ():
        text = getattr(res, "text", str(res))
        if not getattr(res, "ok", True) or _TOOL_REFUSAL in text:
            continue
        out |= citations_in(text)
    return out


def check_cite_or_label(intent: str, transcript=()) -> str | None:
    """None if a FOX intent satisfies the contract; else why it does not."""
    claim = detect_doctrine_claim(intent)
    if claim is None:
        return None                      # no claim to support (see DECISIONS)
    if has_judgment_label(intent):
        return None                      # labelled as the model's own call
    available = turn_citations(transcript)
    if citations_in(intent) & available:
        return None                      # cited a page returned this turn
    if available:
        return (f"the intent asserts doctrine ({claim!r}) without citing it. "
                f"search_manual returned p.{', p.'.join(sorted(available))} "
                f"this turn — cite the page it came from, or say plainly that "
                f"the reasoning is your own judgement.")
    return (f"the intent asserts doctrine ({claim!r}) but search_manual "
            f"returned nothing citable this turn. Either label the reasoning "
            f"as your own judgement, or drop the claim.")


@dataclass
class CommandResult:
    command: str
    ok: bool
    detail: str
    intent: str = ""
    t: float = field(default_factory=time.monotonic)

    def __str__(self) -> str:
        mark = "ok" if self.ok else "REFUSED"
        note = f'  "{self.intent}"' if self.intent else ""
        return f"{self.command}: {mark} — {self.detail}{note}"


def extract_json(text: str):
    """Pull the first JSON value out of model text. (value, error).

    Tolerates ```json fences and prose on either side — the model is
    talking to a human debrief log as well as to a parser.
    """
    if not text or not text.strip():
        return None, "empty response"
    s = text.strip()
    if "```" in s:
        # take the content of the first fenced block
        parts = s.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            s = body.strip()
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None, "no JSON object or array in response"
    try:
        value, end = json.JSONDecoder().raw_decode(s[start:])
    except json.JSONDecodeError as e:
        return None, f"malformed JSON: {e}"
    # A SECOND JSON value in the same response is refused outright. Observed
    # live in GT-03: the model emitted a bare object followed by an array, and
    # raw_decode silently kept the object -- a FOX and a HOLD it believed it
    # had issued never reached the human, with no error raised. When two plans
    # arrive we cannot know which was meant, so neither flies. Trailing PROSE
    # is still fine; only a decodable second value is an error.
    rest = s[start + end:]
    nxt = min([i for i in (rest.find("{"), rest.find("[")) if i >= 0], default=-1)
    if nxt >= 0:
        try:
            json.JSONDecoder().raw_decode(rest[nxt:])
        except json.JSONDecodeError:
            pass                    # a stray brace in prose, not a second plan
        else:
            return None, ("response contains more than one JSON value — "
                          "refusing the whole response rather than silently "
                          "dropping the rest. Send exactly one object, or one "
                          "array of objects.")
    return value, None


def validate_command(raw) -> tuple[Command | None, str | None]:
    """One raw dict -> a validated Command, or an error string."""
    if not isinstance(raw, dict):
        return None, f"command must be an object, got {type(raw).__name__}"
    name = raw.get("command") or raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "missing 'command'"
    name = name.strip().upper()
    spec = COMMANDS.get(name)
    if spec is None:
        return None, (f"unknown command {name!r}; "
                      f"valid: {', '.join(COMMANDS)}")

    args_in = raw.get("args", {})
    if args_in is None:
        args_in = {}
    if not isinstance(args_in, dict):
        return None, f"{name}: 'args' must be an object"

    by_name = {a.name: a for a in spec.args}
    unknown = sorted(set(args_in) - set(by_name))
    if unknown:
        return None, (f"{name}: unknown argument(s) {', '.join(unknown)}; "
                      f"expected {sorted(by_name) or 'none'}")

    args: dict = {}
    for a in spec.args:
        if a.name not in args_in:
            if a.required:
                return None, f"{name}: missing required argument {a.name!r}"
            continue
        v = args_in[a.name]
        if a.kind == "choice":
            if not isinstance(v, str):
                return None, f"{name}.{a.name}: must be one of {a.choices}"
            vl = v.strip().lower()
            match = [c for c in a.choices if c.startswith(vl[:1])] if vl else []
            if vl not in a.choices and len(match) != 1:
                return None, (f"{name}.{a.name}: {v!r} is not one of "
                              f"{'/'.join(a.choices)}")
            args[a.name] = vl if vl in a.choices else match[0]
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, (f"{name}.{a.name}: must be a number, "
                          f"got {type(v).__name__}")
        if a.kind == "integer":
            if float(v) != int(v):
                return None, f"{name}.{a.name}: must be a whole number, got {v}"
            v = int(v)
        if a.lo is not None and not (a.lo <= v <= a.hi):
            return None, (f"{name}.{a.name}: {v}{(' ' + a.unit) if a.unit else ''} "
                          f"is outside {a.lo:g}..{a.hi:g} — refusing rather than "
                          f"clamping a number this far off")
        args[a.name] = v

    if spec.at_least_one and not any(k in args for k in spec.at_least_one):
        return None, (f"{name}: needs at least one of "
                      f"{', '.join(spec.at_least_one)}")

    warnings = []
    intent = raw.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        intent = "(no intent given)"
        warnings.append("no 'intent' string — logged for debrief, give one")
    return Command(name, args, intent.strip(), tuple(warnings)), None


def parse_commands(text: str) -> tuple[list[Command], list[str]]:
    """Model text -> (commands, errors). Never raises."""
    value, err = extract_json(text)
    if err:
        return [], [err]
    items = value if isinstance(value, list) else [value]
    commands, errors = [], []
    for i, raw in enumerate(items):
        cmd, e = validate_command(raw)
        if e:
            errors.append(f"[{i}] {e}")
        else:
            commands.append(cmd)
    return commands, errors


# ================================================================= snapshot
def _fmt_hdg(rad) -> str:
    return "---" if rad is None else f"{math.degrees(rad) % 360:03.0f}"


def build_snapshot(daemon, pilot=None, history=3) -> str:
    """Compact tactical picture for the model. Pure-ish: reads, never writes."""
    s = daemon.tele.latest()
    ap = daemon.ap
    now = time.monotonic()
    lines = [f"=== HARNESS STATE  seq {s.seq}  sim_t {s.t:.1f} ==="]
    mode = "AUTO" if daemon.engaged else "MANUAL"
    auth = pilot.authority if pilot is not None else OFF
    mock = "   [MOCK]" if getattr(daemon, "mock", False) else ""
    lines.append(f"MODE: {mode}   AGENT AUTHORITY: {auth}{mock}")
    lines.append(
        f"OWN: hdg {math.degrees(s.hdg) % 360:05.1f}  alt {s.alt / FT:,.0f} ft  "
        f"M{s.mach:.2f}  vv {s.vv * 196.85:+.0f} fpm  bank {math.degrees(s.bank):+.1f}  "
        f"aoa {math.degrees(s.aoa):+.1f}  g {s.g:.1f}  fuel {s.fuel_kg:,.0f} kg")
    alt_txt = f"{ap.tgt_alt / FT:,.0f} ft" if ap.tgt_alt is not None else "---"
    mach_txt = f"{ap.tgt_mach:.2f}" if ap.tgt_mach is not None else "---"
    commit = (f"  (commit hdg {_fmt_hdg(daemon.mm.commit_hdg)})"
              if daemon.mm.commit_hdg is not None else "")
    lines.append(f"AP SETPOINTS: mode={daemon.mm.active}  "
                 f"tgt hdg {_fmt_hdg(ap.tgt_hdg)}  alt {alt_txt}  "
                 f"mach {mach_txt}{commit}")

    locked_ids = [l.get("id") for l in s.locked]
    lines.append("CONTACTS:")
    if not s.contacts:
        lines.append("  (none)")
    for c in s.contacts:
        mark = " [LOCKED]" if c.get("id") in locked_ids else ""
        lines.append(
            f"  id {c.get('id')}  az {math.degrees(c.get('az_rad') or 0.0):+6.1f}  "
            f"{(c.get('dist_m') or 0.0) / NM:5.1f} nm  "
            f"{(c.get('alt_m') or 0.0) / FT:7,.0f} ft  "
            f"M{c.get('mach') or 0:.2f}{mark}")
    lines.append("RWR:")
    if not s.rwr:
        lines.append("  (clean)")
    for e in s.rwr:
        lines.append(f"  type {e.get('type')}  az "
                     f"{math.degrees(e.get('az_rad') or 0.0):+6.1f}  "
                     f"pri {e.get('priority')}")

    if pilot is not None:
        fa = pilot.fire_auth
        lines.append("WEAPONS: FOX authorization: " +
                     (fa.describe(now) if fa is not None else
                      "NONE — request FOX and ask the human to authorize"))
    lines.append(f"LOCK MANAGER: {daemon.lm.status}")
    if s.errors:
        lines.append(f"TELEMETRY ERRORS: {s.errors}")
    if pilot is not None and pilot.log:
        lines.append("RECENT DECISIONS (newest last):")
        for r in list(pilot.log)[-history:]:
            lines.append(f"  {r}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are the pilot agent for a Su-27 flying in the DCS simulator.
A deterministic PID autopilot does the actual flying; you issue doctrine-level
commands roughly every 8 seconds and it holds what you ask for.

You are given a state snapshot. Reply with ONE JSON object, or a JSON array of
at most 3 objects to be executed in order:

  {{"command": "CRANK", "args": {{"direction": "left"}}, "intent": "cut closure, hold the contact at gimbal"}}

`intent` is a short human-readable reason. It is written to the debrief log and
is how the human understands what you were thinking. Always include it.

COMMANDS
{command_reference()}

RULES
- Aviation units: degrees, feet, mach. Never SI.
- Out-of-range numbers are refused, not clamped. Stay inside the stated ranges.
- Commands marked "requires AUTO" only run when the human has engaged the
  autopilot. If MODE is MANUAL, the human is flying: use HOLD or systems
  commands only.
- You do NOT fire weapons. FOX is a request. Release requires the human to type
  an authorization for that exact target at the console, and requires a lock.
  If you believe a shot is available, issue FOX with a clear intent — the
  refusal message tells the human what you want.

- CITE OR LABEL, on FOX especially. The human reads your `intent` when deciding
  whether to authorize a shot, so it must not put invented doctrine in front of
  them. If your intent asserts doctrine — an employment envelope, a launch
  window, a range or a threshold — then ONE of these must be true:
    * you cite the page search_manual returned to you this turn, e.g.
      "per p.22 ...". Cite only what you actually got back this turn.
    * you say plainly that it is your own judgement, e.g. "manual returned no
      employment-range doctrine, so this is my judgement, not a cited rule".
    * you drop the claim and describe the geometry instead: "8 nm, head-on,
      M1.2 closing, RWR correlating" needs no citation — it is what the human
      can see on their own display.
  A FOX that asserts doctrine without citing it and without labelling it is
  REFUSED, and the refusal names the phrase that tripped it. Saying "I do not
  have doctrine for this" is always safe and is never held against you. Making
  a number up is the only thing that is.

- The manual DOES carry missile employment ranges, but they are per-weapon and
  easy to conflate. The radar-guided R-27ER/R and the infrared R-27ET/T have
  DIFFERENT ranges on DIFFERENT pages, and the semi-active radar missiles must
  be guided to impact while the IR ones are fire-and-forget. If you quote a
  range, quote it for the weapon you are actually shooting, and cite the page
  that number is printed on — not the page of the section heading above it.
  A citation that points at the wrong page or the wrong missile is worse than
  no citation, because it looks checked.

- If a query returns nothing, that is the tool working, not failing. Try
  rephrasing once; if it still returns nothing, label your reasoning as your
  own judgement. Do not fill the silence.
- The human can revoke your authority at any instant with the word `manual`.
  That is normal and expected. Do not work around it.
- You have a search_manual tool over the Su-27 tactics manual. Use it when
  doctrine matters (employment ranges, evasion) and you are unsure. It cites
  its sources and says plainly when the manual does not cover something —
  if it says that, do not invent doctrine.
- When you have nothing useful to do, HOLD. An empty decision beats a wrong one.
- Output JSON only. No commentary outside it.
"""


# ================================================================== the loop
class AgentPilot:
    """Owns agent authority, the fire interlock, and the decision cycle."""

    def __init__(self, daemon, registry, client=None,
                 cadence_s: float = DEFAULT_CADENCE_S, log_size: int = 200):
        self.d = daemon
        self.registry = registry
        self.client = client
        self.cadence_s = cadence_s
        self.authority = OFF
        self.fire_auth: FireAuthorization | None = None
        self.log: deque[CommandResult] = deque(maxlen=log_size)
        self.last_error: str | None = None
        self._epoch = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------- authority ---
    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def set_authority(self, level: str, reason: str = "") -> str:
        if level not in AUTHORITY_LEVELS:
            raise ValueError(f"authority must be one of {AUTHORITY_LEVELS}")
        with self._lock:
            if level == ACTIVE and not self.d.engaged:
                return ("refused: agent cannot take the flight path while the "
                        "daemon is in MANUAL. Type 'auto' first.")
            if level == OFF:
                return self.revoke(reason or "agent off")
            prev, self.authority = self.authority, level
            # Any authority change invalidates plans built under the old one.
            self._epoch += 1
            return f"agent authority {prev} -> {level}" + (f" ({reason})" if reason else "")

    def revoke(self, reason: str) -> str:
        """Instant, unconditional. This is what the safety word calls."""
        with self._lock:
            prev = self.authority
            self.authority = OFF
            self._epoch += 1
            had_auth = self.fire_auth is not None
            self.fire_auth = None
            self._stop.set()
        note = " (fire authorization cleared)" if had_auth else ""
        return f"agent authority {prev} -> off — {reason}{note}"

    # ------------------------------------------------- fire authorization --
    def authorize_fire(self, target_id: int, ttl_s: float = DEFAULT_FIRE_TTL_S,
                       issued_by: str = "console") -> str:
        """Human act. The agent has no path to this method."""
        with self._lock:
            self.fire_auth = FireAuthorization(
                target_id=int(target_id), ttl_s=float(ttl_s),
                issued_at=time.monotonic(), issued_by=issued_by)
            return (f"FIRE AUTHORIZED for target {int(target_id)}, valid "
                    f"{ttl_s:.0f}s, single use. Lock interlock still applies.")

    def deauthorize(self, reason: str = "console") -> str:
        with self._lock:
            if self.fire_auth is None:
                return "no fire authorization outstanding"
            tid = self.fire_auth.target_id
            self.fire_auth = None
            return f"fire authorization for target {tid} revoked ({reason})"

    # ---------------------------------------------------------- dispatch --
    def _refuse(self, cmd: Command, why: str) -> CommandResult:
        res = CommandResult(str(cmd), False, why, cmd.intent)
        self.log.append(res)
        return res

    def _accept(self, cmd: Command, detail: str) -> CommandResult:
        res = CommandResult(str(cmd), True, detail, cmd.intent)
        self.log.append(res)
        return res

    def dispatch(self, cmd: Command, epoch: int | None = None,
                 transcript=()) -> CommandResult:
        """Gate and execute one command. The gate and the action share a lock,
        so a revocation lands *between* commands, never inside one."""
        spec = COMMANDS[cmd.name]
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return self._refuse(cmd, "authority changed mid-plan — plan abandoned")
            if self.authority == OFF:
                return self._refuse(cmd, "agent authority is off")
            if self.authority == ADVISORY:
                return self._refuse(cmd, "ADVISORY — recommendation only, not executed")
            if spec.needs_engaged and not self.d.engaged:
                return self._refuse(cmd, "daemon is in MANUAL — the human has the jet")
            try:
                return self._execute(cmd, spec, transcript)
            except Exception as e:      # a bad command must not kill the loop
                return self._refuse(cmd, f"dispatch raised {type(e).__name__}: {e}")

    def _execute(self, cmd: Command, spec: CommandSpec,
                 transcript=()) -> CommandResult:
        d, a = self.d, cmd.args
        s = d.tele.latest()

        if cmd.name == "HOLD":
            return self._accept(cmd, "holding, no change")

        if cmd.name == "FLY":
            d.mm.fly(hdg_rad=math.radians(a["hdg"]) if "hdg" in a else None,
                     alt_m=a["alt"] * FT if "alt" in a else None,
                     mach=a.get("mach"))
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "CRANK":
            ref = (math.radians(a["ref_hdg"]) if "ref_hdg" in a else
                   (d.ap.tgt_hdg if d.ap.tgt_hdg is not None else s.hdg))
            d.mm.crank(s, a["direction"], ref, a.get("offset_deg", 50.0))
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "NOTCH":
            d.mm.notch(s, a["direction"], math.radians(a["threat_brg"]))
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "PUMP":
            brg = math.radians(a["threat_brg"]) if "threat_brg" in a else None
            d.mm.pump(s, brg)
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "RECOMMIT":
            if not d.mm.recommit(s):
                return self._refuse(cmd, "no commit reference stored yet")
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "RTB":
            d.mm.fly(hdg_rad=math.radians(a["hdg"]),
                     alt_m=a.get("alt", 20000.0) * FT,
                     mach=a.get("mach", 0.85))
            d.mm.active = "rtb"
            return self._accept(cmd, d.mm.describe(s))

        if cmd.name == "DEFEND":
            d.sysx.execute("dispense")
            if "threat_brg" not in a:
                return self._accept(cmd, "countermeasures away, heading unchanged")
            threat = math.radians(a["threat_brg"])
            # beam the threat the short way round from where the nose is now
            left = wrap_pi(threat - math.pi / 2 - s.hdg)
            right = wrap_pi(threat + math.pi / 2 - s.hdg)
            direction = "left" if abs(left) <= abs(right) else "right"
            d.mm.notch(s, direction, threat)
            return self._accept(cmd, f"countermeasures away, beaming {direction}; "
                                     f"{d.mm.describe(s)}")

        if cmd.name == "RADAR":
            d.sysx.execute("radar")
            return self._accept(cmd, "radar toggled")

        if cmd.name == "LOCK":
            tid = a["target_id"]
            if not d.lm.lock_contact(tid):
                return self._refuse(cmd, d.lm.status)
            return self._accept(cmd, f"lock hunt started on contact {tid}")

        if cmd.name == "DROP_LOCK":
            d.lm.abort()
            d.sysx.execute("unlock")
            return self._accept(cmd, "lock hunt aborted, designator reset")

        if cmd.name == "FOX":
            return self._fox(cmd, s, transcript)

        return self._refuse(cmd, "no dispatch path — this is a harness bug")

    def _fox(self, cmd: Command, s, transcript=()) -> CommandResult:
        """Weapon release request. Ruling 4: the trigger is a human act."""
        tid = cmd.args["target_id"]

        # Cite-or-label runs FIRST, before the authorization is even looked at.
        # Two reasons: the model then gets the same feedback whether or not the
        # human happened to have authorized, and refusing here cannot possibly
        # consume an authorization -- it is unreachable from this branch.
        bad = check_cite_or_label(cmd.intent, transcript)
        if bad:
            return self._refuse(
                cmd, f"FOX REFUSED — {bad}\n"
                     f'      intent as given: "{cmd.intent}"')

        auth = self.fire_auth
        if auth is None:
            return self._refuse(
                cmd, f"FOX REFUSED — no human fire authorization. The human must "
                     f"type `authorize fire {tid}` at the console.")
        err = auth.check(tid, time.monotonic())
        if err:
            return self._refuse(cmd, f"FOX REFUSED — {err}")
        locked_ids = [l.get("id") for l in s.locked]
        if tid not in locked_ids:
            return self._refuse(
                cmd, f"FOX REFUSED — interlock: no lock on target {tid} "
                     f"(locked: {locked_ids or 'nothing'})")
        auth.consumed_at = time.monotonic()
        self.d.sysx.execute("fire")
        # A bare readback passes the gate (see DECISIONS.md), but say so, so a
        # drift toward justification-free shot requests is visible in the log
        # rather than looking like a clean pass.
        bare = "" if detect_doctrine_claim(cmd.intent) else \
               " [no doctrine justification offered]"
        return self._accept(cmd, f"FOX — release commanded on target {tid} under "
                                 f"human authorization. R-27 is SARH: keep the "
                                 f"lock.{bare}")

    # ------------------------------------------------------------- cycle --
    def execute_plan(self, commands, epoch: int | None = None,
                     transcript=()) -> list[CommandResult]:
        if epoch is None:
            epoch = self.epoch
        results = []
        for cmd in commands:
            results.append(self.dispatch(cmd, epoch, transcript))
        return results

    def snapshot(self) -> str:
        return build_snapshot(self.d, self)

    def step(self) -> tuple[list[CommandResult], list[str]]:
        """One decision cycle. (results, errors). Never raises."""
        if self.client is None:
            return [], ["no model client attached — `agent client mock` or attach one"]
        with self._lock:
            if self.authority == OFF:
                return [], ["agent authority is off"]
            epoch = self._epoch
        snap = self.snapshot()
        # Scope the tool transcript to this decision: the FOX gate must judge
        # the intent against what the manual returned *this* turn, not what it
        # returned three cycles ago in a different engagement.
        self.registry.begin_turn()
        try:
            text = self.client.decide(snap)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return [], [f"model call failed: {self.last_error}"]
        transcript = self.registry.transcript()
        commands, errors = parse_commands(text)
        for c in commands:
            for w in c.warnings:
                errors.append(f"{c.name}: {w}")
        if self.authority == ADVISORY:
            results = [CommandResult(str(c), True, "ADVISORY — recommendation only",
                                     c.intent) for c in commands]
            self.log.extend(results)
            return results, errors
        return self.execute_plan(commands, epoch, transcript), errors

    def run(self, cycles: int | None = None, cadence_s: float | None = None):
        """Background decision loop. Stops on revocation."""
        if self._thread is not None and self._thread.is_alive():
            return "agent loop already running"
        cadence = self.cadence_s if cadence_s is None else cadence_s
        self._stop.clear()

        def loop():
            n = 0
            while not self._stop.is_set():
                if self.authority == OFF:
                    break
                results, errors = self.step()
                for r in results:
                    print(f"  agent> {r}")
                for e in errors:
                    print(f"  agent! {e}")
                n += 1
                if cycles is not None and n >= cycles:
                    break
                self._stop.wait(cadence)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return (f"agent loop running at {cadence:.1f}s cadence"
                f"{'' if cycles is None else f', {cycles} cycles'}")

    def stop(self) -> str:
        self._stop.set()
        return "agent loop stopped (authority unchanged)"

    # ------------------------------------------------------------ status --
    def status_line(self) -> str:
        now = time.monotonic()
        fa = self.fire_auth
        parts = [f"authority={self.authority}",
                 f"epoch={self.epoch}",
                 f"client={getattr(self.client, 'name', 'none')}",
                 f"cadence={self.cadence_s:.0f}s",
                 f"tools={','.join(self.registry.names()) or 'none'}",
                 f"fire_auth={fa.describe(now) if fa else 'none'}"]
        line = "  ".join(parts)
        if self.last_error:
            line += f"\n  last model error: {self.last_error}"
        return line
