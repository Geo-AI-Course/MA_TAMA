# MA TAMA

A geodata-driven web application that estimates the probability of TAMA38 for any address in Tel Aviv, helping renters and buyers make informed decisions about their next home.

## Problem

TAMA38 (National Building Plan 38) grants significant renovation rights and value uplift to eligible buildings, yet most renters and buyers have no practical way to assess a property's likelihood of undergoing such a plan before signing a lease or purchase. This information asymmetry puts tenants at a disadvantage — they may unknowingly commit to a home that will face years of construction, or miss out on one that stands to significantly appreciate.

## Target Users

- Renters evaluating apartments in Tel Aviv before signing a lease
- Buyers looking to factor TAMA38 potential into property valuation
- Real estate professionals seeking data-driven insights on building eligibility

## Data Source

Building and permit data is fetched from the **Tel Aviv Municipal Engineering Archive** via its public ArcGIS REST API. The addresses layer (MapServer/527) is ingested into a local PostGIS database, providing street names, building numbers, and building geometries in EPSG:2039 (Israeli TM Grid).

## How to Run

### 1. Prerequisites

- Python >= 3.10
- PostgreSQL with PostGIS extension

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Populate the database

Run the data-fetch script once (or let the scheduled task handle it weekly):

```bash
python fetch_tlv_addresses.py
```

This creates the `TLV.addresses` table in the `MA_TAMA` PostgreSQL database.

### 4. Start the web app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## Web Interface

The app is a Leaflet-based map with an OSM background, served by a lightweight Flask backend.

**Features:**
- **Address search** — type a street name and building number to locate any address in Tel Aviv
- **Autocomplete** — both fields query the PostGIS database live for instant suggestions
- **Map zoom** — on match, the map flies to the building and highlights it with a bold blue outline
- Geometries are stored in EPSG:2039 and transformed to WGS84 server-side before rendering

## Project Structure

```
MA_TAMA/
├── app.py                  # Flask backend + REST API
├── fetch_tlv_addresses.py  # ArcGIS → PostGIS ingestion script
├── setup_schedule.ps1      # Windows Task Scheduler setup (weekly refresh)
├── requirements.txt
└── templates/
    └── index.html          # Leaflet UI
```

## Configuration

Database credentials and target schema are configured at the top of both `app.py` and `fetch_tlv_addresses.py`:

```python
POSTGIS = {
    "host":     "localhost",
    "port":     5432,
    "database": "MA_TAMA",
    "user":     "postgres",
    "password": "mypassword",
    "schema":   "TLV",
}
```

## Roadmap

- [x] Construct a working web map interface with basic user interactions
- [ ] Create an analysis system based on fetching data from TLV engineering archive
- [ ] Build a dashboard representing the analysis to the user
- [ ] Deploy using a PostGIS cloud service
- [ ] Additional geodata layers as contextual overlays (zoning, proximity to landmarks, demographics)
- [ ] Neighborhood-level heatmap view
- [ ] Comparison tool for multiple addresses
