"""
Bulk-scrapes the Tel Aviv Engineering Archive for every building with a TAMA38 permit.

Workflow:
  1. Query TLV.permits + TLV.addresses to build a unique (k_rechov, ms_bayit) list
  2. Create TLV.archive_timelines if it doesn't exist
  3. For each building NOT already in the table: scrape + upsert
  4. Skip already-scraped rows on re-run (safe to run repeatedly)

Usage:
    python fetch_archive_bulk.py [--limit N] [--delay SECONDS] [--rescrape]

Options:
    --limit N         Scrape at most N buildings (handy for quick tests)
    --delay SECONDS   Pause between requests, default 2.5
    --rescrape        Re-scrape buildings already in the table
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
    k_rechov      integer   NOT NULL,
    ms_bayit      text      NOT NULL,
    form1         date,
    permit_verbal date,
    permit_signed date,
    build         date,
    form4         date,
    scraped_at    timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (k_rechov, ms_bayit)
);
"""


def _ensure_table(conn):
    conn.execute(text(_DDL))
    conn.commit()


# ── Queries ───────────────────────────────────────────────────────────────────

def _get_already_scraped(conn) -> set:
    rows = conn.execute(
        text('SELECT k_rechov, ms_bayit FROM "TLV".archive_timelines')
    ).fetchall()
    return {(int(r[0]), str(r[1])) for r in rows}


def _get_target_buildings(conn) -> list:
    """
    Return all unique (k_rechov, ms_bayit, lat, lon) for addresses that sit
    within 5 m of a TAMA38 permit geometry.
    Each building (house number) appears once.
    """
    rows = conn.execute(text("""
        SELECT DISTINCT ON (a.k_rechov, a.ms_bayit)
            a.k_rechov::integer,
            a.ms_bayit::text                           AS ms_bayit,
            ST_Y(ST_Transform(a.geometry, 4326))       AS lat,
            ST_X(ST_Transform(a.geometry, 4326))       AS lon
        FROM "TLV".addresses a
        JOIN "TLV".permits   p ON ST_DWithin(a.geometry, p.geometry, 5)
        WHERE (
            p.sw_tama_38         = 'כן'
         OR p.sw_tama_38_chadash = 'כן'
         OR p.sw_tama_38_tosefet = 'כן'
        )
        AND a.k_rechov IS NOT NULL
        AND a.ms_bayit IS NOT NULL
        ORDER BY a.k_rechov, a.ms_bayit
    """)).fetchall()
    return [(int(r[0]), str(r[1]), float(r[2]), float(r[3])) for r in rows]


def _upsert(conn, k_rechov: int, ms_bayit: str, tl: dict):
    conn.execute(text("""
        INSERT INTO "TLV".archive_timelines
            (k_rechov, ms_bayit, form1, permit_verbal, permit_signed, build, form4, scraped_at)
        VALUES (:k, :m, :f1, :pv, :ps, :b, :f4, :ts)
        ON CONFLICT (k_rechov, ms_bayit) DO UPDATE SET
            form1         = EXCLUDED.form1,
            permit_verbal = EXCLUDED.permit_verbal,
            permit_signed = EXCLUDED.permit_signed,
            build         = EXCLUDED.build,
            form4         = EXCLUDED.form4,
            scraped_at    = EXCLUDED.scraped_at
    """), {
        "k":  k_rechov,
        "m":  ms_bayit,
        "f1": tl.get("form1"),
        "pv": tl.get("permit_verbal"),
        "ps": tl.get("permit_signed"),
        "b":  tl.get("build"),
        "f4": tl.get("form4"),
        "ts": datetime.now(tz=timezone.utc),
    })
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",    type=int,   default=None)
    ap.add_argument("--delay",    type=float, default=2.5)
    ap.add_argument("--rescrape", action="store_true",
                    help="Re-scrape buildings already in the table")
    args = ap.parse_args()

    with engine.connect() as conn:
        _ensure_table(conn)
        already_done = set() if args.rescrape else _get_already_scraped(conn)
        all_targets  = _get_target_buildings(conn)

    todo = [(k, m, lat, lon) for k, m, lat, lon in all_targets
            if (k, m) not in already_done]
    if args.limit:
        todo = todo[: args.limit]

    log.info(
        "TAMA38 buildings in DB: %d | already scraped: %d | to scrape: %d",
        len(all_targets), len(already_done), len(todo),
    )

    ok = err = empty = 0
    for i, (k_rechov, ms_bayit, lat, lon) in enumerate(todo, 1):
        url = BASE_URL.format(k_rechov=k_rechov, ms_bayit=ms_bayit)
        log.info(
            "[%d/%d]  k=%s  m=%s  (%.4f, %.4f)",
            i, len(todo), k_rechov, ms_bayit, lat, lon,
        )
        try:
            tl = scrape_archive_timeline(url)
            with engine.connect() as conn:
                _upsert(conn, k_rechov, ms_bayit, tl)
            if tl:
                log.info("  → %s", tl)
                ok += 1
            else:
                log.warning("  → no milestone data found (saved empty record)")
                empty += 1
        except Exception as exc:
            log.error("  → scrape failed: %s", exc)
            err += 1

        if i < len(todo):
            time.sleep(args.delay)

    log.info("Finished.  ok=%d  empty=%d  err=%d", ok, empty, err)


if __name__ == "__main__":
    main()
