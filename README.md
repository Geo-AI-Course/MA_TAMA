# MA TAMA

A geodata-driven web application that estimates the probability of TAMA38 for any address in Tel Aviv — and tracks the full permit lifecycle for buildings that have already gone through the process.

## Problem

TAMA38 (National Building Plan 38) grants significant renovation rights and value uplift to eligible buildings, yet most renters and buyers have no practical way to assess a property's likelihood of undergoing such a plan before signing a lease or purchase. This information asymmetry puts tenants at a disadvantage — they may unknowingly commit to a home that will face years of construction, or miss out on one that stands to significantly appreciate.

## Target Users

- Renters evaluating apartments in Tel Aviv before signing a lease
- Buyers looking to factor TAMA38 potential into property valuation
- Real estate professionals seeking data-driven insights on building eligibility

## Data Sources

| Layer | Source |
|-------|--------|
| Address + building geometry | Tel Aviv ArcGIS MapServer (EPSG:2039 → WGS84) |
| Active TAMA38 permits | Tel Aviv ArcGIS MapServer/772 — ingested into PostGIS |
| Permit timeline milestones | Tel Aviv Engineering Archive (scraped via Playwright) |

## Features

### TAMA38 Likelihood Dashboard
For buildings without an existing TAMA38 permit, the app scores the building across several signals:

- **Building age** — older buildings score higher
- **Floor count** — fewer floors = higher renovation potential
- **Existing TAMA38 permits nearby** — social proof within 200 m and 500 m
- **Open construction site** — active site adjacent to the building
- **Overall outlook** — composite score ring with color-coded verdict

### 5-Step Permit Timeline
For buildings with an existing or completed TAMA38, the app shows a historical timeline with five milestones:

1. **טופס 1** — Initial permit application (רישוי)
2. **היתר מילולי חתום** — Signed verbal approval
3. **היתר — תכנית חתומה** — Signed building plan (full permit)
4. **תחילת בנייה** — Construction start
5. **טופס 4** — Occupancy certificate (אכלוס)

Steps 1, 4, and 5 are populated from the GIS permit layer. Steps 2 and 3 are scraped live from the Tel Aviv Engineering Archive.

### Completed TAMA38 Mode
When a building has already completed TAMA38 (received a טופס 4 or completion certificate), the likelihood dashboard is hidden and replaced with a "TAMA38 History" view showing the full permit timeline with all five milestones.

### Two-Phase Loading
The UI renders GIS-derived data immediately, then enriches the timeline with archive data asynchronously (~10–15 s). A skeleton shimmer animation indicates which steps are still loading.

## How to Run

### 1. Prerequisites

- Python >= 3.10
- PostgreSQL with PostGIS extension
- Chromium (for Playwright archive scraping)

### 2. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Populate the database

Run the data-fetch script once (or let the scheduled task handle it weekly):

```bash
python fetch_tlv_addresses.py
```

This creates the `TLV.addresses`, `TLV.buildings`, `TLV.permits`, and `TLV.building_sites` tables in the `MA_TAMA` PostgreSQL database.

### 4. Start the web app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## Project Structure

```
MA_TAMA/
├── app.py                  # Flask backend + REST API
├── tama_score.py           # TAMA38 scoring engine
├── scrape_archive.py       # Playwright scraper for Tel Aviv Engineering Archive
├── _scrape_worker.py       # Subprocess entry point for scraper (Playwright isolation)
├── fetch_tlv_addresses.py  # ArcGIS → PostGIS ingestion script
├── setup_schedule.ps1      # Windows Task Scheduler setup (weekly refresh)
├── requirements.txt
└── templates/
    └── index.html          # Leaflet SPA — map, search, dashboard, timeline
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the UI |
| `GET` | `/api/autocomplete/streets?q=` | Street name suggestions |
| `GET` | `/api/autocomplete/buildings?street=&q=` | Building number suggestions |
| `GET` | `/api/search?street=&building=` | Building geometry + TAMA38 analysis |
| `GET` | `/api/nearby_permits?street=&building=` | Nearby TAMA38 permit polygons (GeoJSON) |
| `GET` | `/api/archive_timeline?k_rechov=&ms_bayit=` | Permit milestones from Engineering Archive |

## Configuration

Database credentials and target schema are configured at the top of `app.py` and `fetch_tlv_addresses.py`:

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

## Architecture Notes

**Playwright isolation** — The Tel Aviv Engineering Archive is an Angular/SharePoint SPA that requires a headless browser. Playwright is invoked in a subprocess (`_scrape_worker.py`) rather than directly inside Flask, preventing Chromium from conflicting with Flask's Werkzeug debug reloader. Flask also runs with `use_reloader=False` and pre-compiles Playwright's `.pyc` files at startup to avoid watchdog false-triggers.

**Two-phase data loading** — `/api/search` returns immediately with GIS permit data. The frontend then fires a separate `/api/archive_timeline` request and merges the result into the timeline when it resolves.

## Roadmap

- [x] Working web map with address search and building geometry
- [x] TAMA38 scoring engine (age, floors, nearby permits, active sites)
- [x] Permit lifecycle timeline (5 milestones)
- [x] Live scraping of Tel Aviv Engineering Archive for verbal/signed permit dates
- [x] Completed-TAMA38 mode — history view replaces likelihood dashboard
- [ ] Deploy using a PostGIS cloud service
- [ ] Neighborhood-level heatmap view
- [ ] Comparison tool for multiple addresses
- [ ] Additional geodata layers (zoning, proximity to landmarks)
