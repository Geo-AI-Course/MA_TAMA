"""
Fetch TLV addresses layer (MapServer/527) from Tel Aviv ArcGIS REST
and load into a PostGIS database, replacing the table each run.
"""
import logging
import requests
import geopandas as gpd
from sqlalchemy import create_engine, text

# --- Configuration ---
POSTGIS = {
    "host": "localhost",
    "port": 5432,
    "database": "MA_TAMA",
    "user": "postgres",
    "password": "mypassword",
    "schema": "TLV",
}
LAYER_URL = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/IView2/MapServer/527"
TABLE_NAME = "addresses"
BATCH_SIZE = 2000
TARGET_SRID = 2039  # Israel 1993 / Israeli TM Grid (native CRS of this layer)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fetch_tlv_addresses.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def fetch_all_features() -> gpd.GeoDataFrame:
    query_url = f"{LAYER_URL}/query"
    all_features = []
    offset = 0

    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": TARGET_SRID,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": BATCH_SIZE,
        }
        resp = requests.get(query_url, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json().get("features", [])
        if not batch:
            break
        all_features.extend(batch)
        log.info("Fetched %d features so far (offset %d)", len(all_features), offset)
        if len(batch) < BATCH_SIZE:
            break
        offset += BATCH_SIZE

    if not all_features:
        raise RuntimeError("No features returned from the REST service.")

    return gpd.GeoDataFrame.from_features(all_features, crs=f"EPSG:{TARGET_SRID}")


def save_to_postgis(gdf: gpd.GeoDataFrame) -> None:
    schema = POSTGIS["schema"]
    engine = create_engine(
        f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
        f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
    )

    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    gdf.to_postgis(TABLE_NAME, engine, schema=schema, if_exists="replace", index=False)

    geom_col = gdf.geometry.name
    with engine.begin() as conn:
        # Spatial index (GIST) for geometry queries
        conn.execute(text(
            f'CREATE INDEX {TABLE_NAME}_geom_idx '
            f'ON "{schema}"."{TABLE_NAME}" USING GIST ("{geom_col}")'
        ))
        # B-tree index on the primary address identifier
        conn.execute(text(
            f'CREATE INDEX {TABLE_NAME}_id_ktovet_idx '
            f'ON "{schema}"."{TABLE_NAME}" (id_ktovet)'
        ))
        # B-tree index on street code for common filter/join queries
        conn.execute(text(
            f'CREATE INDEX {TABLE_NAME}_k_rechov_idx '
            f'ON "{schema}"."{TABLE_NAME}" (k_rechov)'
        ))

    log.info(
        "Saved %d records to %s.%s with spatial and attribute indexes.",
        len(gdf), schema, TABLE_NAME,
    )


def main():
    log.info("=== Starting TLV addresses fetch ===")
    gdf = fetch_all_features()
    log.info("Total features fetched: %d", len(gdf))
    save_to_postgis(gdf)
    log.info("=== Finished ===")


if __name__ == "__main__":
    main()
