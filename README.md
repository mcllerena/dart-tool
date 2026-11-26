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

## Map data-source links

These are the source pages linked from the map's **About → Data sources** panel.

### Infrastructure

- [Electric Substations | 543rd Data Site](https://543rd-gpc-hub-543rd.hub.arcgis.com/datasets/electric-substations/explore?location=39.149545%2C-9.857174%2C2)
- [U.S. Electric Power Transmission Lines | CMRA](https://resilience.climate.gov/datasets/d4090758322c4d32a4cd002ffaa0aa12_0/about)
- [Electricity Transmission Lines | UNDP GeoHub](https://geohub.data.undp.org/data/4bd462e63c0b8ebea1295fdf61ca6b61)
- [Power Grid Projects Map | Apollo Technical](https://www.apollotechnical.com/power-grid-projects-map/#power-grid-projects-map)
- [Electric Power Transmission Lines | ArcGIS](https://www.arcgis.com/home/item.html?id=730d3401e03544469dd791b51fe0cf54#overview)
- [Offshore Wind Export Cables | BOEM](https://boem-metaport-boem.hub.arcgis.com/datasets/offshore-wind-export-cables-proposed-or-installed/about)

### Power plants

- [EIA-860M: Monthly Update to the Preliminary Monthly Electric Generator Inventory](https://www.eia.gov/electricity/data/eia860M/)
- [Power Plants in the U.S. | ArcGIS Hub](https://hub.arcgis.com/datasets/b063316fac7345dba4bae96eaa813b2f/explore?location=44.619558%2C-118.495999%2C4)
- [U.S. Power Plants | NETL ArcGIS Portal](https://arcgis.netl.doe.gov/portal/home/item.html?id=f4dc5f7fc1944f1eaac3e09f386d4f75&view=list&sortOrder=desc&sortField=defaultFSOrder#data)
