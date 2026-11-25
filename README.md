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
