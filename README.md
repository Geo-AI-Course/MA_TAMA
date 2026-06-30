# MA TAMA

A geodata-driven web application that estimates the completion probability of TAMA38 for any address in Tel Aviv — and tracks the full permit lifecycle for buildings that have already gone through the process.

## Problem

TAMA38 (National Building Plan 38) grants significant renovation rights and value uplift to eligible buildings, yet most renters and buyers have no practical way to assess a property's likelihood of completing such a plan before signing a lease or purchase.  This information asymmetry puts tenants at a disadvantage — they may unknowingly commit to a home that will face years of construction, or miss out on one that stands to significantly appreciate.

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
| Neighborhood boundaries | Tel Aviv ArcGIS MapServer/511 |

## Features

### ML Completion Forecast
For buildings with an active TAMA38 permit, a trained gradient-boosting classifier estimates the probability that the project will reach Form 4 (occupancy certificate) within the expected timeframe.  The forecast is shown as a colour-coded progress bar:

- **Green** (≥ 65 %) — on track
- **Amber** (40–65 %) — some delay risk
- **Red** (< 40 %) — significant overrun risk

Features used: time elapsed since Form 1, permit speed, construction start, building age and floors, track type (1 / 2), lat/lon, and neighbourhood-level completion rates derived from completed buildings.

### TAMA38 Likelihood Dashboard
For buildings without an existing TAMA38 permit, the app scores the building across several signals:

- **Building age** — older buildings score higher
- **Floor count** — fewer floors = higher renovation potential
- **Existing TAMA38 permits nearby** — social proof within 200 m and 500 m
- **Open construction site** — active site adjacent to the building
- **Overall outlook** — composite score ring with colour-coded verdict

### 5-Step Permit Timeline
For buildings with an existing or completed TAMA38, the app shows a historical timeline with five milestones:

1. **טופס 1** — Initial permit application (רישוי)
2. **היתר מילולי חתום** — Signed verbal approval
3. **היתר — תכנית חתומה** — Signed building plan (full permit)
4. **תחילת בנייה** — Construction start
5. **טופס 4** — Occupancy certificate (אכלוס)

Steps 1, 4, and 5 are populated from the GIS permit layer.  Steps 2 and 3 are scraped live from the Tel Aviv Engineering Archive.

### Completed TAMA38 Mode
When a building has already completed TAMA38 (received a טופס 4 or completion certificate), the likelihood dashboard is hidden and replaced with a "TAMA38 History" view showing the full permit timeline with all five milestones.

### Two-Phase Loading
The UI renders GIS-derived data immediately, then enriches the timeline with archive data asynchronously (~10–15 s).  A skeleton shimmer animation indicates which steps are still loading.

---

## How to Run

### One-command setup (recommended)

```bash
python setup.py
```

This single script handles everything in order:

1. Checks Python >= 3.10
2. Installs all pip dependencies
3. Installs Playwright / Chromium
4. Connects to PostgreSQL and creates the `MA_TAMA` database if absent
5. Enables PostGIS, creates the `TLV` schema
6. Fetches GIS layers from Tel Aviv ArcGIS (re-fetches if older than 7 days)
7. Downloads Tel Aviv neighborhood boundaries
8. Runs the Engineering Archive bulk scrape (4–6 hours; safe to interrupt and resume)
9. Trains the ML model
10. Launches the Flask web app

Useful flags:

| Flag | Effect |
|------|--------|
| `--skip-scrape` | Skip archive scrape (use partial data) |
| `--skip-train` | Skip model training |
| `--skip-app` | Setup only — do not launch Flask |
| `--force-refresh` | Re-fetch GIS data even if fresh |
| `--refresh-days N` | GIS freshness threshold in days (default: 7) |
| `--host 0.0.0.0` | Bind on all interfaces |
| `--port 5001` | Custom port |

### Manual setup (advanced)

```bash
# 1. Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# 2. Fetch GIS data
python fetch_tlv_addresses.py

# 3. Fetch neighbourhood boundaries
python fetch_neighborhoods.py

# 4. Run the archive bulk scrape (takes several hours)
python fetch_archive_bulk.py --delay 0.5

# 5. Train the ML model
python train_tama_model.py

# 6. Start the app
python app.py
```

### Database credentials

Defaults: `localhost:5432 / MA_TAMA / postgres / mypassword`.
Override with environment variables before running any script:

```bash
export DB_HOST=myserver DB_PORT=5432 DB_NAME=MA_TAMA DB_USER=postgres DB_PASSWORD=secret
```

---

## Project Structure

```
MA_TAMA/
├── setup.py                # One-shot setup & launcher (start here)
├── app.py                  # Flask backend + REST API
├── tama_score.py           # TAMA38 heuristic scoring engine
├── ml_score.py             # ML completion forecast (loads tama_model.pkl)
├── train_tama_model.py     # Train HistGradientBoosting classifier
├── scrape_archive.py       # Playwright scraper — Tel Aviv Engineering Archive
├── _scrape_worker.py       # Subprocess entry point for scraper (Playwright isolation)
├── fetch_tlv_addresses.py  # ArcGIS → PostGIS ingestion (4 layers)
├── fetch_neighborhoods.py  # Neighbourhood boundaries → TLV.neighborhoods
├── fetch_archive_bulk.py   # Bulk scrape of archive_timelines table
├── check_progress.py       # Show archive scrape progress + ETA
├── check_db.py             # Row-count summary for all tables
├── setup_schedule.ps1      # Windows Task Scheduler setup (weekly refresh)
├── requirements.txt
├── tama_model.pkl          # Trained model bundle (generated by train_tama_model.py)
├── tama_model_meta.json    # Human-readable training metadata
└── templates/
    └── index.html          # Leaflet SPA — map, search, dashboard, timeline, ML bar
```

### Database tables

| Table | Description |
|-------|-------------|
| `TLV.addresses` | 52 k address points (ArcGIS layer 527) |
| `TLV.buildings` | 46 k building polygons (ArcGIS layer 513) |
| `TLV.building_sites` | ~1.6 k active construction sites (ArcGIS layer 499) |
| `TLV.permits` | ~10.5 k TAMA38 permit polygons (ArcGIS layer 772) |
| `TLV.neighborhoods` | 71 Tel Aviv neighbourhood polygons (ArcGIS layer 511) |
| `TLV.archive_timelines` | Per-building milestone dates scraped from Engineering Archive |
| `TLV.meta` | Internal key-value store for setup script freshness timestamps |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the UI |
| `GET` | `/api/autocomplete/streets?q=` | Street name suggestions |
| `GET` | `/api/autocomplete/buildings?street=&q=` | Building number suggestions |
| `GET` | `/api/search?street=&building=` | Building geometry + TAMA38 analysis + ML forecast |
| `GET` | `/api/nearby_permits?street=&building=` | Nearby TAMA38 permit polygons (GeoJSON) |
| `GET` | `/api/archive_timeline?k_rechov=&ms_bayit=` | Permit milestones from Engineering Archive |
| `GET` | `/api/neighborhood_stats` | GeoJSON FeatureCollection with per-neighbourhood completion rates |
| `GET` | `/api/duration_stats` | Reference duration statistics used by the ML model |

---

## ML Pipeline

The completion-probability model is a `HistGradientBoostingClassifier` (scikit-learn) that natively handles missing values.

**Training flow:**
1. Join permits + buildings + addresses + `archive_timelines` + neighborhoods
2. Compute raw durations between milestones (Form 1 → Permit, Permit → Build, Permit → Form 4)
3. Derive three time-ratio features normalised against completed-building percentiles:
   - `progress_pct` — days since Form 1 / median(Form 1 → Form 4)
   - `permit_speed_ratio` — Form 1 → Permit duration / median
   - `days_past_p75_total` — days overrun past the 75th-percentile total timeline
4. Label buildings as *completed* (have Form 4) or *stalled* (elapsed > p90 × 1.3)
5. Cross-validate (skipped if minority class < 10 samples; retrain after bulk scrape)
6. Fit on full dataset and save `tama_model.pkl`

**Current training data** (grows as archive scrape progresses):
- ~195 completed buildings  
- ~5 stalled buildings  
- Duration statistics: median Form 1 → Form 4 = 5.9 years; p90 = 7.4 years

The model improves as `fetch_archive_bulk.py` scrapes more buildings (~2,900 total).

---

## Architecture Notes

**Playwright isolation** — The Tel Aviv Engineering Archive is an Angular/SharePoint SPA that requires a headless browser.  Playwright is invoked in a subprocess (`_scrape_worker.py`) rather than directly inside Flask, preventing Chromium from conflicting with Flask's Werkzeug debug reloader.  Flask also runs with `use_reloader=False` and pre-compiles Playwright's `.pyc` files at startup to avoid watchdog false-triggers.

**Two-phase data loading** — `/api/search` returns immediately with GIS permit data.  The frontend then fires a separate `/api/archive_timeline` request and merges the result into the timeline when it resolves.

**Setup freshness tracking** — `setup.py` records `gis_last_fetched` in `TLV.meta` after each successful GIS download and skips the fetch on subsequent runs if the data is still within the configured TTL.

---

## Roadmap

- [x] Working web map with address search and building geometry
- [x] TAMA38 scoring engine (age, floors, nearby permits, active sites)
- [x] Permit lifecycle timeline (5 milestones)
- [x] Live scraping of Tel Aviv Engineering Archive for verbal/signed permit dates
- [x] Completed-TAMA38 mode — history view replaces likelihood dashboard
- [x] ML completion forecast with time-ratio features and neighbourhood context
- [x] Neighbourhood boundaries (71 Tel Aviv neighbourhoods)
- [x] One-script setup (`setup.py`) — installs, fetches, trains, launches
- [ ] Neighbourhood-level heatmap view
- [ ] Comparison tool for multiple addresses
- [ ] Deploy to a cloud PostgreSQL service
