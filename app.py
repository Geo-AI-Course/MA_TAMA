"""
Ma TAMA – Flask backend
Serves the UI and three API endpoints that query the TLV.addresses PostGIS table.
"""
import json
import logging

from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, text

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
            text(
                'SELECT DISTINCT t_rechov FROM "TLV".addresses '
                "WHERE t_rechov ILIKE :q ORDER BY t_rechov LIMIT 10"
            ),
            {"q": f"%{q}%"},
        )
        return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/autocomplete/buildings")
def autocomplete_buildings():
    street = request.args.get("street", "").strip()
    q = request.args.get("q", "").strip()
    if not street:
        return jsonify([])
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                'SELECT DISTINCT ms_bayit::text FROM "TLV".addresses '
                "WHERE t_rechov ILIKE :street "
                "AND (:q = '' OR ms_bayit::text LIKE :q_like) "
                "ORDER BY ms_bayit LIMIT 20"
            ),
            {"street": street, "q": q, "q_like": f"{q}%"},
        )
        return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/search")
def search():
    street = request.args.get("street", "").strip()
    building = request.args.get("building", "").strip()
    if not street or not building:
        return jsonify({"error": "street and building are required"}), 400

    params = {"street": street, "building": building}

    # Primary: building polygon that contains (or is within 1 m of) the address point
    row = None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.t_rechov, a.ms_bayit::text, "
                    "ST_AsGeoJSON(ST_Transform(b.geometry, 4326)) "
                    'FROM "TLV".addresses a '
                    'JOIN "TLV".buildings b ON ST_DWithin(a.geometry, b.geometry, 1) '
                    "WHERE a.t_rechov ILIKE :street AND a.ms_bayit::text = :building "
                    "ORDER BY ST_Distance(a.geometry, b.geometry) "
                    "LIMIT 1"
                ),
                params,
            ).fetchone()
    except Exception as exc:
        log.warning("Building polygon lookup failed (%s) — falling back to address point", exc)

    # Fallback: address point geometry
    if not row:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT t_rechov, ms_bayit::text, "
                    "ST_AsGeoJSON(ST_Transform(geometry, 4326)) "
                    'FROM "TLV".addresses '
                    "WHERE t_rechov ILIKE :street AND ms_bayit::text = :building "
                    "LIMIT 1"
                ),
                params,
            ).fetchone()

    if not row:
        return jsonify({"error": "Address not found"}), 404

    return jsonify(
        {"street": row[0], "building": row[1], "geometry": json.loads(row[2])}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
