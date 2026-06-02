# MA_TAMA

A geodata-driven web application that estimates the probability of TAMA38 for any address in Tel Aviv, helping renters and buyers make informed decisions about their next home.

## Problem

TAMA38 (National Building Plan 38) grants significant renovation rights and value uplift to eligible buildings, yet most renters and buyers have no practical way to assess a property's likelihood of undergoing such a plan before signing a lease or purchase. This information asymmetry puts tenants at a disadvantage — they may unknowingly commit to a home that will face years of construction, or miss out on one that stands to significantly appreciate.

## Target Users

- Renters evaluating apartments in Tel Aviv before signing a lease
- Buyers looking to factor TAMA38 potential into property valuation
- Real estate professionals seeking data-driven insights on building eligibility

## Data Source

Building and permit data is fetched from the **Tel Aviv Municipal Engineering Archive**, which contains historical construction records, building permits, and structural metadata. This public data is analyzed to derive features relevant to TAMA38 eligibility (e.g., construction year, number of floors, building footprint, existing permit history).

## How to Run

```
pip install -r requirements.txt
python main.py
```

## Requirements

- Python >= 3.10
- PostgreSQL + PostGIS

## Structure

- `main.py`
- `fetch_tlv_addresses.py`
- `data/`
- `output/`

## Roadmap

- [ ] Construct a working web map interface with basic user interactions
- [ ] Create an analysis system based on fetching data from TLV engineering archive
- [ ] Build a dashboard representing the analysis to the user
- [ ] Deploy everything using Streamlit and POSTGIS cloud service
- [ ] Additional geodata layers as contextual overlays (zoning, proximity to landmarks, demographics)
- [ ] Neighborhood-level heatmap view
- [ ] Comparison tool for multiple addresses
