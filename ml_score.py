"""
Thin wrapper around the trained TAMA38 completion model.

The model is loaded once on first call and cached for the life of the process.
Returns None gracefully when the model file hasn't been trained yet.
"""
import json
import logging
import pickle
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).parent / "tama_model.pkl"
_META_PATH  = Path(__file__).parent / "tama_model_meta.json"

_pipe          = None
_feature_cols  = None
_nbhd_classes  = None
_loaded        = False   # True once we've attempted a load (even if it failed)


def _load() -> bool:
    global _pipe, _feature_cols, _nbhd_classes, _loaded
    _loaded = True
    if not _MODEL_PATH.exists():
        log.debug("tama_model.pkl not found — ML scoring disabled")
        return False
    try:
        with open(_MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        _pipe         = bundle["pipeline"]
        _feature_cols = bundle["feature_cols"]
        _nbhd_classes = bundle.get("nbhd_classes", [])
        log.info("ML model loaded (%d features)", len(_feature_cols))
        return True
    except Exception as exc:
        log.warning("Failed to load tama_model.pkl: %s", exc)
        return False


def predict_completion_proba(features: dict) -> float | None:
    """
    Predict the probability that a building with an active TAMA38 permit will
    eventually complete (reach Form 4 / אכלוס).

    Parameters
    ----------
    features : dict
        Subset of the keys used during training.  Missing keys are filled with
        NaN (handled natively by HistGradientBoostingClassifier).

    Keys used (all optional at call-time):
        days_since_form1         int/float   days the permit file has been open
        days_form1_to_permit     int/float   -1 or NaN if permit not obtained yet
        days_permit_to_build     int/float   -1 or NaN if construction not started
        has_permit               0/1
        has_construction         0/1
        building_year            int
        building_floors          int
        is_track2                0/1
        lat                      float
        lon                      float
        days_form1_to_verbal     float       from archive data (NaN if missing)
        days_verbal_to_signed    float       from archive data (NaN if missing)
        neighborhood             str         neighborhood name (optional)

    Returns
    -------
    float in [0, 1] or None if model not available
    """
    global _loaded
    if not _loaded and not _load():
        return None
    if _pipe is None:
        return None

    # Build feature vector in the exact order the model expects
    row = []
    for col in _feature_cols:
        if col == "neighborhood_enc":
            nbhd = features.get("neighborhood")
            if nbhd and nbhd in _nbhd_classes:
                row.append(float(_nbhd_classes.index(nbhd)))
            else:
                row.append(np.nan)
        else:
            val = features.get(col)
            row.append(float(val) if val is not None else np.nan)

    try:
        proba = _pipe.predict_proba([row])[0][1]
        return round(float(proba), 4)
    except Exception as exc:
        log.warning("ML predict_proba failed: %s", exc)
        return None


def model_meta() -> dict:
    """Return the training metadata dict (or {} if not available)."""
    if _META_PATH.exists():
        try:
            return json.loads(_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}
