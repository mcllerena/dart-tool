# DART Grid Atlas

Run the interactive U.S. power infrastructure map from the repository root:

```powershell
python src\dart_tool\map.py
```

The map opens in your browser at <http://127.0.0.1:8765/> and serves its data
locally. Use the navigation buttons to switch between **Power Plants Map** and
**Infrastructure Map**. The theme control on the right switches between light
and dark map styles.

The page (`src\dart_tool\map_v2.html`) is rendered with
[Mapbox GL JS](https://github.com/mapbox/mapbox-gl-js) (WebGL). It reads the
bundled power plant CSV, the ISO-region GeoJSON files, and a precompiled
`src\dart_tool\data\transmission_lines.geojson`. Regenerate that file after
updating the transmission line shapefile in `src\dart_tool\data\tx_lines`:

```powershell
python src\dart_tool\convert_tx_lines.py
```

No Python packages are needed. An internet connection is required for the
Mapbox GL JS library and basemap tiles. Stop the local server with **Ctrl+C**.

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