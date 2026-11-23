# DART Grid Atlas

Run the interactive U.S. power infrastructure map from the repository root:

```powershell
python -m pip install -r requirements.txt
python src\dart_tool\map.py
```

The map opens in your browser at <http://127.0.0.1:8765/> and serves its data
locally. Use the navigation buttons to switch between **Power Plants Map**,
**Infrastructure Map**, and **Nodal Price**. The theme control on the right
switches between light and dark map styles.

The Nodal Price tab retrieves day-ahead and real-time node prices with the
open-source [GridStatus Python library](https://github.com/gridstatus/gridstatus).
PJM price requests require a PJM Data Miner API key in the `PJM_API_KEY`
environment variable; the other supported ISO adapters use public data feeds.
The map uses GridStatus' ERCOT bus-to-substation directory and matches station
names against the local substation coordinate data. It plots only unique,
normalized-name matches within the ISO footprint. Unmatched prices remain
available in the CSV export rather than being assigned guessed locations.

The panel in the top-left corner is the legend and filter for the active tab:

- **Power Plants Map**: click a plant technology to show or hide it, and drag
  the two capacity handles to limit plants to a range of 0 to 1000+ MW. Marker
  sizes rescale to the selected technology's capacity range. Expand **All
  Markets** or **Other/Non-Markets**, then select **Only [+]** beside a region
  to show plants only in that region.
- **Infrastructure Map**: click a voltage class to show or hide its
  transmission lines and substations, and use **Show Substations** to turn the
  substation dots on or off. Substations without a recorded voltage are not
  shown. BOEM offshore wind export cables (`offshore_wind_tx_cables.geojson`)
  are drawn as dashed lines colored by voltage class; **Show Offshore Wind
  Cables** toggles them. DC cables, and cables proposed as AC and/or DC, also
  appear when only **DC** is selected.
- **Nodal Price**: select an available ISO (CAISO, ERCOT, MISO, NYISO, PJM,
  ISO-NE, or SPP) or **All** to show every region together, plus an operating
  date, day-ahead or real-time market, and latest interval or daily-average
  price. For **All**, the selected date is interpreted in each ISO's local
  timezone; regions with unavailable data are reported while available regions
  remain visible. For PJM, set the Data Miner key before starting the server:

  ```powershell
  $env:PJM_API_KEY = "<your PJM Data Miner key>"
  python src\dart_tool\map.py
  ```

  Use **Export selected prices (CSV)** to download matched and unmatched prices.

Click **Reset** next to a panel title to restore that tab's filters to their
defaults. It is greyed out when nothing is filtered.

The page (`src\dart_tool\map_v2.html`) is rendered with
[Mapbox GL JS](https://github.com/mapbox/mapbox-gl-js) (WebGL). It reads the
preconverted power-plant GeoJSON files, the substation CSV, ISO-region GeoJSON
files, and the complete
`src\dart_tool\data\tx_lines\tx_lines_complete_hifld.geojson`.
The large transmission-line file is fetched only when the Infrastructure Map
tab is selected and an AC voltage class is visible. The Infrastructure Map
preloads the 17-feature DC subset from
`src\dart_tool\data\tx_lines\tx_lines_dc.geojson` alongside the SunZia route,
so choosing DC-only reveals the ready layers without parsing or filtering the
much larger AC-line file. DC-only mode shows substations matched to DC-line
endpoints; the substation toggle still controls their visibility.
Substation data is fetched only when those points are enabled in the
Infrastructure Map. Plant-to-region matching first checks
grouped market bounds, then applies the existing polygon containment test to
candidate boundaries.
The DC voltage-class layer includes the SunZia route in
`src\dart_tool\data\tx_lines\sunzia_transmission_project.geojson`. Its geometry
comes from the BLM **SunZia Preferred Route Major Transmission Line (Draft)**
feature layer, whose 53 link segments total about 515 miles. Endpoints within
15 m are joined. The two remaining chains are connected with a straight,
462 m inferred link so the map displays one continuous path; this connector is
explicitly marked as inferred in the GeoJSON and is not surveyed BLM geometry.
The BLM source warns that the draft data may contain errors or omissions and
is not for engineering or operational use. The 525 kV DC voltage, Pattern
Energy operator, and line type attributes come from the Grid Status
transmission map, which attributes its data to OpenStreetMap under the Open
Database License (ODbL). Source links and attributions are retained in the
GeoJSON. The original SunZia ROW polygon and proposed tower points remain
available in `hvdc.csv` as raw project data.

Each coordinate-valid generator row appears as its own map feature; rows with
repeated Plant IDs are not grouped. The map opens on Operating rows. The
side-by-side **Planned** and **Retired** buttons switch to those sheets,
including their Puerto Rico records. A centered **Canceled/Postponed** button
shows that sheet. Clicking the selected status button again returns to
Operating. Technology **Only [+]** controls continue to filter the selected
view. Each marker uses its row's coordinates and capacity, and its popup shows
all non-empty columns from that workbook row. Marker sizes are scaled to the
selected technologies' 95th-percentile capacity so a single high-capacity
outlier does not flatten the size differences among other plants.

`src\dart_tool\excel2geojson.py` converts the workbook into one GeoJSON file per
sheet, retaining the original column names and values as feature properties.
It skips rows without valid latitude/longitude coordinates and writes each file
atomically. The map requests only the active status group, which is cached in
memory by the local server. Run the exporter again after updating the workbook:

```powershell
python src\dart_tool\excel2geojson.py
```

The generated files are stored in
`src\dart_tool\data\power_plants`: `us_operating_pp.geojson`,
`us_planned_pp.geojson`, `us_retired_pp.geojson`, `us_canceled_pp.geojson`,
`pr_operating_pp.geojson`, `pr_planned_pp.geojson`, and
`pr_retired_pp.geojson`.

No Python packages are needed. An internet connection is required for the
Mapbox GL JS library and basemap tiles. Stop the local server with **Ctrl+C**.

## Complete HIFLD transmission lines

`src\dart_tool\fetch_hifld_tx_lines.py` downloads all records and geometries
from the public [Transmission Lines (HIFLD) ArcGIS layer](https://www.arcgis.com/home/item.html?id=d77284d1a97a426eaf960bab4764999c&sublayer=0&dataTabView=table)
and writes a WGS 84 GeoJSON FeatureCollection to
`src\dart_tool\data\tx_lines\tx_lines_complete_hifld.geojson`. GeoJSON preserves
the line geometries and attributes; no Python packages are required.

```powershell
python src\dart_tool\fetch_hifld_tx_lines.py
```

The ArcGIS layer currently reports 94,619 records, two fewer than the
94,621-record target. The script reports this difference and exports every
record currently available from the source.

## NETL U.S. power plants

`src\dart_tool\fetch_arcgis_power_plants.py` downloads the public
[US_Power_Plants ArcGIS layer](https://arcgis.netl.doe.gov/portal/home/item.html?id=f4dc5f7fc1944f1eaac3e09f386d4f75&view=list&sortOrder=desc&sortField=defaultFSOrder#data)
and exports all attributes and WGS 84 point geometries to:

- `src\dart_tool\data\power_plants\power_plants_arcgis_data.geojson`
- `src\dart_tool\data\power_plants\power_plants_arcgis_data.csv`

The CSV includes a `geometry` column containing the point geometry as compact
GeoJSON (or `null` when the source has no coordinates), in addition to the
source attributes. The ArcGIS layer currently reports 14,006 plants.

```powershell
python src\dart_tool\fetch_arcgis_power_plants.py
```

These are separate exports. The map uses the Operating, Planned, Retired,
Canceled or Postponed, Operating_PR, Planned_PR, and Retired_PR sheets of
`september_generator2026.xlsx`; `Power_Plants.csv` remains available as a
separate dataset.

## OpenStreetMap power data

`src\dart_tool\fetch_osm_power.py` downloads the OpenStreetMap power
infrastructure shown in the Power layer of
[Open Infrastructure Map](https://openinframap.org) (the same public data the
GridStatus infrastructure map is derived from) and saves it to
`src\dart_tool\data\osm`:

| File                    | Contents                                                       |
| ----------------------- | -------------------------------------------------------------- |
| `power_lines.geojson`   | Transmission lines and cables (voltage, operator, circuits, …) |
| `substations.geojson`   | Substations as points (name, operator, voltage, type)          |
| `power_plants.geojson`  | Power plants as points (name, source, output in MW)            |
| `metadata.json`         | Source, license, download date and feature counts              |

```powershell
python src\dart_tool\fetch_osm_power.py --tile-size 4         # whole U.S. (several hours)
python src\dart_tool\fetch_osm_power.py --bbox=-71.9,41.1,-71.1,42.1   # small test area
python src\dart_tool\fetch_osm_power.py --merge-only          # rebuild files from the cache
```

The script queries the public Overpass API tile by tile and caches each tile in
`data\osm\tiles`, so rerunning the same command after an interruption only
downloads the missing tiles. Add `--include-minor-lines` to also export
distribution lines. The data is © OpenStreetMap contributors under the
[ODbL](https://www.openstreetmap.org/copyright); keep that attribution wherever
it is displayed.

## Mapbox access token

Mapbox GL JS needs an access token to load Mapbox basemap styles
(`mapbox://styles/mapbox/light-v11` and `dark-v11`). The server reads it from
the `MAPBOX_TOKEN` environment variable and injects it into the page. If the
variable is not set, a shared default public token in
`src\dart_tool\map.py` (`DEFAULT_MAPBOX_TOKEN`) is used.

Every map load counts against the token owner's free tier, so anyone running
their own copy should use their own token:

1. Create a free account at <https://account.mapbox.com/auth/signup/>.
2. Copy the **Default public token** (starts with `pk.`) from
   <https://console.mapbox.com/account/access-tokens/>.
3. Set it before starting the server:

```powershell
# Current terminal only
$env:MAPBOX_TOKEN = "pk.your-token"
python src\dart_tool\map.py

# Or save it for future terminals
setx MAPBOX_TOKEN "pk.your-token"
```

### How the token works

- **Public tokens (`pk.…`)** are designed to be embedded in browser code. Every
  request the map makes to Mapbox (style, fonts, sprites, vector tiles) carries
  the token, which identifies your account for usage and billing.
- **Secret tokens (`sk.…`)** have write/admin scopes and must never be put in
  client-side code or committed to the repository.
- Anyone who views the page source can see a public token. If the map is
  deployed publicly, open the token in the
  [Mapbox console](https://console.mapbox.com/account/access-tokens/) and add
  **URL restrictions** (for example `https://your-domain.com`) so other sites
  cannot use it. Include your local address too (for example
  `http://127.0.0.1:8765`) if the same token is used for development, or keep
  a separate unrestricted token for local work.
- To change the token, create one in the console (the default public scopes
  are enough) and set `MAPBOX_TOKEN`. Rotate it from the console if it is ever
  abused.

### Free tier and billing

Mapbox GL JS is billed by **map loads**. A map load is counted each time a
`mapboxgl.Map` object is initialized, which in this app means each time the
page is opened or refreshed. Within a load, panning, zooming, switching between
the Power Plants and Infrastructure tabs, and toggling the theme are free, and
all vector tile requests are included. A single load lasts up to 12 hours.

| Monthly map loads     | Cost per 1,000 loads |
| --------------------- | -------------------- |
| Up to 50,000          | Free                 |
| 50,001 – 100,000      | $5.00                |
| 100,001 – 200,000     | $4.00                |
| 200,001 – 1,000,000   | $3.00                |
| 1,000,001 – 5,000,000 | $2.50                |
| 5,000,001+            | Contact sales        |

Pricing as listed on <https://www.mapbox.com/pricing> (November 2026); check
that page for current rates.

- The free 50,000 loads per month is far more than local or small-team use
  needs. For example, 20 people opening the map 10 times every workday is
  about 4,400 loads per month.
- The power plant, transmission line, and ISO-region data are served by the
  local Python server and added as GeoJSON sources, so they do not count
  toward Mapbox usage.
- Usage is visible on the
  [statistics page](https://console.mapbox.com/account/statistics/). You are
  only charged for usage above the free tier; set billing alerts in the
  console to be warned before that happens.
- See Mapbox's
  [Manage your web map costs](https://docs.mapbox.com/help/troubleshooting/manage-web-map-costs/)
  guide for more ways to reduce map loads.

Run the standard-library tests with:

```powershell
python -m unittest discover -s tests
```