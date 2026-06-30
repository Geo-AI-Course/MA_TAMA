#!/usr/bin/env python3
"""
Ma TAMA — one-shot setup and launcher.

Checks every prerequisite in order, fills in what is missing, then starts
the web app.  Safe to re-run: each step is skipped when already up to date.

Steps
-----
1  Python version >= 3.10
2  pip dependencies (requirements.txt)
3  Playwright Chromium browser
4  PostgreSQL + PostGIS connection
5  TLV schema + meta table
6  GIS data freshness        (re-fetches if older than --refresh-days, default 7)
7  Neighborhood boundaries   (fetched once; cached in TLV.neighborhoods)
8  Engineering Archive scrape  (skipped with --skip-scrape)
9  ML model training  (skipped with --skip-train)
10 Launch app  (skipped with --skip-app)

Usage
-----
    python setup.py                      # full setup + launch
    python setup.py --skip-scrape        # skip the slow archive scrape
    python setup.py --skip-app           # setup only, do not launch Flask
    python setup.py --refresh-days 14    # tolerate 14-day-old GIS data
    python setup.py --force-refresh      # re-fetch GIS data unconditionally
    python setup.py --host 0.0.0.0 --port 5001   # custom Flask bind

Database credentials are read from environment variables if set:
    DB_HOST  DB_PORT  DB_NAME  DB_USER  DB_PASSWORD
falling back to the defaults below.
"""
import argparse
import importlib
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────

HERE = Path(__file__).parent

DB = {
    "host":     os.environ.get("DB_HOST",     "localhost"),
    "port":     int(os.environ.get("DB_PORT", 5432)),
    "database": os.environ.get("DB_NAME",     "MA_TAMA"),
    "user":     os.environ.get("DB_USER",     "postgres"),
    "password": os.environ.get("DB_PASSWORD", "mypassword"),
}

# ── Terminal helpers ───────────────────────────────────────────────────────────

# Reconfigure stdout to UTF-8 so Unicode symbols work on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def section(title):
    bar = "-" * max(0, 54 - len(title))
    print(f"\n{_c('1;36', f'-- {title} {bar}')}")


def ok(msg):    print(f"  [ok] {msg}")
def info(msg):  print(f"       {msg}")
def warn(msg):  print(f"  [!!] {msg}")
def step(msg):  print(f"  [..] {msg} ", end="", flush=True)
def done():     print("done")


def fatal(msg):
    print(f"\n  [!!] {msg}\n")
    sys.exit(1)


# ── Step 1 — Python version ───────────────────────────────────────────────────

def check_python():
    section("Python")
    v = sys.version_info
    if v < (3, 10):
        fatal(f"Python 3.10+ required, got {v.major}.{v.minor}.  "
              "Download from https://python.org")
    ok(f"Python {v.major}.{v.minor}.{v.micro}")


# ── Step 2 — pip dependencies ─────────────────────────────────────────────────

def install_deps():
    section("Dependencies")
    req = HERE / "requirements.txt"
    if not req.exists():
        warn("requirements.txt not found — skipping pip install")
        return

    missing = []
    pkg_map = {
        "flask":        "flask",
        "sqlalchemy":   "sqlalchemy",
        "psycopg2":     "psycopg2",
        "geopandas":    "geopandas",
        "requests":     "requests",
        "playwright":   "playwright",
        "sklearn":      "scikit-learn",
        "pandas":       "pandas",
        "numpy":        "numpy",
    }
    for import_name, pip_name in pkg_map.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        step(f"Installing {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
            stdout=subprocess.DEVNULL,
        )
        done()
    else:
        ok("All packages present")


# ── Step 3 — Playwright Chromium ──────────────────────────────────────────────

def install_chromium():
    section("Playwright / Chromium")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            _ = pw.chromium  # just access the attribute; no launch
        ok("Chromium available")
    except Exception:
        step("Installing Chromium browser")
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        done()


# ── Step 4 — PostgreSQL / PostGIS ─────────────────────────────────────────────

def _engine(dbname=None):
    from sqlalchemy import create_engine
    db = dbname or DB["database"]
    return create_engine(
        f"postgresql+psycopg2://{DB['user']}:{DB['password']}"
        f"@{DB['host']}:{DB['port']}/{db}"
    )


def check_db():
    section("Database")
    from sqlalchemy import text

    # Try connecting to the target database; create it if it doesn't exist
    try:
        with _engine().connect() as c:
            ver = c.execute(text("SELECT version()")).scalar()
        ok(f"Connected to {DB['database']} ({ver.split(',')[0].strip()})")
    except Exception:
        # Database might not exist — connect to 'postgres' and create it
        try:
            step(f"Creating database {DB['database']}")
            with _engine("postgres").connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as c:
                c.execute(text(f'CREATE DATABASE "{DB["database"]}"'))
            done()
        except Exception as exc:
            fatal(
                f"Cannot connect to PostgreSQL at {DB['host']}:{DB['port']}.\n\n"
                "     Make sure PostgreSQL is running and the credentials are correct.\n"
                "     Set DB_HOST / DB_PORT / DB_USER / DB_PASSWORD env vars to override.\n\n"
                f"     Error: {exc}"
            )

    # PostGIS + schema
    from sqlalchemy import text
    with _engine().connect() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        c.execute(text('CREATE SCHEMA IF NOT EXISTS "TLV"'))
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS "TLV".meta (
                key        text PRIMARY KEY,
                value      text,
                updated_at timestamp NOT NULL DEFAULT now()
            )
        """))
        c.commit()
    ok("PostGIS extension + TLV schema ready")


# ── Step 5 — GIS data freshness ───────────────────────────────────────────────

def _meta_get(conn, key: str):
    from sqlalchemy import text
    row = conn.execute(
        text('SELECT value, updated_at FROM "TLV".meta WHERE key = :k'),
        {"k": key},
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _meta_set(conn, key: str, value: str):
    from sqlalchemy import text
    conn.execute(text("""
        INSERT INTO "TLV".meta (key, value, updated_at)
        VALUES (:k, :v, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """), {"k": key, "v": value})
    conn.commit()


def _table_exists(conn, table: str) -> bool:
    from sqlalchemy import text
    return conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'TLV' AND table_name = :t
    """), {"t": table}).fetchone() is not None


def check_gis_data(refresh_days: int, force: bool):
    section("GIS data")
    from sqlalchemy import text

    with _engine().connect() as conn:
        val, updated_at = _meta_get(conn, "gis_last_fetched")

    needs_refresh = force or val is None

    if not needs_refresh and updated_at:
        age = datetime.now(tz=timezone.utc) - updated_at.replace(tzinfo=timezone.utc)
        if age > timedelta(days=refresh_days):
            needs_refresh = True
            warn(f"GIS data is {age.days} days old (threshold: {refresh_days})")
        else:
            ok(f"GIS data is {age.days} days old — up to date")

    if needs_refresh:
        step("Fetching GIS layers from Tel Aviv ArcGIS")
        print()   # newline so layer progress prints below
        sys.stdout.flush()
        # Import and run the fetch logic directly
        import fetch_tlv_addresses as ftlv
        try:
            ftlv.main()
        except Exception as exc:
            fatal(f"GIS fetch failed: {exc}")
        with _engine().connect() as conn:
            _meta_set(conn, "gis_last_fetched", date.today().isoformat())
        ok("GIS layers updated and saved")
    else:
        # Sanity-check row counts
        with _engine().connect() as conn:
            for tbl, min_rows in [("addresses", 10000), ("permits", 1000)]:
                if _table_exists(conn, tbl):
                    n = conn.execute(
                        text(f'SELECT COUNT(*) FROM "TLV".{tbl}')
                    ).scalar()
                    info(f"{tbl}: {n:,} rows")
                else:
                    warn(f"{tbl} table missing — forcing refresh")
                    check_gis_data(refresh_days, force=True)
                    return


# ── Step 6 — Neighborhood boundaries ─────────────────────────────────────────

def check_neighborhoods():
    section("Neighborhood boundaries")
    from sqlalchemy import text

    with _engine().connect() as conn:
        if _table_exists(conn, "neighborhoods"):
            n = conn.execute(
                text('SELECT COUNT(*) FROM "TLV".neighborhoods')
            ).scalar() or 0
            if n > 0:
                ok(f"neighborhoods table present ({n} polygons)")
                return
        warn("neighborhoods table missing or empty — fetching")

    step("Fetching neighborhood boundaries")
    print()
    import fetch_neighborhoods as fn
    try:
        fn.main()
        ok("Neighborhoods loaded")
    except (Exception, SystemExit):
        warn("Neighborhoods unavailable — geographic ML features will be disabled")


# ── Step 7 — Engineering Archive scrape ──────────────────────────────────────

def check_archive_scrape(skip: bool):
    section("Engineering Archive scrape")

    if skip:
        warn("Skipped (--skip-scrape).  ML timeline features will be limited.")
        return

    from sqlalchemy import text

    with _engine().connect() as conn:
        # Total TAMA38 buildings (unique k_rechov/ms_bayit pairs near permits)
        try:
            total = conn.execute(text("""
                SELECT COUNT(DISTINCT (a.k_rechov, a.ms_bayit))
                FROM "TLV".addresses a
                JOIN "TLV".permits   p ON ST_DWithin(a.geometry, p.geometry, 5)
                WHERE p.sw_tama_38 = 'כן'
                   OR p.sw_tama_38_chadash = 'כן'
                   OR p.sw_tama_38_tosefet = 'כן'
            """)).scalar() or 0
        except Exception:
            total = 0

        # Already scraped
        scraped = 0
        if _table_exists(conn, "archive_timelines"):
            scraped = conn.execute(
                text('SELECT COUNT(*) FROM "TLV".archive_timelines')
            ).scalar() or 0

    coverage = (scraped / total * 100) if total else 0
    info(f"Scraped {scraped:,} / {total:,} buildings  ({coverage:.1f}% coverage)")

    if coverage >= 95:
        ok("Archive scrape complete")
        return

    if scraped > 0:
        warn(f"Scrape is {100 - coverage:.1f}% incomplete — resuming from building {scraped + 1}")
    else:
        info("Starting fresh scrape.  This takes 4–6 hours.")
        info("You can interrupt with Ctrl+C and re-run setup.py — it will resume.")

    print()
    try:
        subprocess.run(
            [sys.executable, str(HERE / "fetch_archive_bulk.py"), "--delay", "0.5"],
            check=True,
        )
    except KeyboardInterrupt:
        print()
        warn("Scrape interrupted.  Re-run setup.py to continue from where it stopped.")
    except subprocess.CalledProcessError as exc:
        warn(f"Scrape exited with code {exc.returncode} — continuing with partial data")


# ── Step 7 — ML model training ────────────────────────────────────────────────

def check_model(skip: bool):
    section("ML model")

    if skip:
        warn("Skipped (--skip-train).  ML completion forecast will be unavailable.")
        return

    model_pkl = HERE / "tama_model.pkl"
    meta_json = HERE / "tama_model_meta.json"

    needs_train = not model_pkl.exists()

    if not needs_train:
        # Retrain if GIS data was refreshed more recently than the model
        from sqlalchemy import text
        with _engine().connect() as conn:
            gis_val, _ = _meta_get(conn, "gis_last_fetched")

        if gis_val:
            gis_date = date.fromisoformat(gis_val)
            model_date = date.fromtimestamp(model_pkl.stat().st_mtime)
            if gis_date > model_date:
                warn("GIS data updated after model was trained — retraining")
                needs_train = True

    if not needs_train:
        import json
        if meta_json.exists():
            m = json.loads(meta_json.read_text(encoding="utf-8"))
            ok(f"Model up to date  "
               f"(trained {m.get('trained_at','?')[:10]}, "
               f"ROC-AUC {m.get('cv_roc_auc','?')})")
        else:
            ok("Model file exists")
        return

    step("Training ML model")
    print()
    try:
        subprocess.run(
            [sys.executable, str(HERE / "train_tama_model.py")],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        warn(f"Model training failed (exit {exc.returncode}) — "
             "ML forecast will be unavailable until more data is collected")


# ── Step 8 — Launch app ───────────────────────────────────────────────────────

def launch_app(host: str, port: int):
    section("Launching Ma TAMA")
    print(f"\n  Open http://{host}:{port} in your browser\n")
    print("  Press Ctrl+C to stop.\n")

    env = {**os.environ, "FLASK_ENV": "development"}
    try:
        subprocess.run(
            [sys.executable, str(HERE / "app.py"), "--host", host, "--port", str(port)],
            env=env,
        )
    except KeyboardInterrupt:
        print("\n  App stopped.")


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Ma TAMA — setup and launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--refresh-days", type=int, default=7,
                   help="Re-fetch GIS data if older than N days (default: 7)")
    p.add_argument("--force-refresh", action="store_true",
                   help="Re-fetch GIS data even if fresh")
    p.add_argument("--skip-scrape", action="store_true",
                   help="Skip the Engineering Archive scrape")
    p.add_argument("--skip-train",  action="store_true",
                   help="Skip ML model training")
    p.add_argument("--skip-app",    action="store_true",
                   help="Run setup only, do not launch Flask")
    p.add_argument("--host", default="127.0.0.1",
                   help="Flask bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=5000,
                   help="Flask bind port (default: 5000)")
    return p.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    print(f"\n{_c('1', 'Ma TAMA -- Setup & Launch')}")
    print(_c("36", "=" * 56))

    check_python()
    install_deps()
    install_chromium()
    check_db()
    check_gis_data(args.refresh_days, args.force_refresh)
    check_neighborhoods()
    check_archive_scrape(args.skip_scrape)
    check_model(args.skip_train)

    if not args.skip_app:
        launch_app(args.host, args.port)
    else:
        print(f"\n{_c('32', '  Setup complete.')}  Run  python app.py  to start.\n")


if __name__ == "__main__":
    main()
