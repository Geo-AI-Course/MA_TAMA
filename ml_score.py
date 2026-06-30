"""
Thin wrapper around the trained TAMA38 completion model.

The model is loaded once on first call and cached for the life of the process.
Returns None gracefully when the model file hasn't been trained yet.

At prediction time the three time-ratio features are computed here from the
duration_stats that were saved alongside the model during training — so the
caller only needs to pass raw elapsed-day values.
"""
import json
import logging
import pickle
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).parent / "tama_model.pkl"
_META_PATH  = Path(__file__).parent / "tama_model_meta.json"

_pipe           = None
_feature_cols   = None
_nbhd_classes   = None
_duration_stats = None
_loaded         = False


def _load() -> bool:
    global _pipe, _feature_cols, _nbhd_classes, _duration_stats, _loaded
    _loaded = True
    if not _MODEL_PATH.exists():
        log.debug("tama_model.pkl not found — ML scoring disabled")
        return False
    try:
        with open(_MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        _pipe           = bundle["pipeline"]
        _feature_cols   = bundle["feature_cols"]
        _nbhd_classes   = bundle.get("nbhd_classes", [])
        _duration_stats = bundle.get("duration_stats", {})
        log.info("ML model loaded (%d features, %d duration-stat keys)",
                 len(_feature_cols), len(_duration_stats))
        return True
    except Exception as exc:
        log.warning("Failed to load tama_model.pkl: %s", exc)
        return False


# ── Time-ratio feature computation ────────────────────────────────────────────

def _compute_time_ratios(raw: dict, stats: dict) -> dict:
    """
    Derive progress_pct, permit_speed_ratio, days_past_p75_total from the
    raw elapsed-day values and the reference distribution stored during training.
    """
    extra = {}

    dsf1 = raw.get("days_since_form1")
    d_f2p = raw.get("days_form1_to_permit")

    # progress_pct: position in the typical lifecycle (>1 = overdue)
    s_total = stats.get("form1_to_form4")
    if s_total and dsf1 is not None:
        med = s_total["median"]
        extra["progress_pct"] = min(float(dsf1) / med, 3.0) if med else np.nan
    else:
        extra["progress_pct"] = np.nan

    # permit_speed_ratio: how fast was the Form-1 → Permit step?
    s_permit = stats.get("form1_to_permit")
    if s_permit and d_f2p is not None:
        med = s_permit["median"]
        extra["permit_speed_ratio"] = float(d_f2p) / med if med else np.nan
    else:
        extra["permit_speed_ratio"] = np.nan

    # days_past_p75_total: overshoot beyond the 75th-percentile total duration
    if s_total and dsf1 is not None:
        p75 = s_total["p75"]
        extra["days_past_p75_total"] = max(0.0, float(dsf1) - p75)
    else:
        extra["days_past_p75_total"] = np.nan

    return extra


# ── Public API ────────────────────────────────────────────────────────────────

def predict_completion_proba(features: dict) -> float | None:
    """
    Predict the probability that a building with an active TAMA38 permit will
    reach Form 4.

    Raw elapsed-day keys expected (subset is fine; missing → NaN):
        days_since_form1, days_form1_to_permit, days_permit_to_build,
        days_since_last_milestone, has_permit, has_construction,
        building_year, building_floors, is_track2, lat, lon,
        days_form1_to_verbal, days_verbal_to_signed, neighborhood (str, optional)

    Returns float in [0, 1] or None when the model is not available.
    """
    global _loaded
    if not _loaded and not _load():
        return None
    if _pipe is None:
        return None

    # Enrich with time-ratio features derived from the saved reference stats
    enriched = {**features, **_compute_time_ratios(features, _duration_stats or {})}

    # Build feature vector in training order
    row = []
    for col in _feature_cols:
        if col == "neighborhood_enc":
            nbhd = enriched.get("neighborhood")
            if nbhd and nbhd in _nbhd_classes:
                row.append(float(_nbhd_classes.index(nbhd)))
            else:
                row.append(np.nan)
        else:
            val = enriched.get(col)
            row.append(float(val) if val is not None else np.nan)

    try:
        proba = _pipe.predict_proba([row])[0][1]
        return round(float(proba), 4)
    except Exception as exc:
        log.warning("ML predict_proba failed: %s", exc)
        return None


def duration_stats() -> dict:
    """Return the reference duration-stats dict (or {} if model not loaded)."""
    global _loaded
    if not _loaded:
        _load()
    return _duration_stats or {}


def model_meta() -> dict:
    """Return the full training metadata dict (or {} if not available)."""
    if _META_PATH.exists():
        try:
            return json.loads(_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}
