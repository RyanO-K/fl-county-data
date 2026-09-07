# Florida County Land Data Pipeline

Centralizes public county GIS data — parcel boundaries, lot size, zoning,
land use, and assessed value — into one local SQLite database, for all 67 Florida counties. All sources
are official county/state government GIS services or open data portals;
no scraping of non-public systems, no credentials, no paid endpoints are
used by default.

## Layout

```
fl-county-data/
  .venv/                 - isolated Python environment (requests, pyshp)
  scripts/
    sources.json         - verified source URLs + field mappings, one block per county
    etl.py                - main sync script (run this daily)
    validate_sources.py   - quick smoke test against all configured sources
  data/
    fl_county_data.db     - SQLite output database (now stored at D:l-county-data\data\; override with FL_COUNTY_DB)
  logs/
    etl.log               - append-only run log
```

## Database schema

**`features`** — one row per parcel/zoning/land-use polygon, normalized
core fields plus a raw JSON catch-all:

| column | meaning |
|---|---|
| county | e.g. `miami_dade`, `broward` |
| dataset_type | `parcels`, `zoning`, `land_use`, or `future_land_use` |
| feature_key | source's natural ID (FOLIO/parcel #) or OBJECTID |
| acreage | lot size in acres, where the source publishes it |
| land_use_code / land_use_desc | DOR or county land-use code |
| zoning_code / zoning_desc | zoning district code/description |
| land_value / building_value / total_value | assessed values, where published |
| geometry_geojson | parcel/zone boundary as GeoJSON, reprojected to WGS84 (lat/lon) |
| attributes_json | full raw record from the source, for anything not normalized |
| last_synced_at | UTC timestamp of last successful write |

Unique on `(county, dataset_type, feature_key)` — reruns upsert in place,
they don't duplicate rows.

**`sync_log`** — one row per source per run: start/end time, status,
row count, error text if failed. Query this to monitor daily health.

## Running it

```
.venv\Scripts\python.exe scripts\etl.py            # sync everything
.venv\Scripts\python.exe scripts\etl.py miami_dade  # sync one county only
.venv\Scripts\python.exe scripts\validate_sources.py  # sanity-check endpoints, no writes
```

## Sources, per county (all 67)

`scripts/sources.json` is the single source of truth. Every REST entry in
it was hit through the real ETL code path by `validate_sources.py` on
2026-09-06 (result: 116 REST layers OK, 40 datasets marked `manual`).
None require an API key. The two research batches the counties were
verified in (`sources_batch_a.json`, `sources_batch_b.json`) and the
original 5-county config (`sources.pilot5.backup.json`) are kept for
reference only; the ETL reads `sources.json`.

Per-source config keys:

| Key | Meaning |
|---|---|
| `url` | ArcGIS REST layer URL (MapServer/FeatureServer + layer id) |
| `key_field` | attribute used as the stable per-feature key for upserts |
| `field_map` | normalized column → source attribute name |
| `verify_ssl: false` | server has a self-signed / broken cert chain (Osceola, Monroe) |
| `no_offset_pagination: true` | server rejects `resultOffset`; fetch by object-id batches (Broward land use, Polk) |
| `format: "json"` | ArcGIS 10.3x server rejects `f=geojson`; Esri JSON is converted on the fly (Clay). Auto-detected when omitted. |
| `type: "manual"` | no public REST endpoint; see `note` for the bulk-download / DOR fallback path |

How the ETL copes with server quirks (all automatic): page size is capped
to the layer's `maxRecordCount`; if offset paging fails part-way (Orange
County returns HTTP 400 on some features) it switches to object-id
batches and bisects any batch that still fails so only the individual
unserializable features are skipped and logged; Esri JSON returned in
place of GeoJSON (Putnam) is normalized; features with an empty key are
skipped.

| County | Parcels | Zoning | Land use / FLU | Manual notes |
|---|---|---|---|---|
| Alachua | `services.arcgis.com` | `services1.arcgis.com` | — |  |
| Baker | `services2.arcgis.com` | manual | — | zoning: No public zoning REST/API layer found for Baker County. Property Appraiser GIS map (bakerpa.com/map/index.html) is an ArcGIS web app viewer only; no sep… |
| Bay | `gis.baycountyfl.gov` | `gis.baycountyfl.gov` | `gis.baycountyfl.gov` |  |
| Bradford | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Bradford County in a focused search. Bradford County GIS page (bradfordcountyfl.gov/gis/) exists but no zoning… |
| Brevard | `gis.brevardfl.gov` | `gis.brevardfl.gov` | `gis.brevardfl.gov` |  |
| Broward | `gisweb-adapters.bcpa.net` | `services.arcgis.com` | `gisweb-adapters.bcpa.net` (no_offset_pagination) |  |
| Calhoun | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county; likely no digitized public zoning GIS service. |
| Charlotte | `agis2.charlottecountyfl.gov` | `agis2.charlottecountyfl.gov` | `agis2.charlottecountyfl.gov` |  |
| Citrus | `www45.swfwmd.state.fl.us` | manual | — | zoning: No standalone zoning geometry REST layer found for Citrus County; zoning code is carried as an attribute (ZONING field) on the SWFWMD parcel layer above… |
| Clay | `maps.claycountygov.com:6443` (format) | `maps.claycountygov.com:6443` (format) | — |  |
| Collier | `services2.arcgis.com` | `services2.arcgis.com` | — |  |
| Columbia | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Columbia County in a focused search. |
| DeSoto | `www45.swfwmd.state.fl.us` | manual | — | zoning: No standalone zoning geometry REST layer found for DeSoto County; zoning code is carried as an attribute (ZONING field) on the SWFWMD parcel layer above… |
| Dixie | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county. |
| Duval | `maps.coj.net` | — | — |  |
| Escambia | `gismaps.myescambia.com` | `gismaps.myescambia.com` | `gismaps.myescambia.com` |  |
| Flagler | `services3.arcgis.com` | `services3.arcgis.com` | — |  |
| Franklin | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Franklin County in a focused search. |
| Gadsden | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Gadsden County in a focused search. |
| Gilchrist | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county. |
| Glades | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer confirmed. Very small rural county. |
| Gulf | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Gulf County in a focused search. |
| Hamilton | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county. |
| Hardee | `www45.swfwmd.state.fl.us` | manual | — | zoning: No standalone zoning geometry REST layer found for Hardee County; zoning code is carried as an attribute (ZONING field) on the SWFWMD parcel layer above… |
| Hendry | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found for Hendry County in a focused search. |
| Hernando | `www45.swfwmd.state.fl.us` | `services2.arcgis.com` | `services2.arcgis.com` |  |
| Highlands | `www45.swfwmd.state.fl.us` | manual | — | zoning: No standalone zoning geometry REST layer found for Highlands County; zoning code is carried as an attribute (ZONING field) on the SWFWMD parcel layer ab… |
| Hillsborough | manual | `maps.hillsboroughcounty.org` | — | parcels: No public REST/API layer found. Bulk parcel shapefile + attribute spreadsheet published at https://downloads.hcpafl.org/ (updated on a rolling basis). … |
| Holmes | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county. |
| Indian River | `gisportal.ircgov.com` | `gisportal.ircgov.com` | `gisportal.ircgov.com` |  |
| Jackson | manual | manual | — | parcels: gis.mijackson.org ParcelViewer/Parcels layer advertises Query but every query request fails (checked 2026-09-06). Use FL DOR statewide cadastral/NAL fo… |
| Jefferson | `services5.arcgis.com` | `services5.arcgis.com` | manual | future_land_use: JC_FLUM_view FeatureServer on services5.arcgis.com exposes no layers (empty service, checked 2026-09-06). Request the FLUM shapefile from Jeffe… |
| Lafayette | `services9.arcgis.com` | manual | — | zoning: No public zoning GIS REST layer found. Very small rural county. |
| Lake | `gis.lakecountyfl.gov` | `gis.lakecountyfl.gov` | — |  |
| Lee | `services2.arcgis.com` | `gismapserver.leegov.com` | — |  |
| Leon | `intervector.leoncountyfl.gov` | — | — |  |
| Levy | `services.arcgis.com` | `services.arcgis.com` | `services.arcgis.com` |  |
| Liberty | manual | manual | — | parcels: No public ArcGIS REST/API layer found for Liberty County (pop. ~8,300, one of FL's smallest/most rural counties). Parcel search available via Schneider… |
| Madison | manual | manual | — | parcels: No public ArcGIS REST/API layer found for Madison County. Parcel search via Schneider Corp qPublic: https://qpublic.schneidercorp.com/Application.aspx?… |
| Manatee | `www.mymanatee.org` | `www.mymanatee.org` | `www.mymanatee.org` |  |
| Marion | `gis.marionfl.org` | `gis.marionfl.org` | `gis.marionfl.org` |  |
| Martin | manual | `geoweb.martin.fl.us` | `geoweb.martin.fl.us` | parcels: No public parcel REST endpoint found. Martin County Property Appraiser publishes bulk parcel data downloads at https://www.pa.martin.fl.us/tools-resour… |
| Miami-Dade | `gisweb.miamidade.gov` | `gisweb.miamidade.gov` | — |  |
| Monroe | `mcgis4.monroecounty-fl.gov` (verify_ssl) | `mcgis4.monroecounty-fl.gov` (verify_ssl) | `mcgis4.monroecounty-fl.gov` (verify_ssl) |  |
| Nassau | `maps.ncpafl.com` | `maps.ncpafl.com` | `maps.ncpafl.com` |  |
| Okaloosa | manual | `okgis.myokaloosa.com` | `okgis.myokaloosa.com` | parcels: No working public parcel-boundary REST layer with CAMA attributes found. Okaloosa County Property Appraiser's parcel data/maps are served through qPubl… |
| Okeechobee | `services3.arcgis.com` | `services3.arcgis.com` | `services3.arcgis.com` |  |
| Orange | `ocgis4.ocfl.net` | `ocgis4.ocfl.net` | `ocgis4.ocfl.net` |  |
| Osceola | `gis.osceola.org` (verify_ssl) | `gis.osceola.org` (verify_ssl) | — |  |
| Palm Beach | manual | `maps.co.palm-beach.fl.us` | `maps.co.palm-beach.fl.us` | parcels: Palm Beach County Property Appraiser publishes a parcel base map for free bulk download via GIS page https://pbcpao.gov/departments/gis.htm and open da… |
| Pasco | `mapping.pascopa.com` | `mapping.pascopa.com` | `mapping.pascopa.com` |  |
| Pinellas | `services.arcgis.com` | manual | — | zoning: 'Zoning (Unincorporated)' dataset found on Pinellas County Enterprise GIS open data hub (https://new-pinellas-egis.opendata.arcgis.com/datasets/Pinellas… |
| Polk | `gis.polk-county.net` (no_offset_pagination) | manual | — | zoning: No standalone county-wide 'Zoning' REST layer found on gis.polk-county.net despite checking the Planning, PublicViewer, and All-In-One_Viewer folders' '… |
| Putnam | `pamap.putnam-fl.gov` | manual | — | zoning: No public REST/ArcGIS service found for Putnam County zoning. County's own GIS server (https://pamap.putnam-fl.gov/server/rest/services) exposes only Ae… |
| Santa Rosa | `services.arcgis.com` | `cloud.santarosa.fl.gov` | `services.arcgis.com` |  |
| Sarasota | `services3.arcgis.com` | `ags3.scgov.net` | — |  |
| Seminole | manual | manual | — | parcels: No public ArcGIS REST endpoint found after several probes (gis.seminolecountyfl.gov and egis.seminolecountyfl.gov do not resolve; no county ArcGIS Hub/… |
| St. Johns | `www.gis.sjcfl.us` | `www.gis.sjcfl.us` | `www.gis.sjcfl.us` |  |
| St. Lucie | manual | `slcgis.stlucieco.gov` | `slcgis.stlucieco.gov` | parcels: No public parcel-boundary REST/FeatureServer layer found. St. Lucie County's ArcGIS Hub (data-slc.opendata.arcgis.com, DCAT feed checked) lists ~40 dat… |
| Sumter | `www45.swfwmd.state.fl.us` | `gis.sumtercountyfl.gov` | `gis.sumtercountyfl.gov` |  |
| Suwannee | `services6.arcgis.com` | — | — |  |
| Taylor | manual | manual | — | parcels: No public ArcGIS REST/API layer found for Taylor County. Parcel search via qPublic: https://qpublic.net/fl/taylor/ (Taylor County Property Appraiser's … |
| Union | manual | manual | — | parcels: No public ArcGIS REST/API layer found for Union County, FL (do not confuse with an ArcGIS-hosted 'UnionParcels02102026' / 'ZoningDistrict' service unde… |
| Volusia | `maps1.vcgov.org` | `maps1.vcgov.org` | — |  |
| Wakulla | `services9.arcgis.com` | `services9.arcgis.com` | — |  |
| Walton | `services1.arcgis.com` | `services1.arcgis.com` | `services1.arcgis.com` |  |
| Washington | `services.arcgis.com` | manual | `services2.arcgis.com` | zoning: No public REST/API zoning layer found for Washington County. County Planning Department page (fetched, confirmed working): https://www.washingtonfl.com/… |

**Statewide fallback / cross-reference source (all 67 counties, not yet
wired into the ETL):** Florida Dept. of Revenue Property Tax Oversight
Data Portal (`floridarevenue.com/property/Pages/DataPortal_RequestAssessmentRollGISData.aspx`)
publishes free annual NAL (parcel roll: land use, value, legal description)
and statewide cadastral GIS files for every county. This is the path for
the `manual` datasets above and for backfilling fields a county strips
from its public layer (Broward, Putnam values).

## Statewide parcel values and sales (all 67 counties)

`scripts/dor_values.py` pulls the **Florida Statewide Cadastral** layer
published by the State Geographic Information Office
(`services9.arcgis.com/Gh9awoU677aKree0/.../Florida_Statewide_Cadastral/FeatureServer/0`),
which is every county's parcel roll as submitted to the Dept. of Revenue:
just / assessed / taxable / land value, DOR use code, land square footage,
year built, living area, and the last two recorded sales (price, year,
month, qualification code). It is an official public service with no
key; the snapshot is annual (DOR collects rolls each April; the current
export is "FDOR Cadastral 2025").

- Rows land in the `parcel_values` table keyed on `(county, parcel_id)`,
  about 10.8M rows statewide. Owner names and mailing addresses are
  deliberately not stored; the parcel's own site address is.
- `apply_values_to_features()` then joins values onto `features` rows of
  `dataset_type = 'parcels'` by normalized parcel id (dashes, spaces, and
  dots stripped) into the `just_value`, `assessed_value`, `taxable_value`,
  `sale_price`, `sale_date`, `sale_qual`, `dor_use_code`, `site_address`
  columns. County-published `land_value` / `total_value` are kept when
  present; DOR fills them where the county layer has none (Broward,
  Putnam, Polk...).
- The pull walks `OBJECTID` ranges with four parallel workers
  (`resultOffset` and `CO_NO=` filters both time out on this layer), at
  roughly 15-20k rows/s, so a full refresh takes about 10-15 minutes.
- `etl.py` runs it automatically at the end of every full run (skip with
  `--no-values`; a single-county run skips the pull but still runs the
  join). `python scripts/dor_values.py --join-only` re-runs just the join.
- No official source publishes current asking/listing prices; that is MLS
  data. The public analogs are county Clerk tax-deed auction minimum bids
  and "Lands Available for Taxes" lists, which are not wired in.

**Disk:** the values table adds roughly 4 GB to the database. This machine
had about 7 GB free before the first load, so full parcel-geometry loads
for the big counties will need space freed or the database moved to a
larger drive first.

## Acreage: where it comes from

Most zoning and land-use district layers, and some attribute-stripped
parcel layers, publish no acreage field, so `features.acreage` is filled
from the best available source and `features.acreage_source` says which:

| `acreage_source` | Meaning |
|---|---|
| `county` | the county layer's own acreage field (`field_map.acreage`) |
| `dor` | parcels only: DOR land square footage / 43,560 from the statewide values table, when the county layer has no acreage |
| `geometry` | computed from the stored WGS84 polygon (spherical area on the equal-area Earth radius; within about 0.2% of the county's own projected shape area) |

In the web UI every acreage figure is underlined with a dotted line; hovering
it shows the citation (which county layer and field, the DOR roll, or the
geometry computation) including the source URL, and the feature detail
popup prints the source next to the number. The UI reads provenance from
`/api/sources`, which is generated from `sources.json`.

**City** (`features.city`) comes from the county layer's site-city or
jurisdiction field where one exists (`field_map.city`, mapped on 49
layers; values are title-cased), and otherwise from the DOR roll's site
city for parcels. `etl.backfill_city()` fills it from the raw attributes
already stored, so adding a mapping never requires a re-download.

**Detail popup mini map.** Clicking any Browse Data row opens the record
with its boundary drawn on an OpenStreetMap mini map. Parcel Values rows
show the boundary too once that county's parcel layer is loaded (the
popup looks the parcel up in `features` by normalized parcel id);
until then it says so.

`etl.backfill_acreage()` runs at the end of every ETL run and computes
geometry acreage for anything still missing it; the DOR join upgrades
geometry-derived parcel acreage to `dor` where land square footage exists.

### Compressed JSON columns

`geometry_geojson` and `attributes_json` are stored as zlib blobs (`etl.encode_json` / `etl.decode_json`), about 2.3x smaller for attributes and 4.5x for rounded geometry at 0.07 ms per row. Cells are self-describing (a zlib stream starts with `0x78`, JSON text never does), so plain-text rows from before the change still decode; `scripts/compress_db.py [--vacuum]` rewrites them in place under the run lock. The API decodes on the way out, so the front end still receives JSON text.

### Priority counties

`etl.PRIORITY_COUNTIES` lists the Tampa Bay and Orlando metro counties (Orange, Pinellas, Pasco, Polk, Osceola, Lake, then the ring Hernando, Manatee, Sarasota, Volusia, Brevard, Citrus, Sumter, and last Hillsborough and Seminole, which come from the slower statewide layer). `load_all.py phase2` loads them first, in that order, before the smallest-first sweep, and `make_demo_db.py --budget-mb N --require-parcels` fills the demo budget with them first (only counties whose parcel boundaries are at least 90% loaded are eligible).

### Value join performance

`features.feature_key_norm` stores the normalized parcel id (same function as `parcel_values.parcel_key`) with an index, so the DOR value join is an index lookup (Washington, 43k parcels: 15 s). The join query carries `INDEXED BY idx_pv_key` on purpose: SQLite's UPDATE..FROM planner otherwise takes the parcel_values primary key and scans every DOR row per parcel (15 min for 30k parcels, days for a metro county). Phase 2 logs `[WARN]` when under half of a county's parcels match, which almost always means the layer's `key_field` is not the parcel number (Alachua's is `Name`; `Prop_ID` is internal).

### Fetch strategy for big layers

Layers whose `maxRecordCount` is under 1000 are fetched by object-id batches (`returnIdsOnly`, then `OBJECTID IN (...)`) instead of `resultOffset` paging: offset paging on those servers slows down with depth (Orange County: 1.5 s per 200-row page at the start, 8 s past 80k rows), while id batches cost the same at any depth. Batches run four at a time (`etl.FETCH_WORKERS`) for both county layers and the statewide-geometry fallback; results are written in order. `"no_offset_pagination": true` forces id batches for a source.

### Parallel counties

`load_all.py` runs counties concurrently (`COUNTY_WORKERS`, default 4, env `FL_COUNTY_WORKERS`) in both phases: every county is a different server, so wall clock is bounded by each server's speed. Each worker thread has its own SQLite connection; WAL serialises the short write batches and the 60 s busy timeout covers the longest single write (a metro county's value join, ~15 s). Combined with the 4 in-flight fetches per county, total concurrency is 16 requests, spread over 4 different servers.

### Run lock

`etl.py` (the scheduled daily refresh) and `load_all.py` (initial bulk load) never write at the same time: whichever starts first writes `etl.lock` (pid + name) next to the database, and the other logs `[SKIP] ... holds the database` and exits. A lock whose pid is no longer running is ignored. The Windows task `FLCountyDataETL` has *start when available* on, so a missed 3 AM run fires at next wake; with the lock it simply skips while a bulk load is still running.

### Extra source config keys
- `"where"`: optional ArcGIS SQL filter applied to every query for that layer (used when one service holds more than one county, e.g. the Baker/Nassau parcel layer).
- `"type": "statewide_geometry"`: parcel boundaries pulled from the statewide DOR cadastral layer by OBJECTID for the parcels already in `parcel_values` (13 counties without a county-hosted parcel layer).
- `"key_transform"`: named reordering applied to the normalized key before the DOR join (`swap_sec_rng` for Orange, whose PARCEL field is range-township-section while the state roll is section-township-range). `feature_key` keeps the county's spelling; only `feature_key_norm` is transformed.
- `"exclude_fields"`: extra raw attribute keys to drop on ingest (owner/mailing fields are dropped automatically).

### Privacy: owner and mailing fields are not stored

Raw layer attributes are kept in `attributes_json`, but any key starting with `OWN`, `OWNER`, `MAIL`, `FIDU` or `TAXPAYER` (any spelling, e.g. `OWNERNAME`, `OwnerAddress1`, `MAILADD`), the DOR/FGDL mailing shorthand (`MCITY`, `MZIP`, `OADDR1` ...), and staff/applicant name fields (`EDITOR_NAME`, `CASE_CONTACT`) are dropped on ingest (`etl.is_private_field`; `OWNTYPE`-style ownership-class keys are kept), and a source can list further keys under `"exclude_fields"` in `sources.json` (Polk's Property Appraiser layer excludes `NAME` and `MAIL_ADDR_*`). The statewide DOR values table never pulls owner columns.

### Map rendering (uncapped)

- The map draws every feature that matches the filters, not just the first few hundred. Rows stream from `/api/features/geometry` in keyset-paged chunks of 2,000 and are drawn on a canvas renderer.
- Drawing runs in 40 ms slices with a 40 ms pause between them, so a large render uses roughly half of one CPU core and the page stays responsive.
- Progress shows in a compact meter pinned to the map's top-right corner: a thin bar plus "Loading 12,340 / 110,000 features" and an x that cancels the render (it bumps the `mapRun` generation token, which the render loop checks after every slice and every chunk). The bar is indeterminate until the first chunk returns the total. Nothing overlays the map, so you can pan, zoom and click already-drawn polygons while the rest streams in; interacting never restarts the load. When the render finishes the meter shows the final count and fades out after ~1.5 s, and the hint above the map reads "Rendered on map (N features, T s)".
- The map frames the selected county at most once per load, and never after the user has panned or zoomed, so a long render does not fight with the view you chose.
- When the throttle engages (any render bigger than one slice) a small toast at the bottom of the map explains that drawing is throttled and may take a while, and a "Done" toast reports the final count and time. Toasts are dismissible and never block the map.

### County outlines and resizable maps

- Both maps (the Browse Data map and the mini map in the row detail popup) draw all 67 county boundaries as dashed lines. The selected county is highlighted in blue. At state zoom each county is labelled; hovering shows the full name. Clicking a lot opens the same detail panel as clicking its table row (mini map, values, source attributes). Outlines are display only - a hover label names the county under the cursor, but clicking empty ground does nothing; change counties with the County filter above the map.
- Boundaries come from the U.S. Census Bureau TIGERweb `State_County` service (Counties layer, generalized to ~50 m) and are stored in `scripts/static/fl_counties.geojson`. Regenerate with `python scripts/fetch_county_boundaries.py` (rarely needed).
- Drag the bottom-right corner of either map to change its height; the map re-lays itself out automatically. Fullscreen still works via the panel button.

## Public demo deployment

Live at https://fl-county-data.onrender.com (source: this repo). The full
database is too large for a free host, so the public site runs a subset of
whole counties (boundaries rounded to 6 decimals); everything else is
identical to the local build. The current demo lists the 18 counties whose
parcel boundaries were complete when it was built (~970 MB uncompressed,
108 MB gzipped; Render's free build handled that size fine).

1. `python scripts/make_demo_db.py --out demo/fl_county_demo.db --budget-mb 1100 --require-parcels`
   (priority metros first, then smallest-first, complete counties only; or
   `--counties a,b,c` for an explicit list) then `gzip -k demo/fl_county_demo.db`.
2. Attach the `.gz` to the GitHub Release tagged `demo-data`
   (`gh release upload demo-data demo/fl_county_demo.db.gz --clobber`).
3. `render.yaml` defines the Render free web service. Its build step runs
   `scripts/fetch_demo_db.py`, which downloads `DEMO_DB_URL` into `FL_COUNTY_DB`;
   `DEMO_MODE=1` shows the banner listing the included counties. Render
   free instances sleep after idle, so the first request can take 30-60 s.

Refreshing the public data = rebuild the demo DB, re-upload the release
asset, and trigger a deploy (or push a commit).

## Known gaps

1. **`manual` datasets** (40 of them, listed above) — no public REST
   endpoint. Wire in the FL DOR statewide cadastral/NAL files (annual)
   or the county's bulk shapefile download.
2. **Stripped attributes** — Broward parcels expose FOLIO + geometry
   only; Putnam parcels expose PARCELID + geometry with null values.
   Join to DOR NAL on parcel id for value/land use.
3. **Large layers and daily cadence** — Miami-Dade (943k), Broward
   (556k), Orange (497k), Duval (408k), Osceola (201k) parcel layers are
   large. The daily job should sync zoning/land-use every run and full
   parcel geometry weekly, with a lightweight attribute-only refresh
   (`returnGeometry=false`) in between — the script pulls geometry every
   time by default. Raise the 4h Task Scheduler execution limit once all
   parcel layers are enabled.
4. Data carries no formal open-data license from most of these
   counties — treat it as "public government record, provided as-is"
   (each source's disclaimer is noted in `sources.json`), not as data
   under an explicit reuse license like CC0.
5. **Values are annual** — the DOR roll lags county appraiser sites by up
   to a year. Counties whose own layers carry values/sales (Lee, Orange,
   Pinellas, Miami-Dade, Duval, Charlotte, Marion) could have those mapped
   into `field_map` for fresher numbers; only value fields are mapped today.

## Daily scheduling

See `scripts/setup_daily_task.ps1` — registers a Windows Task Scheduler
job that runs `etl.py` once per day. Check `logs/etl.log` and the
`sync_log` table after each run to confirm it's healthy.

## Running the web UI

A small local Flask app (`scripts/app.py`) serves a read-only JSON API plus
a single-page HTML/JS frontend on top of `D:l-county-data\datal_county_data.db` (path set in `scripts/etl.py`, env `FL_COUNTY_DB` overrides) — a
filterable/searchable table of parcels/zoning/land-use records, a Leaflet
map view of the polygons (colored by zoning/land-use code, per county), a
**Parcel Values** tab over the statewide FL DOR tax-roll (`parcel_values`,
~10.8M rows for all 67 counties: just/assessed/taxable/land value, last two
sales, use code, site address — filterable by county, DOR use code, value
range, sale price range, and free-text parcel id/address search, sortable,
paginated), and a pipeline health dashboard driven by `sync_log`. It works
generically off whatever `(county, dataset_type)` combinations currently
exist in the database, so newly added counties/datasets show up
automatically with no code changes. Browse Data's feature table/map/detail
view also surface `just_value` and last-sale price+date wherever the
`features` rows have been joined to DOR values.

New read-only endpoints backing the Parcel Values tab: `/api/values`
(paginated/filterable rows), `/api/values/counties` (dropdown source),
`/api/values/use_codes` (county-scoped DOR use code dropdown), and
`/api/value/<county>/<parcel_id>` (single-row detail). `/api/status` also
reports live row counts for `dor_values` sync_log entries from
`parcel_values` instead of `features`.

The **Pipeline Status** tab is driven by `/api/status/counties`: one row per
county in `sources.json` (all 67, including counties with nothing loaded yet)
with a cell per dataset — DOR values, parcels, zoning, land use, future land
use — carrying the last successful sync time, the live row count, and a state
(`ok` / `failed` / `running` / `manual` / `not_loaded` / `not_configured`).
The parcels cell also carries the join rate (share of parcel features with a
`total_value`). The payload is cached in-process for 60 s (~1.3 s to build);
the join rate needs a full scan of the parcel rows (~9 s, `total_value` is not
indexed), so it is computed on a background thread and cached for 10 min —
the first poll after startup returns `join_rates_pending: true` and the page
fills the number in on a later refresh. `/api/status` is unchanged.

Start it:

```
.venv\Scripts\pip.exe install flask   # one-time, already done if you ran this after Flask was added
.venv\Scripts\python.exe scripts\app.py
```

Then open **http://127.0.0.1:5000/** in a browser.

Notes:

- The app only ever opens short-lived, read-only SQLite connections
  (`mode=ro` URI, one per request) and never holds a write-capable handle
  open, so it's safe to leave running while `etl.py` runs concurrently
  (daily via Task Scheduler, or manually) and rewrites the database.
- The frontend polls `/api/status/counties` every 20s and the current table page
  every 25s, patching only the values that changed (row counts, "last
  synced" timestamps, sync status) rather than doing a full page
  re-render, so pipeline progress shows up live without a manual reload.
  The Parcel Values table polls the same way, but only every 20s and only
  while that tab is active and no detail popup is open.
- The map view is per-county/dataset and capped at ~1,500 rendered
  polygons at a time (narrow the filters to see more of a large layer) —
  it's not meant to render an entire 500k+ row parcel layer at once.
- An unfiltered Parcel Values query (no county picked) sorts across the
  full ~10.8M-row table with no covering index and can take several
  seconds — pick a county for a fast, index-backed lookup.
- Dev-server only (`app.run(debug=False, threaded=True)` on
  127.0.0.1:5000); no authentication, single local user, matching the
  rest of this pilot. `threaded=True` lets one slow statewide values
  query run without blocking other concurrent requests.
