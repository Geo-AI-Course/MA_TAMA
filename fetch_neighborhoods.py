"""
Downloads Tel Aviv neighborhood (שכונות) boundaries from the city's ArcGIS REST API
and loads them into PostGIS as TLV.neighborhoods.

Tries the two most likely MapServer layer IDs.  Safe to re-run (truncates and
reloads on each run).

Usage:
    python fetch_neighborhoods.py
"""
import json
import logging
import sys

import requests
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

POSTGIS = {
    "host":     "localhost",
    "port":     5432,
    "database": "MA_TAMA",
    "user":     "postgres",
    "password": "mypassword",
}

# Tel Aviv ArcGIS MapServer candidates for the neighborhoods layer.
_CANDIDATES = [
    "https://gisn.tel-aviv.gov.il/ArcGIS/rest/services/IView2/MapServer/511",
    "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM_FW/ILayer/MapServer/70",
    "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM_FW/ILayer/MapServer/338",
    "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM_FW/ILayer/MapServer/71",
]

_DDL = """
CREATE TABLE IF NOT EXISTS "TLV".neighborhoods (
    id          serial PRIMARY KEY,
    shem_shkuna text,
    ms_shkuna   integer,
    geometry    geometry(MultiPolygon, 2039)
);
CREATE INDEX IF NOT EXISTS neighborhoods_geom_idx
    ON "TLV".neighborhoods USING GIST (geometry);
"""

engine = create_engine(
    f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
    f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
)


def _fetch_geojson(base_url: str) -> dict | None:
    """Query an ArcGIS MapServer layer for all features as GeoJSON (EPSG:2039)."""
    params = {
        "where":      "1=1",
        "outFields":  "*",
        "outSR":      "2039",
        "f":          "geojson",
        "returnGeometry": "true",
    }
    try:
        r = requests.get(f"{base_url}/query", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "features" in data and data["features"]:
            log.info("Fetched %d features from %s", len(data["features"]), base_url)
            return data
    except Exception as exc:
        log.warning("Candidate %s failed: %s", base_url, exc)
    return None


def _load_into_db(geojson: dict):
    """Truncate and reload TLV.neighborhoods from a GeoJSON FeatureCollection."""
    features = geojson.get("features", [])
    with engine.connect() as conn:
        conn.execute(text(_DDL))
        conn.execute(text('TRUNCATE "TLV".neighborhoods RESTART IDENTITY'))
        for feat in features:
            props = feat.get("properties") or {}
            geom  = feat.get("geometry")
            if not geom:
                continue
            # Possible field names for the Hebrew neighborhood name
            name = (
                props.get("shem_shchuna")
                or props.get("SHEM_SHCHUNA")
                or props.get("SHEM_SHKUNA")
                or props.get("shem_shkuna")
                or props.get("SHEM")
                or props.get("NAME")
                or props.get("name")
                or ""
            )
            ms_sh = (
                props.get("ms_shchuna")
                or props.get("MS_SHCHUNA")
                or props.get("MS_SHKUNA")
                or props.get("ms_shkuna")
                or props.get("OBJECTID")
            )
            conn.execute(text("""
                INSERT INTO "TLV".neighborhoods (shem_shkuna, ms_shkuna, geometry)
                VALUES (
                    :name,
                    :ms,
                    ST_SetSRID(
                        ST_Multi(ST_GeomFromGeoJSON(:geom)),
                        2039
                    )
                )
            """), {
                "name": str(name),
                "ms":   int(ms_sh) if ms_sh is not None else None,
                "geom": json.dumps(geom),
            })
        conn.commit()
    log.info("Loaded %d neighborhoods into TLV.neighborhoods", len(features))


def main():
    geojson = None
    for url in _CANDIDATES:
        log.info("Trying %s ...", url)
        geojson = _fetch_geojson(url)
        if geojson:
            break

    if not geojson:
        log.error(
            "Could not fetch neighborhood boundaries from any candidate URL.\n"
            "The training script will fall back to lat/lon coordinates as spatial features.\n"
            "You can add more candidate URLs to _CANDIDATES in fetch_neighborhoods.py."
        )
        sys.exit(1)

    _load_into_db(geojson)
    log.info("Done — run train_tama_model.py to include neighborhood features.")


if __name__ == "__main__":
    main()
