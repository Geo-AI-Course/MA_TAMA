"""
Fetch TLV geodata layers from the Tel Aviv ArcGIS REST service
and load them into a PostGIS database, replacing each table every run.

Layers:
  addresses  – MapServer/527  (address points, EPSG:2039)
  buildings  – MapServer/513  (building footprint polygons, EPSG:2039)
"""
import logging
import requests
import geopandas as gpd
from sqlalchemy import create_engine, text

# --- Shared configuration ---
POSTGIS = {
    "host": "localhost",
    "port": 5432,
    "database": "MA_TAMA",
    "user": "postgres",
    "password": "mypassword",
    "schema": "TLV",
}
BATCH_SIZE = 2000
TARGET_SRID = 2039  # Israel 1993 / Israeli TM Grid (native CRS of both layers)

BASE_URL = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/IView2/MapServer"

# --- Layer registry ---
# Each entry: url, table name in PostGIS, extra B-tree indexes beyond the GIST
LAYERS = [
    {
        "url": f"{BASE_URL}/527",
        "table": "addresses",
        "indexes": ["id_ktovet", "k_rechov"],
    },
    {
        "url": f"{BASE_URL}/513",
        "table": "buildings",
        "indexes": ["id_binyan", "ms_komot", "year"],
    },
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fetch_tlv_addresses.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def fetch_all_features(layer_url: str) -> gpd.GeoDataFrame:
    query_url = f"{layer_url}/query"
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
        log.info("  %s: %d features so far (offset %d)", layer_url, len(all_features), offset)
        if len(batch) < BATCH_SIZE:
            break
        offset += BATCH_SIZE

    if not all_features:
        raise RuntimeError(f"No features returned from {layer_url}")

    return gpd.GeoDataFrame.from_features(all_features, crs=f"EPSG:{TARGET_SRID}")


def save_to_postgis(gdf: gpd.GeoDataFrame, table_name: str, attribute_indexes: list[str]) -> None:
    schema = POSTGIS["schema"]
    engine = create_engine(
        f"postgresql+psycopg2://{POSTGIS['user']}:{POSTGIS['password']}"
        f"@{POSTGIS['host']}:{POSTGIS['port']}/{POSTGIS['database']}"
    )

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    gdf.to_postgis(table_name, engine, schema=schema, if_exists="replace", index=False)

    geom_col = gdf.geometry.name
    with engine.begin() as conn:
        conn.execute(text(
            f'CREATE INDEX {table_name}_geom_idx '
            f'ON "{schema}"."{table_name}" USING GIST ("{geom_col}")'
        ))
        for col in attribute_indexes:
            conn.execute(text(
                f'CREATE INDEX {table_name}_{col}_idx '
                f'ON "{schema}"."{table_name}" ({col})'
            ))

    log.info(
        "  Saved %d records to %s.%s with GIST + %d attribute indexes.",
        len(gdf), schema, table_name, len(attribute_indexes),
    )


def main():
    for layer in LAYERS:
        log.info("=== Fetching layer: %s → %s ===", layer["url"], layer["table"])
        gdf = fetch_all_features(layer["url"])
        log.info("  Total features: %d", len(gdf))
        save_to_postgis(gdf, layer["table"], layer["indexes"])

    log.info("=== All layers finished ===")


if __name__ == "__main__":
    main()
