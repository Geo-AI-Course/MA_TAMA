"""
Ma TAMA – Flask backend
Endpoints:
  GET /                              – serve UI
  GET /api/autocomplete/streets      – street name suggestions
  GET /api/autocomplete/buildings    – building number suggestions
  GET /api/search                    – address → building polygon + TAMA38 analysis
  GET /api/nearby_permits            – TAMA38 permit polygons within 500 m
"""
import json
import logging

import requests as _requests
from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, text

import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys
from datetime import date as _date

from tama_score import compute_tama_score
from ml_score import predict_completion_proba, duration_stats as _ml_duration_stats

_WORKER = _os.path.join(_os.path.dirname(__file__), "_scrape_worker.py")

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

POSTGIS = {
    "host": "localhost",
    "port": 5432,
    "database": "MA_TAMA",
    "user": "postgres",
    "password": "mypassword",
    "schema": "TLV",
}

engine = create_engine(
    f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
    f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
)


def _row_to_dict(row) -> dict:
    """Serialize a SQLAlchemy Row to a plain dict, converting datetimes to ISO strings."""
    result = {}
    for k, v in row._mapping.items():
        result[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return result


def _ts_to_iso(val) -> str | None:
    """Convert a Unix-ms timestamp (int or float) to an ISO date string."""
    if val is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(val) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError, TypeError):
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/autocomplete/streets")
def autocomplete_streets():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT DISTINCT t_rechov FROM "TLV".addresses '
                 "WHERE t_rechov ILIKE :q ORDER BY t_rechov LIMIT 10"),
            {"q": f"%{q}%"},
        )
        return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/autocomplete/buildings")
def autocomplete_buildings():
    street = request.args.get("street", "").strip()
    q      = request.args.get("q", "").strip()
    if not street:
        return jsonify([])
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT DISTINCT ms_bayit::text FROM "TLV".addresses '
                 "WHERE t_rechov ILIKE :street "
                 "AND (:q = '' OR ms_bayit::text LIKE :q_like) "
                 "ORDER BY ms_bayit LIMIT 20"),
            {"street": street, "q": q, "q_like": f"{q}%"},
        )
        return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/search")
def search():
    street   = request.args.get("street",   "").strip()
    building = request.args.get("building", "").strip()
    if not street or not building:
        return jsonify({"error": "street and building are required"}), 400

    params = {"street": street, "building": building}

    # ── 1. Building geometry + metadata ──────────────────────────────────────
    row = None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    a.t_rechov, a.ms_bayit::text, a.k_rechov,
                    ST_AsGeoJSON(ST_Transform(b.geometry, 4326))  AS geom_json,
                    ST_AsText(a.geometry)                         AS addr_wkt,
                    b.year::int, b.ms_komot::int, b.t_sug_mivne,
                    ST_Y(ST_Transform(a.geometry, 4326))          AS lat,
                    ST_X(ST_Transform(a.geometry, 4326))          AS lon
                FROM "TLV".addresses  a
                JOIN "TLV".buildings  b ON ST_DWithin(a.geometry, b.geometry, 1)
                WHERE a.t_rechov ILIKE :street AND a.ms_bayit::text = :building
                ORDER BY ST_Distance(a.geometry, b.geometry)
                LIMIT 1
            """), params).fetchone()
    except Exception as exc:
        log.warning("Building join failed (%s) — falling back to address point", exc)

    if not row:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    t_rechov, ms_bayit::text, k_rechov,
                    ST_AsGeoJSON(ST_Transform(geometry, 4326)) AS geom_json,
                    ST_AsText(geometry)                        AS addr_wkt,
                    NULL AS year, NULL AS ms_komot, NULL AS t_sug_mivne,
                    ST_Y(ST_Transform(geometry, 4326))         AS lat,
                    ST_X(ST_Transform(geometry, 4326))         AS lon
                FROM "TLV".addresses
                WHERE t_rechov ILIKE :street AND ms_bayit::text = :building
                LIMIT 1
            """), params).fetchone()

    if not row:
        return jsonify({"error": "Address not found"}), 404

    t_rechov, ms_bayit, k_rechov, geom_json, addr_wkt, year, ms_komot, t_sug_mivne, lat, lon = row

    # ── 2. TAMA38 analysis ────────────────────────────────────────────────────
    permits         = []
    nearby_200m     = 0
    nearby_500m     = 0
    has_open_site   = False
    permits_loaded  = True

    try:
        with engine.connect() as conn:
            permit_rows = conn.execute(text("""
                SELECT sw_tama_38, sw_tama_38_chadash, sw_tama_38_tosefet,
                       building_stage, permission_date,
                       open_request, tr_hathalat_bniya, finished
                FROM "TLV".permits
                WHERE ST_DWithin(geometry,
                                 ST_SetSRID(ST_GeomFromText(:wkt), 2039), 5)
            """), {"wkt": addr_wkt}).fetchall()
            permits = [_row_to_dict(r) for r in permit_rows]

            cnt = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE ST_DWithin(
                        geometry, ST_SetSRID(ST_GeomFromText(:wkt), 2039), 200
                    )) AS n200,
                    COUNT(*) FILTER (WHERE ST_DWithin(
                        geometry, ST_SetSRID(ST_GeomFromText(:wkt), 2039), 500
                    )) AS n500
                FROM "TLV".permits
                WHERE (sw_tama_38 = 'כן' OR sw_tama_38_chadash = 'כן'
                       OR sw_tama_38_tosefet = 'כן')
                AND NOT ST_DWithin(geometry,
                                   ST_SetSRID(ST_GeomFromText(:wkt), 2039), 5)
            """), {"wkt": addr_wkt}).fetchone()
            nearby_200m = cnt[0] or 0
            nearby_500m = cnt[1] or 0

    except Exception as exc:
        log.warning("Permits query failed (%s) — table not loaded yet?", exc)
        permits_loaded = False

    try:
        with engine.connect() as conn:
            site = conn.execute(text("""
                SELECT 1 FROM "TLV".building_sites
                WHERE ST_DWithin(geometry,
                                 ST_SetSRID(ST_GeomFromText(:wkt), 2039), 5)
                LIMIT 1
            """), {"wkt": addr_wkt}).fetchone()
            has_open_site = site is not None
    except Exception as exc:
        log.warning("Building sites query failed (%s)", exc)

    tama = compute_tama_score(
        permits=permits,
        year=year,
        ms_komot=ms_komot,
        nearby_200m=nearby_200m,
        has_open_site_case=has_open_site,
    )
    tama["nearby_500m"] = nearby_500m
    if not permits_loaded:
        tama["data_note"] = (
            "Permit data not loaded — run python fetch_tlv_addresses.py "
            "to enable full TAMA38 analysis."
        )

    # ── 3. ML completion probability (only for in-progress permits) ────────────
    ml_proba = None
    has_active_permit = (
        tama.get("status") != "No TAMA38 permit found"
        and not tama.get("is_completed")
    )
    if has_active_permit:
        try:
            tl    = tama.get("timeline", {})
            today = _date.today()

            def _iso_days(a_str, b_str=None):
                if not a_str:
                    return None
                a = _date.fromisoformat(a_str)
                b = _date.fromisoformat(b_str) if b_str else today
                return (b - a).days

            # Most-recent milestone determines recency (staleness signal)
            _last = tl.get("build") or tl.get("permit") or tl.get("form1")

            ml_features = {
                "days_since_form1":         _iso_days(tl.get("form1")),
                "days_form1_to_permit":     _iso_days(tl.get("form1"), tl.get("permit")),
                "days_permit_to_build":     _iso_days(tl.get("permit"), tl.get("build")),
                "days_since_last_milestone": _iso_days(_last),
                "has_permit":               int(bool(tl.get("permit"))),
                "has_construction":         int(bool(tl.get("build"))),
                "building_year":            year,
                "building_floors":          ms_komot,
                "is_track2":                int(any(
                    p.get("sw_tama_38_chadash") == "כן" for p in permits
                )),
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
            }
            ml_proba = predict_completion_proba(ml_features)
        except Exception as exc:
            log.debug("ML prediction skipped: %s", exc)

    archive_url = (
        f"https://handasa.tel-aviv.gov.il/Pages/SearchResultsAnonPageNew.aspx"
        f"?partialAddress={k_rechov}_{ms_bayit}"
        if k_rechov and ms_bayit else None
    )

    return jsonify({
        "street":        t_rechov,
        "building":      ms_bayit,
        "k_rechov":      k_rechov,
        "geometry":      json.loads(geom_json),
        "building_info": {"year": year, "floors": ms_komot, "type": t_sug_mivne},
        "archive_url":   archive_url,
        "tama":          tama,
        "ml_proba":      ml_proba,
    })


@app.route("/api/nearby_permits")
def nearby_permits():
    street   = request.args.get("street",   "").strip()
    building = request.args.get("building", "").strip()
    if not street or not building:
        return jsonify({"type": "FeatureCollection", "features": []})

    try:
        with engine.connect() as conn:
            addr_wkt = conn.execute(text("""
                SELECT ST_AsText(geometry) FROM "TLV".addresses
                WHERE t_rechov ILIKE :street AND ms_bayit::text = :building
                LIMIT 1
            """), {"street": street, "building": building}).scalar()

            if not addr_wkt:
                return jsonify({"type": "FeatureCollection", "features": []})

            rows = conn.execute(text("""
                SELECT
                    ST_AsGeoJSON(ST_Transform(geometry, 4326)),
                    request_stage, building_stage,
                    CASE
                        WHEN sw_tama_38_chadash = 'כן' THEN 'Track 2'
                        WHEN sw_tama_38_tosefet  = 'כן' THEN 'Track 1'
                        ELSE 'TAMA38'
                    END AS track,
                    permission_date, open_request
                FROM "TLV".permits
                WHERE (sw_tama_38 = 'כן' OR sw_tama_38_chadash = 'כן'
                       OR sw_tama_38_tosefet = 'כן')
                AND ST_DWithin(geometry,
                               ST_SetSRID(ST_GeomFromText(:wkt), 2039), 500)
                AND NOT ST_DWithin(geometry,
                                   ST_SetSRID(ST_GeomFromText(:wkt), 2039), 5)
                LIMIT 50
            """), {"wkt": addr_wkt}).fetchall()

    except Exception as exc:
        log.warning("nearby_permits failed: %s", exc)
        return jsonify({"type": "FeatureCollection", "features": []})

    features = [
        {
            "type": "Feature",
            "geometry": json.loads(r[0]),
            "properties": {
                "request_stage":   r[1],
                "building_stage":  r[2],
                "track":           r[3],
                "permission_date": _ts_to_iso(r[4]),
                "open_request":    _ts_to_iso(r[5]),
            },
        }
        for r in rows
    ]
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/neighborhood_stats")
def neighborhood_stats():
    """
    Return per-neighborhood TAMA38 completion statistics as a GeoJSON
    FeatureCollection.  Requires TLV.neighborhoods to be populated via
    fetch_neighborhoods.py.
    """
    try:
        with engine.connect() as conn:
            has_nbhd = conn.execute(text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'TLV' AND table_name = 'neighborhoods'
            """)).fetchone()
            if not has_nbhd:
                return jsonify({"error": "Neighborhood data not loaded"}), 404

            rows = conn.execute(text("""
                SELECT
                    n.shem_shkuna                                AS neighborhood,
                    COUNT(p.ctid)                                AS total,
                    COUNT(p.ctid) FILTER (WHERE
                        p.finished IS NOT NULL
                        OR p.building_stage IN (
                            'קיים אכלוס', 'קיימת לפחות תעודת גמר אחת'
                        )
                    )                                            AS completed,
                    AVG(
                        (p.permission_date - p.open_request) / 86400000.0
                    ) FILTER (
                        WHERE p.permission_date IS NOT NULL
                          AND p.open_request    IS NOT NULL
                    )                                            AS avg_days_to_permit,
                    ST_AsGeoJSON(ST_Transform(n.geometry, 4326)) AS geom_json
                FROM "TLV".neighborhoods n
                JOIN "TLV".permits p
                    ON ST_Within(p.geometry, n.geometry)
                WHERE (
                    p.sw_tama_38         = 'כן'
                 OR p.sw_tama_38_chadash = 'כן'
                 OR p.sw_tama_38_tosefet = 'כן'
                )
                GROUP BY n.shem_shkuna, n.geometry
                HAVING COUNT(p.ctid) > 0
                ORDER BY completed DESC
            """)).fetchall()
    except Exception as exc:
        log.warning("neighborhood_stats failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    features = []
    for row in rows:
        total     = int(row[1]) if row[1] else 0
        completed = int(row[2]) if row[2] else 0
        features.append({
            "type": "Feature",
            "geometry": json.loads(row[4]) if row[4] else None,
            "properties": {
                "neighborhood":       row[0],
                "total":              total,
                "completed":          completed,
                "completion_rate":    round(completed / total, 3) if total else 0,
                "avg_days_to_permit": round(float(row[3]), 0) if row[3] else None,
            },
        })
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/duration_stats")
def duration_stats_endpoint():
    """
    Return TAMA38 duration statistics derived from completed buildings.
    Populated after train_tama_model.py has been run.
    Used by the frontend to show "typical timeline" context.
    """
    stats = _ml_duration_stats()
    if not stats:
        return jsonify({"error": "Model not trained yet — run train_tama_model.py"}), 404
    return jsonify(stats)


@app.route("/api/archive_timeline")
def archive_timeline():
    """
    Scrape the Tel Aviv engineering archive page for a building and return
    milestone dates not available in the GIS layer (e.g. verbal vs signed permit).

    Query params: k_rechov, ms_bayit
    Response: {"timeline": {form1?, permit_verbal?, permit_signed?, build?, form4?}}
    """
    k_rechov = request.args.get("k_rechov", "").strip()
    ms_bayit  = request.args.get("ms_bayit",  "").strip()
    if not k_rechov or not ms_bayit:
        return jsonify({"error": "k_rechov and ms_bayit are required"}), 400

    url = (
        "https://handasa.tel-aviv.gov.il/Pages/SearchResultsAnonPageNew.aspx"
        f"?partialAddress={k_rechov}_{ms_bayit}"
    )
    try:
        proc = _subprocess.run(
            [_sys.executable, _WORKER, url],
            capture_output=True, text=True, timeout=40,
            cwd=_os.path.dirname(__file__),
        )
        timeline = _json.loads(proc.stdout) if proc.stdout.strip() else {}
    except Exception as exc:
        log.warning("Archive scrape subprocess failed: %s", exc)
        timeline = {}
    return jsonify({"timeline": timeline})


# ── Zoning layer helpers ──────────────────────────────────────────────────────

_ZONING_LAYER_URL = (
    "https://gisn.tel-aviv.gov.il/ArcGIS/rest/services/IView2/MapServer/837"
)
_zoning_style_cache: dict | None = None


def _rgba_hex(rgba: list) -> str:
    r, g, b = rgba[0], rgba[1], rgba[2]
    return f"#{r:02x}{g:02x}{b:02x}"


def _parse_sym(sym: dict, label: str = "") -> dict:
    fill  = sym.get("color", [0, 0, 0, 0])
    out   = (sym.get("outline") or {})
    ocol  = out.get("color", [0, 0, 0, 0])
    return {
        "fill":        _rgba_hex(fill),
        "fillOpacity": round(fill[3] / 255, 2) if len(fill) > 3 else 1.0,
        "stroke":      _rgba_hex(ocol) if len(ocol) > 3 and ocol[3] > 0 else "#555",
        "weight":      out.get("width", 0.4),
        "pattern":     sym.get("style", "esriSFSSolid"),
        "label":       label,
    }


@app.route("/api/zoning/style")
def zoning_style():
    """Return a compact value→style map built from the layer's drawingInfo."""
    global _zoning_style_cache
    if _zoning_style_cache is not None:
        return jsonify(_zoning_style_cache)

    try:
        resp = _requests.get(_ZONING_LAYER_URL, params={"f": "json"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Failed to fetch zoning layer info: %s", exc)
        return jsonify({"error": str(exc)}), 502

    renderer = data.get("drawingInfo", {}).get("renderer", {})
    styles: dict = {}

    default_sym = renderer.get("defaultSymbol")
    if default_sym:
        styles["__default__"] = _parse_sym(default_sym, renderer.get("defaultLabel", "אחר"))

    for info in renderer.get("uniqueValueInfos", []):
        styles[str(info["value"])] = _parse_sym(info["symbol"], info.get("label", ""))

    _zoning_style_cache = {"styles": styles}
    return jsonify(_zoning_style_cache)


@app.route("/api/zoning/identify")
def zoning_identify():
    """Return the zone at a lat/lng click point via ArcGIS identify."""
    lat  = request.args.get("lat", type=float)
    lng  = request.args.get("lng", type=float)
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng required"}), 400

    # Build a tiny bbox around the click point
    d = 0.0005
    params = {
        "geometry":     f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "layers":       "all:837",
        "sr":           "4326",
        "mapExtent":    f"{lng-d},{lat-d},{lng+d},{lat+d}",
        "imageDisplay": "1,1,96",
        "tolerance":    "3",
        "returnGeometry": "false",
        "f":            "json",
    }
    try:
        resp = _requests.get(
            "https://gisn.tel-aviv.gov.il/ArcGIS/rest/services/IView2/MapServer/identify",
            params=params, timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            attrs = results[0].get("attributes", {})
            return jsonify({
                "label":       attrs.get("t_yeud") or attrs.get("t_yeud_karka") or "—",
                "value":       str(attrs.get("k_yeud_karka", "")),
                "layerName":   results[0].get("layerName", ""),
            })
        return jsonify({"label": None})
    except Exception as exc:
        log.warning("Zoning identify failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--host", default="127.0.0.1")
    _p.add_argument("--port", type=int, default=5000)
    _opts = _p.parse_args()

    # Pre-compile Playwright's .py files so the Werkzeug watchdog doesn't
    # detect file changes and restart mid-request when the scraper first runs.
    import compileall, site as _site
    for _d in _site.getsitepackages():
        compileall.compile_dir(_d, quiet=2, force=False)

    # use_reloader=False prevents a second watchdog restart loop
    app.run(debug=True, host=_opts.host, port=_opts.port, use_reloader=False)
