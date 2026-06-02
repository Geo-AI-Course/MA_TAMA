"""
TAMA38 probability scoring — pure functions, no DB access.

Composite score (0–100) from five weighted signals:
  permit    40 %  – direct TAMA38 permit status
  age       25 %  – building construction year (pre-1980 is eligible)
  floors    15 %  – floor count (2–4 is the developer sweet spot)
  proximity 15 %  – TAMA38 projects within 200 m
  site       5 %  – open building-site case on record
"""
from datetime import date, datetime

# Values that mean "no" in Tel Aviv's systems (Hebrew + common falsy)
_NO_VALS = frozenset({
    "", "לא", "no", "n", "false", "0", "null", "none", "-", "אין", "לא רלוונטי",
})

WEIGHTS = {"permit": 0.40, "age": 0.25, "floors": 0.15, "proximity": 0.15, "site": 0.05}

_OUTLOOK = ((75, "Active / High", "#22c55e"),
            (50, "Likely",        "#3b82f6"),
            (30, "Possible",      "#f59e0b"),
            ( 0, "Low",           "#ef4444"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_yes(val) -> bool:
    return val is not None and str(val).strip().lower() not in _NO_VALS


def _is_tama38(p: dict) -> bool:
    return any(_is_yes(p.get(k)) for k in
               ("sw_tama_38", "sw_tama_38_chadash", "sw_tama_38_tosefet"))


def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return datetime.fromisoformat(str(val)[:10]).date()
    except Exception:
        return None


def _rank_permit(p: dict) -> int:
    """0–5: how far along is this permit?"""
    bs        = str(p.get("building_stage") or "").lower()
    rs        = str(p.get("request_stage")  or "").lower()
    has_perm  = bool(p.get("permission_date"))
    has_start = bool(p.get("tr_hathalat_bniya"))
    expiry    = _parse_date(p.get("expiry_date"))
    expired   = expiry is not None and expiry < date.today()

    try:
        progress = int(p.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0

    if has_start or progress > 50 or any(k in bs for k in
            ("בביצוע", "בנייה", "active", "construction", "בהתקדמות")):
        return 5
    if has_perm and not expired:
        return 4
    if any(k in rs for k in ("ועדה", "committee", "review", "אישור", "בדיקה")):
        return 3
    if p.get("open_request"):
        return 1 if expired else 2
    return 0


_SCORE_MAP = {5: 100, 4: 85, 3: 70, 2: 55, 1: 20, 0: 30}
_LABEL_MAP = {
    5: "Under construction",
    4: "Permit approved",
    3: "Under committee review",
    2: "Request submitted",
    1: "Permit expired",
    0: "Application open",
}


# ── Signal scorers ────────────────────────────────────────────────────────────

def _score_permit(permits: list) -> tuple:
    """Returns (score, status_label, track_label, timeline_dict)."""
    tama = [p for p in permits if _is_tama38(p)]
    if not tama:
        return 0, "No TAMA38 permit found", "Unknown", {}

    best = max(tama, key=_rank_permit)
    rank = _rank_permit(best)

    if _is_yes(best.get("sw_tama_38_chadash")):
        track = "Track 2 — Demolish & Rebuild"
    elif _is_yes(best.get("sw_tama_38_tosefet")):
        track = "Track 1 — Reinforce & Add Floors"
    else:
        track = "TAMA38 (track unspecified)"

    timeline = {}
    for key, label in (("open_request",      "Request opened"),
                        ("permission_date",   "Permit granted"),
                        ("tr_hathalat_bniya", "Construction started"),
                        ("expiry_date",       "Permit expires")):
        d = _parse_date(best.get(key))
        if d:
            timeline[label] = d.isoformat()

    return _SCORE_MAP[rank], _LABEL_MAP[rank], track, timeline


def _score_age(year) -> int:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return 40
    if y < 1970: return 90
    if y < 1980: return 75
    if y < 1990: return 25
    return 5


def _score_floors(ms_komot) -> int:
    try:
        f = int(ms_komot)
    except (TypeError, ValueError):
        return 40
    if f <= 4:  return 90
    if f <= 7:  return 65
    if f <= 12: return 30
    return 10


def _score_proximity(nearby_count: int) -> int:
    if nearby_count == 0: return 0
    if nearby_count == 1: return 40
    if nearby_count <= 3: return 70
    return 100


# ── Composite ─────────────────────────────────────────────────────────────────

def compute_tama_score(
    *,
    permits: list,
    year,
    ms_komot,
    nearby_200m: int,
    has_open_site_case: bool,
) -> dict:
    s_permit, status, track, timeline = _score_permit(permits)
    s_age       = _score_age(year)
    s_floors    = _score_floors(ms_komot)
    s_proximity = _score_proximity(nearby_200m)
    s_site      = 100 if has_open_site_case else 0

    composite = max(0, min(100, int(round(
        s_permit    * WEIGHTS["permit"]    +
        s_age       * WEIGHTS["age"]       +
        s_floors    * WEIGHTS["floors"]    +
        s_proximity * WEIGHTS["proximity"] +
        s_site      * WEIGHTS["site"]
    ))))

    _, outlook, color = next(row for row in _OUTLOOK if composite >= row[0])

    return {
        "score":       composite,
        "outlook":     outlook,
        "color":       color,
        "status":      status,
        "track":       track,
        "timeline":    timeline,
        "nearby_200m": nearby_200m,
        "signals": {
            "permit":    s_permit,
            "age":       s_age,
            "floors":    s_floors,
            "proximity": s_proximity,
            "site":      s_site,
        },
    }
