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

from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, text

from tama_score import compute_tama_score

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
                    b.year, b.ms_komot, b.t_sug_mivne
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
                    NULL AS year, NULL AS ms_komot, NULL AS t_sug_mivne
                FROM "TLV".addresses
                WHERE t_rechov ILIKE :street AND ms_bayit::text = :building
                LIMIT 1
            """), params).fetchone()

    if not row:
        return jsonify({"error": "Address not found"}), 404

    t_rechov, ms_bayit, k_rechov, geom_json, addr_wkt, year, ms_komot, t_sug_mivne = row

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
                       request_stage, building_stage, permission_date,
                       open_request, tr_hathalat_bniya, expiry_date,
                       progress, yechidot_diyur, finished
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
                WHERE (
                    (sw_tama_38      IS NOT NULL AND sw_tama_38      != '') OR
                    (sw_tama_38_chadash IS NOT NULL AND sw_tama_38_chadash != '') OR
                    (sw_tama_38_tosefet IS NOT NULL AND sw_tama_38_tosefet != '')
                )
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

    archive_url = (
        f"https://handasa.tel-aviv.gov.il/Pages/SearchResultsAnonPageNew.aspx"
        f"?partialAddress={k_rechov}_{ms_bayit}"
        if k_rechov and ms_bayit else None
    )

    return jsonify({
        "street":        t_rechov,
        "building":      ms_bayit,
        "geometry":      json.loads(geom_json),
        "building_info": {"year": year, "floors": ms_komot, "type": t_sug_mivne},
        "archive_url":   archive_url,
        "tama":          tama,
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
                        WHEN sw_tama_38_chadash IS NOT NULL
                             AND sw_tama_38_chadash != '' THEN 'Track 2'
                        WHEN sw_tama_38_tosefet  IS NOT NULL
                             AND sw_tama_38_tosefet  != '' THEN 'Track 1'
                        ELSE 'TAMA38'
                    END AS track,
                    permission_date, open_request
                FROM "TLV".permits
                WHERE (
                    (sw_tama_38      IS NOT NULL AND sw_tama_38      != '') OR
                    (sw_tama_38_chadash IS NOT NULL AND sw_tama_38_chadash != '') OR
                    (sw_tama_38_tosefet IS NOT NULL AND sw_tama_38_tosefet != '')
                )
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
                "permission_date": r[4].isoformat() if r[4] else None,
                "open_request":    r[5].isoformat() if r[5] else None,
            },
        }
        for r in rows
    ]
    return jsonify({"type": "FeatureCollection", "features": features})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
