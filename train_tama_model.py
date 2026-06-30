"""
Train a TAMA38 completion-probability classifier.

What it predicts
----------------
For a building that ALREADY HAS an active TAMA38 permit:
  → probability it will eventually reach Form 4 (אכלוס / occupancy)

Label definition
----------------
  1  = completed  : `finished` is set, or building_stage in completed set
  0  = stalled    : days_since_form1 > stale_threshold (data-driven from p90
                    of completed buildings, with 30% grace period)
  skip            : recent permits still within plausible completion window

Features
--------
Base (GIS + buildings):
  days_since_form1         how long the file has been open (days)
  days_form1_to_permit     speed of initial approval (NaN if no permit yet)
  days_permit_to_build     permit → construction start (NaN if N/A)
  days_since_last_milestone  days since most recent recorded milestone
  has_permit               0/1
  has_construction         0/1
  building_year
  building_floors
  is_track2                TAMA38 chadash
  lat, lon

Time-ratio (derived from duration stats of completed buildings):
  progress_pct          days_since_form1 / median(form1→form4) — how "overdue"
  permit_speed_ratio    days_form1_to_permit / median(form1→permit) — fast/slow
  days_past_p75_total   max(0, days_since_form1 - p75(form1→form4))

Archive (if fetch_archive_bulk.py has run):
  days_form1_to_verbal
  days_verbal_to_signed

Neighborhood (if fetch_neighborhoods.py has run):
  neighborhood_enc      label-encoded

Outputs
-------
  tama_model.pkl       {"pipeline", "feature_cols", "nbhd_classes"}
  tama_model_meta.json  feature list, duration_stats, CV metrics, importances

Usage
-----
    python train_tama_model.py
"""
import json
import logging
import pickle
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────

POSTGIS = {
    "host":     "localhost",
    "port":     5432,
    "database": "MA_TAMA",
    "user":     "postgres",
    "password": "mypassword",
}

FALLBACK_STALE_YEARS = 5   # used only when no completed buildings exist yet
MIN_SAMPLES          = 20

MODEL_PATH = Path(__file__).parent / "tama_model.pkl"
META_PATH  = Path(__file__).parent / "tama_model_meta.json"

engine = create_engine(
    f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
    f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
)

_COMPLETED_STAGES = {"קיים אכלוס", "קיימת לפחות תעודת גמר אחת"}

# Features that don't require the duration stats — computed unconditionally
_BASE_FEATURES = [
    "days_since_form1",
    "days_form1_to_permit",
    "days_permit_to_build",
    "days_since_last_milestone",
    "has_permit",
    "has_construction",
    "building_year",
    "building_floors",
    "is_track2",
    "lat",
    "lon",
    "days_form1_to_verbal",
    "days_verbal_to_signed",
]

# Ratio features added after duration stats are known
_TIME_RATIO_FEATURES = [
    "progress_pct",         # days_since_form1 / median(form1→form4)
    "permit_speed_ratio",   # days_form1_to_permit / median(form1→permit)
    "days_past_p75_total",  # max(0, days_since_form1 - p75(form1→form4))
]


# ── Date helpers ───────────────────────────────────────────────────────────────

def _unix_ms_to_date(val) -> date | None:
    if val is None:
        return None
    try:
        return datetime.fromtimestamp(float(val) / 1000, tz=timezone.utc).date()
    except Exception:
        return None


def _ddmmyyyy_to_date(val) -> date | None:
    if not val:
        return None
    first = str(val).split(",")[0].strip()
    try:
        return datetime.strptime(first, "%d/%m/%Y").date()
    except ValueError:
        return None


def _days(a: date | None, b: date | None) -> float:
    if a is None or b is None:
        return np.nan
    return float((b - a).days)


def _iso_to_date(val) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(str(val))
    except ValueError:
        return None


# ── Data loading ──────────────────────────────────────────────────────────────

def _has_table(conn, schema: str, table: str) -> bool:
    r = conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = :s AND table_name = :t
    """), {"s": schema, "t": table}).fetchone()
    return r is not None


def load_raw(conn) -> pd.DataFrame:
    has_archive = _has_table(conn, "TLV", "archive_timelines")
    has_nbhd    = _has_table(conn, "TLV", "neighborhoods")

    archive_join = """
        LEFT JOIN "TLV".archive_timelines at2
            ON a.k_rechov::integer = at2.k_rechov
           AND a.ms_bayit::text    = at2.ms_bayit
    """ if has_archive else ""

    archive_cols = """
        at2.form1         AS arch_form1,
        at2.permit_verbal AS arch_verbal,
        at2.permit_signed AS arch_signed,
        at2.form4         AS arch_form4,
    """ if has_archive else """
        NULL AS arch_form1,
        NULL AS arch_verbal,
        NULL AS arch_signed,
        NULL AS arch_form4,
    """

    nbhd_join = """
        LEFT JOIN "TLV".neighborhoods n
            ON ST_Within(p.geometry, n.geometry)
    """ if has_nbhd else ""

    nbhd_col = "n.shem_shkuna AS neighborhood," if has_nbhd else "NULL AS neighborhood,"

    sql = f"""
        SELECT DISTINCT ON (p.ctid)
            p.open_request,
            p.permission_date,
            p.tr_hathalat_bniya,
            p.finished,
            p.building_stage,
            p.sw_tama_38_chadash,
            b.year        AS building_year,
            b.ms_komot    AS building_floors,
            ST_Y(ST_Transform(p.geometry, 4326)) AS lat,
            ST_X(ST_Transform(p.geometry, 4326)) AS lon,
            {nbhd_col}
            {archive_cols}
            a.k_rechov,
            a.ms_bayit
        FROM "TLV".permits p
        LEFT JOIN "TLV".buildings b
            ON ST_DWithin(p.geometry, b.geometry, 2)
        LEFT JOIN "TLV".addresses a
            ON ST_DWithin(p.geometry, a.geometry, 5)
        {archive_join}
        {nbhd_join}
        WHERE (
            p.sw_tama_38         = 'כן'
         OR p.sw_tama_38_chadash = 'כן'
         OR p.sw_tama_38_tosefet = 'כן'
        )
        ORDER BY p.ctid, ST_Distance(p.geometry, COALESCE(a.geometry, p.geometry))
    """
    df = pd.read_sql(text(sql), conn)
    log.info("Loaded %d raw permit rows", len(df))
    return df


# ── Feature engineering ────────────────────────────────────────────────────────

def engineer(df: pd.DataFrame) -> pd.DataFrame:
    today = date.today()

    df["d_form1"]  = df["open_request"].apply(_unix_ms_to_date)
    df["d_permit"] = df["permission_date"].apply(_unix_ms_to_date)
    df["d_build"]  = df["tr_hathalat_bniya"].apply(_unix_ms_to_date)
    df["d_form4"]  = df["finished"].apply(_ddmmyyyy_to_date)

    df["arch_form1"]  = df["arch_form1"].apply(_iso_to_date)
    df["arch_verbal"] = df["arch_verbal"].apply(_iso_to_date)
    df["arch_signed"] = df["arch_signed"].apply(_iso_to_date)
    df["arch_form4"]  = df["arch_form4"].apply(_iso_to_date)

    # Fill GIS gaps from archive
    df["d_form1"] = df.apply(lambda r: r["d_form1"] or r["arch_form1"], axis=1)
    df["d_form4"] = df.apply(lambda r: r["d_form4"] or r["arch_form4"], axis=1)

    # Base durations
    df["days_since_form1"]      = df["d_form1"].apply(lambda d: _days(d, today) if d else np.nan)
    df["days_form1_to_permit"]  = df.apply(lambda r: _days(r["d_form1"], r["d_permit"]), axis=1)
    df["days_permit_to_build"]  = df.apply(lambda r: _days(r["d_permit"], r["d_build"]),  axis=1)
    df["days_form1_to_form4"]   = df.apply(lambda r: _days(r["d_form1"], r["d_form4"]),   axis=1)
    df["days_form1_to_verbal"]  = df.apply(lambda r: _days(r["d_form1"], r["arch_verbal"]), axis=1)
    df["days_verbal_to_signed"] = df.apply(lambda r: _days(r["arch_verbal"], r["arch_signed"]), axis=1)

    # Time since most recent recorded milestone (activity recency)
    def _last_milestone_days(row) -> float:
        if row["d_build"]:
            return _days(row["d_build"], today)
        if row["d_permit"]:
            return _days(row["d_permit"], today)
        if row["d_form1"]:
            return _days(row["d_form1"], today)
        return np.nan

    df["days_since_last_milestone"] = df.apply(_last_milestone_days, axis=1)

    df["has_permit"]      = df["d_permit"].notna().astype(float)
    df["has_construction"] = df["d_build"].notna().astype(float)

    df["building_year"]   = pd.to_numeric(df["building_year"],   errors="coerce")
    df["building_floors"] = pd.to_numeric(df["building_floors"], errors="coerce")
    df["is_track2"]       = (df["sw_tama_38_chadash"] == "כן").astype(float)
    df["lat"]             = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"]             = pd.to_numeric(df["lon"], errors="coerce")

    # Completion flag
    df["is_completed"] = (
        df["building_stage"].isin(_COMPLETED_STAGES) | df["d_form4"].notna()
    )

    return df


# ── Duration statistics ───────────────────────────────────────────────────────

def compute_duration_stats(df: pd.DataFrame) -> dict:
    """
    Compute percentile statistics from completed buildings only.
    These become the reference distribution for normalizing time-ratio features.
    """
    comp = df[df["is_completed"]].copy()
    n    = len(comp)

    def _stat(series: pd.Series) -> dict | None:
        valid = series.dropna()
        if len(valid) < 5:
            return None
        return {
            "n":      int(len(valid)),
            "median": round(float(valid.median()), 1),
            "mean":   round(float(valid.mean()),   1),
            "std":    round(float(valid.std()),    1),
            "p25":    round(float(valid.quantile(0.25)), 1),
            "p75":    round(float(valid.quantile(0.75)), 1),
            "p90":    round(float(valid.quantile(0.90)), 1),
        }

    stats = {k: v for k, v in {
        "form1_to_form4":    _stat(comp["days_form1_to_form4"]),
        "form1_to_permit":   _stat(comp["days_form1_to_permit"]),
        "permit_to_form4":   _stat(
            (comp["d_form4"] - comp["d_permit"]).dt.days.apply(
                lambda x: float(x) if pd.notna(x) else np.nan
            ) if hasattr(comp["d_form4"], "dt") else
            comp.apply(lambda r: _days(r["d_permit"], r["d_form4"]), axis=1)
        ),
        "permit_to_build":   _stat(comp["days_permit_to_build"]),
        "form1_to_verbal":   _stat(comp["days_form1_to_verbal"]),
        "verbal_to_signed":  _stat(comp["days_verbal_to_signed"]),
    }.items() if v is not None}

    _print_duration_stats(stats, n)
    return stats


def _print_duration_stats(stats: dict, n_completed: int):
    _LABELS = {
        "form1_to_permit":   "Form 1 → Permit",
        "permit_to_build":   "Permit → Construction start",
        "permit_to_form4":   "Permit → Form 4",
        "form1_to_form4":    "Form 1 → Form 4  (total)",
        "form1_to_verbal":   "Form 1 → Verbal permit  (archive)",
        "verbal_to_signed":  "Verbal → Signed permit  (archive)",
    }
    _ORDER = [
        "form1_to_permit", "permit_to_build", "permit_to_form4",
        "form1_to_form4", "form1_to_verbal", "verbal_to_signed",
    ]

    lines = [
        f"\n{'─'*72}",
        f"  TAMA38 Duration Statistics  (N = {n_completed} completed buildings)",
        f"{'─'*72}",
        f"  {'Stage':<40s} {'Median':>7} {'Mean':>7} {'P25':>7} {'P75':>7} {'P90':>7}",
        f"  {'─'*40} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*7}",
    ]
    for key in _ORDER:
        if key not in stats:
            continue
        s   = stats[key]
        lbl = _LABELS.get(key, key)

        def _fmt(v):
            if v >= 365:
                return f"{v/365:.1f}y"
            return f"{int(v)}d"

        lines.append(
            f"  {lbl:<40s} {_fmt(s['median']):>7} {_fmt(s['mean']):>7} "
            f"{_fmt(s['p25']):>7} {_fmt(s['p75']):>7} {_fmt(s['p90']):>7}"
        )
    lines.append(f"{'─'*72}\n")
    log.info("\n".join(lines))


def add_time_ratio_features(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """
    Add three normalised time features using the reference distribution
    of completed buildings.  These features capture WHERE each building
    sits relative to the typical timeline — the core of the time-factor ask.
    """
    df = df.copy()

    # ── progress_pct ─────────────────────────────────────────────────────────
    # 0 = just submitted, 1 = at median completion time, >1 = overdue
    if "form1_to_form4" in stats:
        med = stats["form1_to_form4"]["median"]
        df["progress_pct"] = (df["days_since_form1"] / med).clip(0, 3.0)
    else:
        df["progress_pct"] = np.nan

    # ── permit_speed_ratio ───────────────────────────────────────────────────
    # <1 = faster than median, >1 = slower.  NaN if no permit yet.
    if "form1_to_permit" in stats:
        med = stats["form1_to_permit"]["median"]
        df["permit_speed_ratio"] = df["days_form1_to_permit"] / med
    else:
        df["permit_speed_ratio"] = np.nan

    # ── days_past_p75_total ──────────────────────────────────────────────────
    # Days elapsed beyond the p75 typical total duration.
    # 0 means still within normal range; large values signal prolonged stall.
    if "form1_to_form4" in stats:
        p75 = stats["form1_to_form4"]["p75"]
        df["days_past_p75_total"] = (df["days_since_form1"] - p75).clip(lower=0)
    else:
        df["days_past_p75_total"] = np.nan

    return df


# ── Label definition (data-driven stale threshold) ────────────────────────────

def assign_labels(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """
    Stale threshold = p90(form1→form4) × 1.3  — 30% beyond the very longest
    typical project.  Falls back to FALLBACK_STALE_YEARS if stats unavailable.
    """
    if "form1_to_form4" in stats:
        stale_days = stats["form1_to_form4"]["p90"] * 1.3
        log.info(
            "Stale threshold: %.0f days (p90=%.0f × 1.3)",
            stale_days, stats["form1_to_form4"]["p90"],
        )
    else:
        stale_days = FALLBACK_STALE_YEARS * 365
        log.info("Stale threshold: %.0f days (fallback — no stats yet)", stale_days)

    df = df.copy()
    df["is_stalled"] = (
        ~df["is_completed"] &
        df["days_since_form1"].gt(stale_days)
    )
    return df


# ── Neighbourhood analysis ────────────────────────────────────────────────────

def neighborhood_analysis(df: pd.DataFrame) -> pd.DataFrame:
    if "neighborhood" not in df.columns or df["neighborhood"].isna().all():
        log.warning("No neighborhood data — run fetch_neighborhoods.py first")
        return pd.DataFrame()

    grp = df.dropna(subset=["neighborhood"]).copy()
    grp = grp[grp["is_completed"] | grp["is_stalled"]]

    if grp.empty:
        return pd.DataFrame()

    stats = (
        grp.groupby("neighborhood")
        .agg(
            total                    = ("is_completed", "count"),
            completed                = ("is_completed", "sum"),
            median_days_form1_permit = ("days_form1_to_permit",  "median"),
            median_days_total        = ("days_form1_to_form4",   "median"),
        )
        .assign(completion_rate=lambda d: (d["completed"] / d["total"]).round(3))
        .sort_values("completion_rate", ascending=False)
        .reset_index()
    )
    log.info("\n=== Neighbourhood Analysis ===\n%s", stats.to_string(index=False))
    return stats


# ── Model training ────────────────────────────────────────────────────────────

def train(X: pd.DataFrame, y: pd.Series) -> Pipeline:
    clf = HistGradientBoostingClassifier(
        max_iter=300,
        max_depth=5,
        learning_rate=0.05,
        min_samples_leaf=10,
        random_state=42,
        class_weight="balanced",
    )
    pipe = Pipeline([("clf", clf)])

    cv         = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    roc_scores = cross_val_score(pipe, X, y, cv=cv, scoring="roc_auc")
    acc_scores = cross_val_score(pipe, X, y, cv=cv, scoring="accuracy")

    log.info(
        "Cross-validation  ROC-AUC: %.3f ± %.3f   Accuracy: %.3f ± %.3f",
        roc_scores.mean(), roc_scores.std(),
        acc_scores.mean(), acc_scores.std(),
    )

    pipe.fit(X, y)
    return pipe, float(roc_scores.mean()), float(acc_scores.mean())


def feature_importances(pipe: Pipeline, feature_cols: list[str]) -> dict:
    clf  = pipe.named_steps["clf"]
    imps = {}
    if hasattr(clf, "feature_importances_"):
        for name, imp in zip(feature_cols, clf.feature_importances_):
            imps[name] = round(float(imp), 4)
        ranked = sorted(imps.items(), key=lambda x: -x[1])
        log.info("Feature importances:\n%s",
                 "\n".join(f"  {n:<35s} {v:.4f}" for n, v in ranked))
    return imps


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    with engine.connect() as conn:
        raw = load_raw(conn)

    df = engineer(raw)

    # Duration statistics — computed from completed buildings
    dur_stats = compute_duration_stats(df)

    # Time-ratio features (require stats)
    df = add_time_ratio_features(df, dur_stats)

    # Data-driven labels
    df = assign_labels(df, dur_stats)

    labelled = df[df["is_completed"] | df["is_stalled"]].copy()
    labelled["label"] = labelled["is_completed"].astype(int)

    log.info(
        "Labelled samples — completed: %d  stalled: %d  total: %d",
        int(labelled["label"].sum()),
        int((labelled["label"] == 0).sum()),
        len(labelled),
    )

    if len(labelled) < MIN_SAMPLES:
        log.error(
            "Only %d labelled samples — not enough to train reliably.\n"
            "Run fetch_archive_bulk.py to collect more data first.",
            len(labelled),
        )
        sys.exit(1)

    # Neighbourhood analysis
    neighborhood_analysis(df)

    # Full feature set
    used_features = _BASE_FEATURES + _TIME_RATIO_FEATURES

    # Encode neighbourhood if available
    nbhd_classes = []
    if "neighborhood" in labelled.columns and not labelled["neighborhood"].isna().all():
        le = LabelEncoder()
        nbhd_enc = le.fit_transform(labelled["neighborhood"].fillna("Unknown")).astype(float)
        labelled = labelled.copy()
        labelled["neighborhood_enc"] = nbhd_enc
        used_features = used_features + ["neighborhood_enc"]
        nbhd_classes  = list(le.classes_)

    X = labelled[used_features].copy()
    y = labelled["label"]

    pipe, roc_auc, accuracy = train(X, y)
    imps = feature_importances(pipe, used_features)

    # Save model
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({
            "pipeline":      pipe,
            "feature_cols":  used_features,
            "nbhd_classes":  nbhd_classes,
            "duration_stats": dur_stats,
        }, f)

    meta = {
        "feature_cols":   used_features,
        "nbhd_classes":   nbhd_classes,
        "duration_stats": dur_stats,
        "trained_at":     datetime.utcnow().isoformat(),
        "n_completed":    int(labelled["label"].sum()),
        "n_stalled":      int((labelled["label"] == 0).sum()),
        "cv_roc_auc":     round(roc_auc, 4),
        "cv_accuracy":    round(accuracy, 4),
        "importances":    imps,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    log.info("Saved model → %s", MODEL_PATH)
    log.info("Saved meta  → %s", META_PATH)


if __name__ == "__main__":
    main()
