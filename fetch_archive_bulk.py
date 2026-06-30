"""
Bulk-scrapes the Tel Aviv Engineering Archive for every building with a TAMA38 permit.

Workflow:
  1. Query TLV.permits + TLV.addresses to build a unique (k_rechov, ms_bayit) list
  2. Create TLV.archive_timelines if it doesn't exist
  3. For each building NOT already in the table: scrape + upsert
  4. Skip already-scraped rows on re-run (safe to run repeatedly)

Usage:
    python fetch_archive_bulk.py [--limit N] [--delay SECONDS] [--rescrape] [--update]

Options:
    --limit N         Scrape at most N buildings (handy for quick tests)
    --delay SECONDS   Pause between requests, default 2.5
    --rescrape        Re-scrape ALL buildings regardless of prior status
    --update          Smart re-scrape: only buildings whose GIS permit status
                      has changed since the last scrape (uses gis_fingerprint).
                      New (never-scraped) buildings are always included.
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from scrape_archive import scrape_archive_timeline

# ── Config ────────────────────────────────────────────────────────────────────

POSTGIS = {
    "host":     "localhost",
    "port":     5432,
    "database": "MA_TAMA",
    "user":     "postgres",
    "password": "mypassword",
}

BASE_URL = (
    "https://handasa.tel-aviv.gov.il/Pages/SearchResultsAnonPageNew.aspx"
    "?partialAddress={k_rechov}_{ms_bayit}"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fetch_archive_bulk.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

engine = create_engine(
    f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
    f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS "TLV".archive_timelines (
    k_rechov        integer   NOT NULL,
    ms_bayit        text      NOT NULL,
    form1           date,
    permit_verbal   date,
    permit_signed   date,
    build           date,
    form4           date,
    scraped_at      timestamp NOT NULL DEFAULT now(),
    gis_fingerprint text,
    PRIMARY KEY (k_rechov, ms_bayit)
);
"""

_MIGRATE = """
ALTER TABLE "TLV".archive_timelines
    ADD COLUMN IF NOT EXISTS gis_fingerprint text;
"""


def _ensure_table(conn):
    conn.execute(text(_DDL))
    conn.execute(text(_MIGRATE))
    conn.commit()


# ── Queries ───────────────────────────────────────────────────────────────────

# Fingerprint = MD5 of the most-recent permit's key milestone fields.
# If any of these change in the GIS data the building will be re-scraped.
_FINGERPRINT_EXPR = """
    md5(
        COALESCE(p.building_stage,          '') ||
        COALESCE(p.open_request::text,      '') ||
        COALESCE(p.permission_date::text,   '') ||
        COALESCE(p.tr_hathalat_bniya::text, '') ||
        COALESCE(p.finished::text,          '')
    )
"""

_TARGET_QUERY = f"""
    SELECT DISTINCT ON (a.k_rechov, a.ms_bayit)
        a.k_rechov::integer                        AS k_rechov,
        a.ms_bayit::text                           AS ms_bayit,
        ST_Y(ST_Transform(a.geometry, 4326))       AS lat,
        ST_X(ST_Transform(a.geometry, 4326))       AS lon,
        {_FINGERPRINT_EXPR}                        AS gis_fingerprint
    FROM "TLV".addresses a
    JOIN "TLV".permits   p ON ST_DWithin(a.geometry, p.geometry, 5)
    WHERE (
        p.sw_tama_38         = 'כן'
     OR p.sw_tama_38_chadash = 'כן'
     OR p.sw_tama_38_tosefet = 'כן'
    )
    AND a.k_rechov IS NOT NULL
    AND a.ms_bayit IS NOT NULL
    ORDER BY a.k_rechov, a.ms_bayit, p.open_request DESC NULLS LAST
"""


def _get_already_scraped(conn) -> set:
    rows = conn.execute(
        text('SELECT k_rechov, ms_bayit FROM "TLV".archive_timelines')
    ).fetchall()
    return {(int(r[0]), str(r[1])) for r in rows}


def _get_target_buildings(conn) -> list:
    """All TAMA38 buildings with their current GIS fingerprint."""
    rows = conn.execute(text(_TARGET_QUERY)).fetchall()
    return [(int(r[0]), str(r[1]), float(r[2]), float(r[3]), str(r[4])) for r in rows]


def _get_changed_buildings(conn) -> list:
    """
    Buildings that need re-scraping in --update mode:
      - never scraped before (new in GIS since last run)
      - fingerprint changed (permit milestone dates updated in GIS)
      - fingerprint not yet stored (scraped before this feature was added)

    Returns list of (k_rechov, ms_bayit, lat, lon, gis_fingerprint, reason).
    """
    rows = conn.execute(text(f"""
        WITH current AS ({_TARGET_QUERY})
        SELECT
            c.k_rechov, c.ms_bayit, c.lat, c.lon, c.gis_fingerprint,
            CASE
                WHEN t.k_rechov IS NULL     THEN 'new'
                WHEN t.gis_fingerprint IS NULL THEN 'no_fingerprint'
                ELSE 'changed'
            END AS reason
        FROM current c
        LEFT JOIN "TLV".archive_timelines t
               ON t.k_rechov = c.k_rechov
              AND t.ms_bayit  = c.ms_bayit
        WHERE t.k_rechov         IS NULL
           OR t.gis_fingerprint  IS NULL
           OR t.gis_fingerprint != c.gis_fingerprint
    """)).fetchall()
    return [(int(r[0]), str(r[1]), float(r[2]), float(r[3]), str(r[4]), str(r[5]))
            for r in rows]


def _upsert(conn, k_rechov: int, ms_bayit: str, tl: dict, gis_fingerprint: str | None = None):
    conn.execute(text("""
        INSERT INTO "TLV".archive_timelines
            (k_rechov, ms_bayit, form1, permit_verbal, permit_signed, build, form4,
             scraped_at, gis_fingerprint)
        VALUES (:k, :m, :f1, :pv, :ps, :b, :f4, :ts, :fp)
        ON CONFLICT (k_rechov, ms_bayit) DO UPDATE SET
            form1           = EXCLUDED.form1,
            permit_verbal   = EXCLUDED.permit_verbal,
            permit_signed   = EXCLUDED.permit_signed,
            build           = EXCLUDED.build,
            form4           = EXCLUDED.form4,
            scraped_at      = EXCLUDED.scraped_at,
            gis_fingerprint = EXCLUDED.gis_fingerprint
    """), {
        "k":  k_rechov,
        "m":  ms_bayit,
        "f1": tl.get("form1"),
        "pv": tl.get("permit_verbal"),
        "ps": tl.get("permit_signed"),
        "b":  tl.get("build"),
        "f4": tl.get("form4"),
        "ts": datetime.now(tz=timezone.utc),
        "fp": gis_fingerprint,
    })
    conn.commit()


def _write_meta(conn, key: str, value: str):
    conn.execute(text("""
        INSERT INTO "TLV".meta (key, value, updated_at)
        VALUES (:k, :v, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """), {"k": key, "v": value})
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Bulk-scrape the Tel Aviv Engineering Archive.")
    ap.add_argument("--limit",    type=int,   default=None,
                    help="Stop after N buildings (for testing)")
    ap.add_argument("--delay",    type=float, default=2.5,
                    help="Seconds to wait between requests (default 2.5)")
    ap.add_argument("--rescrape", action="store_true",
                    help="Re-scrape ALL buildings regardless of prior status")
    ap.add_argument("--update",   action="store_true",
                    help="Smart re-scrape: only new or GIS-changed buildings")
    args = ap.parse_args()

    with engine.connect() as conn:
        _ensure_table(conn)

        if args.update:
            changed = _get_changed_buildings(conn)
            all_targets = _get_target_buildings(conn)
            new_count     = sum(1 for *_, reason in changed if reason == "new")
            changed_count = sum(1 for *_, reason in changed if reason == "changed")
            no_fp_count   = sum(1 for *_, reason in changed if reason == "no_fingerprint")
            log.info(
                "Update mode | total buildings: %d | new: %d | changed: %d | missing fingerprint: %d",
                len(all_targets), new_count, changed_count, no_fp_count,
            )
            # Build todo list: (k, m, lat, lon, fingerprint)
            todo_raw = [(k, m, lat, lon, fp) for k, m, lat, lon, fp, _ in changed]
        else:
            all_targets  = _get_target_buildings(conn)
            already_done = set() if args.rescrape else _get_already_scraped(conn)
            todo_raw = [(k, m, lat, lon, fp) for k, m, lat, lon, fp in all_targets
                        if (k, m) not in already_done]
            log.info(
                "TAMA38 buildings in DB: %d | already scraped: %d | to scrape: %d",
                len(all_targets), len(all_targets) - len(todo_raw), len(todo_raw),
            )

    if args.limit:
        todo_raw = todo_raw[: args.limit]

    ok = err = empty = 0
    for i, (k_rechov, ms_bayit, lat, lon, fingerprint) in enumerate(todo_raw, 1):
        url = BASE_URL.format(k_rechov=k_rechov, ms_bayit=ms_bayit)
        log.info(
            "[%d/%d]  k=%s  m=%s  (%.4f, %.4f)",
            i, len(todo_raw), k_rechov, ms_bayit, lat, lon,
        )
        try:
            tl = scrape_archive_timeline(url)
            with engine.connect() as conn:
                _upsert(conn, k_rechov, ms_bayit, tl, gis_fingerprint=fingerprint)
            if tl:
                log.info("  -> %s", tl)
                ok += 1
            else:
                log.warning("  -> no milestone data found (saved empty record)")
                empty += 1
        except Exception as exc:
            log.error("  -> scrape failed: %s", exc)
            err += 1

        if i < len(todo_raw):
            time.sleep(args.delay)

    log.info("Finished.  ok=%d  empty=%d  err=%d", ok, empty, err)

    # Record completion time so setup.py / check_archive_scrape() can assess freshness
    with engine.connect() as conn:
        _write_meta(conn, "archive_last_scraped", datetime.now(tz=timezone.utc).isoformat())


if __name__ == "__main__":
    main()
