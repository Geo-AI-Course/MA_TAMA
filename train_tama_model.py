"""
Train a TAMA38 completion-probability classifier.

What it predicts
----------------
For a building that ALREADY HAS an active TAMA38 permit:
  → probability it will eventually reach Form 4 (אכלוס / occupancy)

This is distinct from the rule-based candidate score shown for buildings
with *no* permit yet.

Label definition
----------------
  1  = completed  : `finished` field is set, or building_stage in completed set
  0  = stalled    : open_request > STALE_YEARS years ago AND no permit obtained
                    (or permit > STALE_YEARS years ago with no finished)
  skip (excluded) : recent permits still within plausible completion window

Features
--------
From GIS (TLV.permits + TLV.buildings):
  days_since_form1         current age of the permit file (days)
  days_form1_to_permit     speed of initial approval (-1 if no permit yet)
  days_permit_to_build     time from permit to construction start (-1 if N/A)
  has_permit               bool: reached permit stage
  has_construction         bool: construction has started
  building_year            year built (pre-1980 = high TAMA38 suitability)
  building_floors          floor count
  is_track2                TAMA38 chadash (demolish & rebuild)
  lat, lon                 WGS84 centroid

From TLV.archive_timelines (if fetch_archive_bulk.py has been run):
  days_form1_to_verbal     form1 → verbal permit (archive data)
  days_verbal_to_signed    verbal → signed permit (archive data)

From TLV.neighborhoods (if fetch_neighborhoods.py has been run):
  neighborhood             label-encoded neighborhood name

Outputs
-------
  tama_model.pkl           sklearn Pipeline (saved with pickle)
  tama_model_meta.json     feature list, metrics, training timestamp

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

STALE_YEARS = 4          # permits older than this with no completion → label 0
MIN_SAMPLES = 20         # abort if fewer labelled samples than this

MODEL_PATH = Path(__file__).parent / "tama_model.pkl"
META_PATH  = Path(__file__).parent / "tama_model_meta.json"

engine = create_engine(
    f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
    f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
)

_COMPLETED_STAGES = {"קיים אכלוס", "קיימת לפחות תעודת גמר אחת"}

FEATURE_COLS = [
    "days_since_form1",
    "days_form1_to_permit",
    "days_permit_to_build",
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

    df["d_form1"]   = df["open_request"].apply(_unix_ms_to_date)
    df["d_permit"]  = df["permission_date"].apply(_unix_ms_to_date)
    df["d_build"]   = df["tr_hathalat_bniya"].apply(_unix_ms_to_date)
    df["d_form4"]   = df["finished"].apply(_ddmmyyyy_to_date)

    df["arch_form1"]   = df["arch_form1"].apply(_iso_to_date)
    df["arch_verbal"]  = df["arch_verbal"].apply(_iso_to_date)
    df["arch_signed"]  = df["arch_signed"].apply(_iso_to_date)
    df["arch_form4"]   = df["arch_form4"].apply(_iso_to_date)

    # Use archive dates to fill gaps in GIS dates
    df["d_form1"]  = df.apply(lambda r: r["d_form1"]  or r["arch_form1"],  axis=1)
    df["d_form4"]  = df.apply(lambda r: r["d_form4"]  or r["arch_form4"],  axis=1)

    df["days_since_form1"]      = df["d_form1"].apply(lambda d: _days(d, today) if d else np.nan)
    df["days_form1_to_permit"]  = df.apply(lambda r: _days(r["d_form1"],  r["d_permit"]), axis=1)
    df["days_permit_to_build"]  = df.apply(lambda r: _days(r["d_permit"], r["d_build"]),  axis=1)
    df["days_form1_to_verbal"]  = df.apply(lambda r: _days(r["d_form1"],  r["arch_verbal"]), axis=1)
    df["days_verbal_to_signed"] = df.apply(lambda r: _days(r["arch_verbal"], r["arch_signed"]), axis=1)

    df["has_permit"]      = df["d_permit"].notna().astype(float)
    df["has_construction"] = df["d_build"].notna().astype(float)

    try:
        df["building_year"]   = pd.to_numeric(df["building_year"],   errors="coerce")
        df["building_floors"] = pd.to_numeric(df["building_floors"], errors="coerce")
    except Exception:
        pass

    df["is_track2"] = (df["sw_tama_38_chadash"] == "כן").astype(float)
    df["lat"]  = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"]  = pd.to_numeric(df["lon"], errors="coerce")

    # Label
    df["is_completed"] = (
        df["building_stage"].isin(_COMPLETED_STAGES) | df["d_form4"].notna()
    )

    stale_days = STALE_YEARS * 365
    df["is_stalled"] = (
        ~df["is_completed"] &
        df["days_since_form1"].gt(stale_days)
    )

    return df


# ── Neighbourhood analysis ────────────────────────────────────────────────────

def neighborhood_analysis(df: pd.DataFrame) -> pd.DataFrame:
    if df["neighborhood"].isna().all():
        log.warning("No neighborhood data — run fetch_neighborhoods.py first")
        return pd.DataFrame()

    grp = df.dropna(subset=["neighborhood"]).copy()
    grp["label_known"] = grp["is_completed"] | grp["is_stalled"]
    grp = grp[grp["label_known"]]

    stats = (
        grp.groupby("neighborhood")
        .agg(
            total         = ("is_completed", "count"),
            completed     = ("is_completed", "sum"),
            avg_days_form1_to_permit = ("days_form1_to_permit", "mean"),
            avg_days_form1_to_form4  = ("days_since_form1",     "mean"),
        )
        .assign(completion_rate=lambda d: (d["completed"] / d["total"]).round(3))
        .sort_values("completion_rate", ascending=False)
        .reset_index()
    )
    log.info("\n=== Neighbourhood Analysis ===\n%s", stats.to_string(index=False))
    return stats


# ── Model training ────────────────────────────────────────────────────────────

def train(X: pd.DataFrame, y: pd.Series) -> Pipeline:
    # HistGradientBoostingClassifier handles NaN natively — no imputation needed
    clf = HistGradientBoostingClassifier(
        max_iter=300,
        max_depth=5,
        learning_rate=0.05,
        min_samples_leaf=10,
        random_state=42,
        class_weight="balanced",
    )
    pipe = Pipeline([("clf", clf)])

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    roc_scores  = cross_val_score(pipe, X, y, cv=cv, scoring="roc_auc")
    acc_scores  = cross_val_score(pipe, X, y, cv=cv, scoring="accuracy")

    log.info(
        "Cross-validation  ROC-AUC: %.3f ± %.3f   Accuracy: %.3f ± %.3f",
        roc_scores.mean(), roc_scores.std(),
        acc_scores.mean(), acc_scores.std(),
    )

    pipe.fit(X, y)
    return pipe


def feature_importances(pipe: Pipeline, feature_cols: list[str]) -> dict:
    clf = pipe.named_steps["clf"]
    imps = {}
    if hasattr(clf, "feature_importances_"):
        for name, imp in zip(feature_cols, clf.feature_importances_):
            imps[name] = round(float(imp), 4)
        ranked = sorted(imps.items(), key=lambda x: -x[1])
        log.info("Feature importances:\n%s",
                 "\n".join(f"  {n:<30s} {v:.4f}" for n, v in ranked))
    return imps


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    with engine.connect() as conn:
        raw = load_raw(conn)

    df = engineer(raw)

    # Labelled subset
    labelled = df[df["is_completed"] | df["is_stalled"]].copy()
    labelled["label"] = labelled["is_completed"].astype(int)

    log.info(
        "Labelled samples — completed: %d  stalled: %d  total: %d",
        labelled["label"].sum(),
        (labelled["label"] == 0).sum(),
        len(labelled),
    )

    if len(labelled) < MIN_SAMPLES:
        log.error(
            "Only %d labelled samples — not enough to train a reliable model.\n"
            "Run fetch_archive_bulk.py to collect more data first.",
            len(labelled),
        )
        sys.exit(1)

    # Neighbourhood analysis (informational only)
    neighborhood_analysis(df)

    # Feature matrix
    X = labelled[FEATURE_COLS].copy()
    y = labelled["label"]

    # Encode neighborhood as an additional ordinal feature if present
    if "neighborhood" in labelled.columns and not labelled["neighborhood"].isna().all():
        le = LabelEncoder()
        X = X.copy()
        X["neighborhood_enc"] = le.fit_transform(
            labelled["neighborhood"].fillna("Unknown")
        ).astype(float)
        used_features = FEATURE_COLS + ["neighborhood_enc"]
        nbhd_classes = list(le.classes_)
    else:
        used_features = FEATURE_COLS
        nbhd_classes  = []

    pipe = train(X[used_features], y)
    imps = feature_importances(pipe, used_features)

    # Save model
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"pipeline": pipe, "feature_cols": used_features, "nbhd_classes": nbhd_classes}, f)

    meta = {
        "feature_cols":   used_features,
        "nbhd_classes":   nbhd_classes,
        "trained_at":     datetime.utcnow().isoformat(),
        "n_completed":    int(labelled["label"].sum()),
        "n_stalled":      int((labelled["label"] == 0).sum()),
        "importances":    imps,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    log.info("Saved model → %s", MODEL_PATH)
    log.info("Saved meta  → %s", META_PATH)


if __name__ == "__main__":
    main()
