"""
Thin wrapper around the two trained TAMA38 models.

  tama_model.pkl             — completion probability for buildings that
                                already have an active TAMA38 permit
  tama_likelihood_model.pkl  — how "TAMA38-like" a building looks, for
                                buildings that don't have a permit yet

Each model is loaded once on first call and cached for the life of the
process.  Both return None gracefully when their model file hasn't been
trained yet.

At prediction time the completion model's three time-ratio features are
computed here from the duration_stats saved alongside it during training —
so the caller only needs to pass raw elapsed-day values.
"""
import json
import logging
import pickle
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_MODEL_PATH            = Path(__file__).parent / "tama_model.pkl"
_META_PATH             = Path(__file__).parent / "tama_model_meta.json"
_LIKELIHOOD_MODEL_PATH = Path(__file__).parent / "tama_likelihood_model.pkl"


# ── Generic lazy-loaded model bundle ──────────────────────────────────────────

class _LazyModel:
    """Loads a {"pipeline", "feature_cols", "nbhd_classes", ...} pickle bundle
    on first use and builds feature vectors in training-column order."""

    def __init__(self, path: Path):
        self.path         = path
        self.pipe         = None
        self.feature_cols = None
        self.nbhd_classes = []
        self.extra        = {}
        self.loaded       = False

    def ensure_loaded(self) -> bool:
        if self.loaded:
            return self.pipe is not None
        self.loaded = True
        if not self.path.exists():
            log.debug("%s not found — model disabled", self.path.name)
            return False
        try:
            with open(self.path, "rb") as f:
                bundle = pickle.load(f)
            self.pipe         = bundle["pipeline"]
            self.feature_cols = bundle["feature_cols"]
            self.nbhd_classes = bundle.get("nbhd_classes", [])
            self.extra        = bundle
            log.info("Loaded %s (%d features)", self.path.name, len(self.feature_cols))
            return True
        except Exception as exc:
            log.warning("Failed to load %s: %s", self.path.name, exc)
            return False

    def predict_proba(self, enriched: dict) -> float | None:
        if not self.ensure_loaded():
            return None
        row = []
        for col in self.feature_cols:
            if col == "neighborhood_enc":
                nbhd = enriched.get("neighborhood")
                if nbhd and nbhd in self.nbhd_classes:
                    row.append(float(self.nbhd_classes.index(nbhd)))
                else:
                    row.append(np.nan)
            else:
                val = enriched.get(col)
                row.append(float(val) if val is not None else np.nan)
        try:
            proba = self.pipe.predict_proba([row])[0][1]
            return round(float(proba), 4)
        except Exception as exc:
            log.warning("%s predict_proba failed: %s", self.path.name, exc)
            return None


_completion = _LazyModel(_MODEL_PATH)
_likelihood = _LazyModel(_LIKELIHOOD_MODEL_PATH)


# ── Time-ratio feature computation (completion model only) ───────────────────

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


# ── Public API — completion model ─────────────────────────────────────────────

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
    if not _completion.ensure_loaded():
        return None
    enriched = {
        **features,
        **_compute_time_ratios(features, _completion.extra.get("duration_stats") or {}),
    }
    return _completion.predict_proba(enriched)


def completion_narrative(features: dict) -> str | None:
    """
    One-sentence, human-readable context for the completion forecast:
    how long similar projects typically take to reach Form 4, and roughly
    how much of that a project at this stage has left.

    Prefers the building's neighbourhood if it has enough completed samples
    (>= 5) to give a stable median; falls back to the citywide figure
    otherwise.  Returns None when no duration stats are available at all.
    """
    if not _completion.ensure_loaded():
        return None

    dur_stats  = _completion.extra.get("duration_stats") or {}
    nbhd_stats = _completion.extra.get("neighborhood_stats") or {}

    citywide = dur_stats.get("form1_to_form4")
    if not citywide:
        return None

    neighborhood = features.get("neighborhood")
    nbhd_entry   = nbhd_stats.get(neighborhood) if neighborhood else None

    if nbhd_entry and nbhd_entry.get("completed", 0) >= 5 and nbhd_entry.get("median_days_total"):
        total_days = nbhd_entry["median_days_total"]
        basis      = f"in {neighborhood}"
    else:
        total_days = citywide["median"]
        basis      = "citywide"

    years = total_days / 365.25
    elapsed = features.get("days_since_form1")

    if elapsed is None:
        return (
            f"Most TAMA38 projects {basis} reach Form 4 within about "
            f"{years:.1f} years of Form 1 approval."
        )

    remaining_years = max(0.0, total_days - float(elapsed)) / 365.25
    if remaining_years < 0.1:
        return (
            f"Most TAMA38 projects {basis} reach Form 4 within about "
            f"{years:.1f} years of Form 1 — this project has already passed that typical mark."
        )
    return (
        f"Most TAMA38 projects {basis} reach Form 4 within about {years:.1f} years "
        f"of Form 1 — at this stage, that typically leaves ~{remaining_years:.1f} more years."
    )


def duration_stats() -> dict:
    """Return the reference duration-stats dict (or {} if model not loaded)."""
    _completion.ensure_loaded()
    return _completion.extra.get("duration_stats") or {}


def model_meta() -> dict:
    """Return the full training metadata dict (or {} if not available)."""
    if _META_PATH.exists():
        try:
            return json.loads(_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ── Public API — likelihood model ─────────────────────────────────────────────

def predict_likelihood_proba(features: dict) -> float | None:
    """
    Predict how "TAMA38-like" a building looks, for buildings that don't have
    an active permit yet.  Trained against building age/floors, nearby permit
    density, open construction sites, and neighbourhood — the same signals the
    heuristic Likelihood Dashboard already uses, but learned from data instead
    of hand-tuned weights.

    Raw keys expected: building_year, building_floors, nearby_200m,
    nearby_500m, has_open_site, lat, lon, neighborhood (str, optional)

    Returns float in [0, 1] or None when the model is not available.
    """
    return _likelihood.predict_proba(features)
