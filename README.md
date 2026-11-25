# Transmission Data Workaround

GitHub blocks files larger than 100 MB. The full transmission GeoJSON exceeded
that limit, so the map now reads `Electric_Power_Transmission_Lines.zip`
instead. The server uses `pyshp` to read the archive, converts its Web Mercator
coordinates to WGS84, and serves map-ready GeoJSON.

Keep the ZIP tracked. The generated `tx_lines_complete_hifld.geojson` is ignored
by Git.
