# Transmission Data Workaround

GitHub blocks files larger than 100 MB. The map uses
`Electric_Power_Transmission_Lines.zip` to build a compact, precompressed WGS84
GeoJSON cache. To rebuild the cache after replacing the ZIP, run:

```powershell
python src\dart_tool\build_tx_line_cache.py
```

The map serves the cache directly, avoiding slow conversion and compression
when the transmission layer is first opened. The oversized
`tx_lines_complete_hifld.geojson` is ignored by Git.

## Data and sources

- [Power-plant data](src/dart_tool/data/power_plants/); the download script uses
  the [NETL U.S. Power Plants layer](https://arcgis.netl.doe.gov/server/rest/services/Hosted/US_Power_Plants/FeatureServer/0).
- [Substations CSV](src/dart_tool/data/substations/Substations.csv); row-level
  source details are in its `SOURCE` column.
- [Transmission-line ZIP](src/dart_tool/data/tx_lines/Electric_Power_Transmission_Lines.zip);
  [HIFLD source layer](https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0).
- [ISO region boundaries](src/dart_tool/data/iso_regions/).
