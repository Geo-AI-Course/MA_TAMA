"""
TAMA38 probability scoring — pure functions, no DB access.

Built against real field values observed in TLV.permits and TLV.building_sites:
  sw_tama_38 / sw_tama_38_chadash / sw_tama_38_tosefet : 'כן' | 'לא'
  building_stage : 'בבניה' | 'קיים אכלוס' | 'קיימת לפחות תעודת גמר אחת' |
                   'קיים היתר' | 'בתהליך היתר'
  open_request / permission_date / tr_hathalat_bniya   : Unix ms timestamp (int/float)
  finished                                              : 'DD/MM/YYYY' text (or null)

Composite score (0–100):
  permit    40 %  – TAMA38 permit stage
  age       25 %  – construction year (pre-1980 eligible for seismic upgrade)
  floors    15 %  – floor count (2–4 is the developer sweet spot)
  proximity 15 %  – TAMA38 projects within 200 m
  site       5 %  – open building-site case (active construction on record)
"""
from datetime import date, datetime, timezone

_TAMA38_YES = 'כן'

WEIGHTS = {"permit": 0.40, "age": 0.25, "floors": 0.15, "proximity": 0.15, "site": 0.05}

_OUTLOOK = (
    (75, "Active / High", "#22c55e"),
    (50, "Likely",        "#3b82f6"),
    (30, "Possible",      "#f59e0b"),
    ( 0, "Low",           "#ef4444"),
)

# building_stage → permit advancement rank (0–5)
_BS_RANK = {
    'בבניה':                       5,  # Under construction
    'קיים אכלוס':                  4,  # Occupied — construction finished
    'קיימת לפחות תעודת גמר אחת':  4,  # Has completion certificate
    'קיים היתר':                   3,  # Permit approved, not yet started
    'בתהליך היתר':                 2,  # In permit process
}

_SCORE_MAP = {5: 100, 4: 90, 3: 80, 2: 60, 1: 40, 0: 30}
_LABEL_MAP = {
    5: "Under construction",
    4: "Construction completed",
    3: "Permit approved",
    2: "In permit process",
    1: "Application submitted",
    0: "Application open",
}


# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_ts(val) -> date | None:
    """Parse Unix-ms timestamp (int/float) → date.  Handles None gracefully."""
    if val is None:
        return None
    try:
        return datetime.fromtimestamp(float(val) / 1000, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def _parse_ddmmyyyy(val) -> date | None:
    """Parse 'DD/MM/YYYY' text (the `finished` field).  Takes the first date if
    multiple are comma-separated."""
    if not val:
        return None
    first = str(val).split(',')[0].strip()
    try:
        return datetime.strptime(first, '%d/%m/%Y').date()
    except ValueError:
        return None


# ── Permit helpers ────────────────────────────────────────────────────────────

def _is_tama38(p: dict) -> bool:
    return any(p.get(k) == _TAMA38_YES
               for k in ('sw_tama_38', 'sw_tama_38_chadash', 'sw_tama_38_tosefet'))


def _rank_permit(p: dict) -> int:
    rank = _BS_RANK.get(p.get('building_stage') or '', -1)
    if rank >= 0:
        return rank
    # Fallback: use timestamp presence when building_stage is absent
    if p.get('permission_date'):
        return 3
    if p.get('open_request'):
        return 1
    return 0


def _score_permit(permits: list) -> tuple:
    """Returns (score, status_label, track_label, timeline_dict)."""
    tama = [p for p in permits if _is_tama38(p)]
    if not tama:
        return 0, "No TAMA38 permit found", "Unknown", {}

    best = max(tama, key=_rank_permit)
    rank = _rank_permit(best)

    # Track (chadash = new demolish+rebuild, tosefet = reinforce+add floors)
    if best.get('sw_tama_38_chadash') == _TAMA38_YES:
        track = "Track 2 — Demolish & Rebuild"
    elif best.get('sw_tama_38_tosefet') == _TAMA38_YES:
        track = "Track 1 — Reinforce & Add Floors"
    else:
        track = "TAMA38 (track unspecified)"

    # Timeline from Unix-ms timestamps
    timeline = {}
    for key, label in (
        ('open_request',      "Request opened"),
        ('permission_date',   "Permit granted"),
        ('tr_hathalat_bniya', "Construction started"),
    ):
        d = _parse_ts(best.get(key))
        if d:
            timeline[label] = d.isoformat()

    # finished is a DD/MM/YYYY text field
    fin = _parse_ddmmyyyy(best.get('finished'))
    if fin:
        timeline["Completed"] = fin.isoformat()

    return _SCORE_MAP[rank], _LABEL_MAP[rank], track, timeline


# ── Signal scorers ────────────────────────────────────────────────────────────

def _score_age(year) -> int:
    try:
        y = int(float(year))   # stored as double precision (e.g. 1972.0)
    except (TypeError, ValueError):
        return 40
    if y < 1970: return 90
    if y < 1980: return 75
    if y < 1990: return 25
    return 5


def _score_floors(ms_komot) -> int:
    try:
        f = int(float(ms_komot))  # stored as double precision (e.g. 4.0)
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

    _, outlook, color = next(r for r in _OUTLOOK if composite >= r[0])

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
