"""Serve the local U.S. power infrastructure map."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import struct
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
HTML_PATH = PACKAGE_DIR / "map_v2.html"
MAPBOX_TOKEN_PLACEHOLDER = "__MAPBOX_TOKEN__"
# Public (pk.) token used when MAPBOX_TOKEN is not set.
DEFAULT_MAPBOX_TOKEN = (
    "pk.eyJ1IjoibWNsbGVyZW5hIiwiYSI6ImNtd3F1M2cyNDB6bTAzMHB4emluMGtxdmEifQ."
    "i-EY2QOnI4Vs2KH9NokNmw"
)
GEOJSON_PATH = DATA_DIR / "transmission_lines.geojson"
EARTH_RADIUS_METERS = 6_378_137.0
WEB_MERCATOR_LIMIT = 20_037_508.342789244
WEB_MERCATOR_CIRCUMFERENCE = 2 * math.pi * EARTH_RADIUS_METERS

POINT_LAYERS = {
    "power-plants": {
        "filename": DATA_DIR / "power_plants" / "Power_Plants.csv",
        "latitude": "LATITUDE",
        "longitude": "LONGITUDE",
        "properties": {
            "plant_code": "PLANT_CODE",
            "name": "NAME",
            "city": "CITY",
            "state": "STATE",
            "status": "STATUS",
            "type": "TYPE",
            "primary_fuel": "PRIM_FUEL",
            "secondary_fuel": "SEC_FUEL",
            "summer_capacity": "SUMMER_CAP",
            "operator": "OPERATOR",
        },
        "numeric": {"summer_capacity"},
    },
    "substations": {
        "filename": DATA_DIR / "substations" / "Substations.csv",
        "latitude": "LATITUDE",
        "longitude": "LONGITUDE",
        "properties": {
            "station_id": "ID",
            "name": "NAME",
            "city": "CITY",
            "state": "STATE",
            "status": "STATUS",
            "type": "TYPE",
            "max_voltage": "MAX_VOLT",
            "min_voltage": "MIN_VOLT",
        },
        "numeric": {"max_voltage", "min_voltage"},
    },
}

DBF_LINE_FIELDS = {
    "ID",
    "TYPE",
    "STATUS",
    "OWNER",
    "VOLTAGE",
    "VOLT_CLASS",
    "SUB_1",
    "SUB_2",
}


@dataclass(frozen=True)
class LineRecord:
    row_index: int
    content_offset: int
    content_length: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def read_point_features(csv_path: Path, layer_name: str) -> dict[str, Any]:
    """Read a point CSV as a compact GeoJSON FeatureCollection."""
    spec = POINT_LAYERS[layer_name]
    features = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            latitude = _number(row.get(spec["latitude"]))
            longitude = _number(row.get(spec["longitude"]))
            if (
                latitude is None
                or longitude is None
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                continue

            properties: dict[str, Any] = {}
            for output_name, column_name in spec["properties"].items():
                value = (row.get(column_name) or "").strip()
                if output_name in spec["numeric"]:
                    properties[output_name] = _number(value)
                else:
                    properties[output_name] = value or None

            features.append(
                {
                    "type": "Feature",
                    "id": properties.get("plant_code") or properties.get("station_id"),
                    "geometry": {
                        "type": "Point",
                        "coordinates": [longitude, latitude],
                    },
                    "properties": properties,
                }
            )
    return {"type": "FeatureCollection", "features": features}


def projected_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert a WGS 84 Web Mercator coordinate into GeoJSON lon/lat."""
    longitude = math.degrees(x / EARTH_RADIUS_METERS)
    bounded_y = min(WEB_MERCATOR_LIMIT, max(-WEB_MERCATOR_LIMIT, y))
    latitude = math.degrees(
        2 * math.atan(math.exp(bounded_y / EARTH_RADIUS_METERS)) - math.pi / 2
    )
    return longitude, latitude


def _lonlat_to_projected(longitude: float, latitude: float) -> tuple[float, float]:
    longitude = min(180.0, max(-180.0, longitude))
    latitude = min(85.05112878, max(-85.05112878, latitude))
    x = EARTH_RADIUS_METERS * math.radians(longitude)
    y = EARTH_RADIUS_METERS * math.log(
        math.tan(math.pi / 4 + math.radians(latitude) / 2)
    )
    return x, y


def _simplify_line(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    """Simplify a line with iterative Ramer-Douglas-Peucker."""
    if len(points) <= 2:
        return points

    tolerance_squared = tolerance * tolerance
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        start_x, start_y = points[start]
        end_x, end_y = points[end]
        dx = end_x - start_x
        dy = end_y - start_y
        length_squared = dx * dx + dy * dy
        greatest_distance = tolerance_squared
        greatest_index = -1

        for index in range(start + 1, end):
            x, y = points[index]
            if length_squared == 0:
                distance_squared = (x - start_x) ** 2 + (y - start_y) ** 2
            else:
                fraction = max(
                    0.0,
                    min(
                        1.0,
                        ((x - start_x) * dx + (y - start_y) * dy)
                        / length_squared,
                    ),
                )
                nearest_x = start_x + fraction * dx
                nearest_y = start_y + fraction * dy
                distance_squared = (x - nearest_x) ** 2 + (y - nearest_y) ** 2
            if distance_squared > greatest_distance:
                greatest_distance = distance_squared
                greatest_index = index

        if greatest_index >= 0:
            keep.add(greatest_index)
            pending.append((start, greatest_index))
            pending.append((greatest_index, end))

    return [point for index, point in enumerate(points) if index in keep]


def parse_polyline_content(
    content: bytes, tolerance: float
) -> list[list[tuple[float, float]]]:
    """Parse and simplify a single ESRI Shapefile PolyLine record."""
    if len(content) < 44:
        raise ValueError("Polyline record is shorter than its fixed header")
    shape_type, = struct.unpack_from("<i", content, 0)
    if shape_type != 3:
        raise ValueError(f"Expected PolyLine shape type 3, found {shape_type}")

    part_count, point_count = struct.unpack_from("<ii", content, 36)
    if part_count < 1 or point_count < 2:
        return []
    parts_end = 44 + part_count * 4
    expected_length = parts_end + point_count * 16
    if expected_length > len(content):
        raise ValueError("Polyline record declares more coordinates than it contains")

    part_starts = [
        start
        for (start,) in struct.iter_unpack(
            "<i", content[44:parts_end]
        )
    ]
    if part_starts[0] != 0 or any(
        start < 0 or start >= point_count for start in part_starts
    ):
        raise ValueError("Polyline record contains invalid part offsets")
    point_data = content[parts_end:expected_length]
    points = list(struct.iter_unpack("<dd", point_data))

    lines = []
    for part_index, start in enumerate(part_starts):
        end = (
            part_starts[part_index + 1]
            if part_index + 1 < len(part_starts)
            else point_count
        )
        if end <= start:
            continue
        simplified = _simplify_line(points[start:end], tolerance)
        if len(simplified) >= 2:
            lines.append([projected_to_lonlat(x, y) for x, y in simplified])
    return lines


def _read_dbf_rows(dbf_path: Path) -> list[dict[str, Any]]:
    """Read only the transmission-line attributes used by map popups."""
    rows = []
    with dbf_path.open("rb") as source:
        header = source.read(32)
        if len(header) != 32:
            raise ValueError(f"Invalid DBF header: {dbf_path}")
        record_count = struct.unpack_from("<I", header, 4)[0]
        header_length, record_length = struct.unpack_from("<HH", header, 8)

        fields = []
        while source.tell() < header_length:
            descriptor = source.read(32)
            if not descriptor or descriptor[0] == 0x0D:
                break
            name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
            fields.append(
                (
                    name,
                    chr(descriptor[11]),
                    descriptor[16],
                    descriptor[17],
                    name in DBF_LINE_FIELDS,
                )
            )
        source.seek(header_length)

        for _ in range(record_count):
            record = source.read(record_length)
            if len(record) != record_length:
                raise ValueError(f"Truncated DBF records: {dbf_path}")
            values: dict[str, Any] = {}
            offset = 1
            for name, field_type, field_length, decimals, include in fields:
                raw = record[offset : offset + field_length].decode(
                    "cp1252", errors="replace"
                ).strip()
                offset += field_length
                if not include:
                    continue
                if not raw or raw == "*":
                    values[name] = None
                elif field_type in {"N", "F"}:
                    try:
                        number = float(raw)
                    except ValueError:
                        values[name] = raw
                    else:
                        values[name] = (
                            int(number)
                            if field_type == "N" and decimals == 0
                            else number
                        )
                else:
                    values[name] = raw
            rows.append(values)
    return rows


def _build_line_index(shapefile_path: Path) -> tuple[list[LineRecord], int]:
    """Index shapefile record bounds without retaining the large geometries."""
    index = []
    record_count = 0
    with shapefile_path.open("rb") as source:
        header = source.read(100)
        if len(header) != 100:
            raise ValueError(f"Invalid shapefile header: {shapefile_path}")
        shape_type, = struct.unpack_from("<i", header, 32)
        if shape_type != 3:
            raise ValueError(
                f"Expected a PolyLine shapefile (shape type 3), found {shape_type}"
            )

        while True:
            record_header = source.read(8)
            if not record_header:
                break
            if len(record_header) != 8:
                raise ValueError(f"Truncated shapefile record header: {shapefile_path}")
            _, content_words = struct.unpack(">ii", record_header)
            content_length = content_words * 2
            if content_length < 4:
                raise ValueError(f"Invalid shapefile record length: {content_length}")
            content_offset = source.tell()
            fixed_header = source.read(min(content_length, 36))
            if len(fixed_header) != min(content_length, 36):
                raise ValueError(f"Truncated shapefile record: {shapefile_path}")
            shape, = struct.unpack_from("<i", fixed_header, 0)
            if shape == 3:
                if content_length < 44:
                    raise ValueError("Polyline record is shorter than its fixed header")
                xmin, ymin, xmax, ymax = struct.unpack_from(
                    "<dddd", fixed_header, 4
                )
                index.append(
                    LineRecord(
                        row_index=record_count,
                        content_offset=content_offset,
                        content_length=content_length,
                        xmin=xmin,
                        ymin=ymin,
                        xmax=xmax,
                        ymax=ymax,
                    )
                )
            source.seek(content_offset + content_length)
            record_count += 1
    return index, record_count


class MapRepository:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._point_cache: dict[str, dict[str, Any]] = {}
        self._line_index: list[LineRecord] | None = None
        self._line_properties: list[dict[str, Any]] | None = None
        self._line_lock = threading.Lock()

    def points(self, layer_name: str) -> dict[str, Any]:
        if layer_name not in POINT_LAYERS:
            raise ValueError(f"Unsupported point layer: {layer_name}")
        if layer_name not in self._point_cache:
            spec = POINT_LAYERS[layer_name]
            self._point_cache[layer_name] = read_point_features(
                self.data_dir / spec["filename"].relative_to(DATA_DIR),
                layer_name,
            )
        return self._point_cache[layer_name]

    def iso_regions(self) -> dict[str, Any]:
        features = []
        region_dir = self.data_dir / "iso_regions"
        region_names = {
            "caiso": "CAISO",
            "ercot": "ERCOT",
            "frcc": "FRCC",
            "isone": "ISO New England",
            "miso": "MISO",
            "northerngrid": "NorthernGrid",
            "nyiso": "NYISO",
            "pjm": "PJM",
            "sertp": "SERTP",
            "spp": "SPP",
            "westconnect": "WestConnect",
        }
        for path in sorted(region_dir.glob("*.json")):
            collection = json.loads(path.read_text(encoding="utf-8"))
            if collection.get("type") != "FeatureCollection":
                raise ValueError(f"{path.name} is not a GeoJSON FeatureCollection")
            region_name = region_names.get(path.stem, path.stem)
            for feature in collection.get("features", []):
                enriched = dict(feature)
                enriched["properties"] = {
                    **(feature.get("properties") or {}),
                    "region": region_name,
                }
                features.append(enriched)
        return {"type": "FeatureCollection", "features": features}

    def _ensure_line_index(self) -> None:
        if self._line_index is not None and self._line_properties is not None:
            return
        with self._line_lock:
            if self._line_index is not None and self._line_properties is not None:
                return
            line_dir = self.data_dir / "tx_lines"
            shapefile_path = line_dir / "Electric_Power_Transmission_Lines_A.shp"
            dbf_path = line_dir / "Electric_Power_Transmission_Lines_A.dbf"
            line_index, record_count = _build_line_index(shapefile_path)
            line_properties = _read_dbf_rows(dbf_path)
            if record_count != len(line_properties):
                raise ValueError(
                    "Transmission shapefile and DBF record counts do not match "
                    f"({record_count} != {len(line_properties)})"
                )
            self._line_index = line_index
            self._line_properties = line_properties

    def transmission_lines(
        self, iso: str | None = None, bbox: tuple[float, float, float, float] | None = None, zoom: float | None = None
    ) -> dict[str, Any]:
        self._ensure_line_index()
        line_index = self._line_index
        line_properties = self._line_properties
        if line_index is None or line_properties is None:
            raise RuntimeError("Transmission line index did not initialize")

        # ISO-based filtering
        if iso:
            iso_regions = {
                "caiso": (-124, 32, -114, 43),
                "ercot": (-104, 25, -93, 36),
                "frcc": (-87, 24, -79, 29),
                "isone": (-73, 41, -67, 46),
                "miso": (-102, 36, -79, 49),
                "northerngrid": (-104, 42, -99, 46),
                "nyiso": (-80, 40, -71, 45),
                "pjm": (-84, 38, -73, 43),
                "sertp": (-88, 31, -78, 40),
                "spp": (-104, 30, -93, 44),
                "westconnect": (-125, 30, -104, 50),
            }
            if iso not in iso_regions:
                raise ValueError(f"Unknown ISO region: {iso}")
            bbox = iso_regions[iso]
            zoom = 5.0  # Default zoom for ISO queries

        if bbox is None:
            raise ValueError("Either iso or bbox must be specified")

        west, south, east, north = bbox
        min_x, min_y = _lonlat_to_projected(west, south)
        max_x, max_y = _lonlat_to_projected(east, north)
        
        tolerance = max(
            50.0,
            WEB_MERCATOR_CIRCUMFERENCE
            / (256 * 2 ** max(0.0, min(zoom or 5.0, 22.0)))
            * 2.0,
        )
        
        min_voltage = 0
        if zoom is None or zoom < 6:
            min_voltage = 115
        elif zoom < 8:
            min_voltage = 69
        elif zoom < 10:
            min_voltage = 20

        features = []
        with (
            self.data_dir.joinpath(
                "tx_lines", "Electric_Power_Transmission_Lines_A.shp"
            ).open("rb") as source
        ):
            for record in line_index:
                if (
                    record.xmax < min_x
                    or record.xmin > max_x
                    or record.ymax < min_y
                    or record.ymin > max_y
                ):
                    continue
                source.seek(record.content_offset)
                content = source.read(record.content_length)
                lines = parse_polyline_content(content, tolerance)
                if not lines:
                    continue
                properties = line_properties[record.row_index]
                
                voltage = properties.get("VOLTAGE")
                if voltage:
                    try:
                        v = float(voltage)
                        if v < min_voltage:
                            continue
                    except (ValueError, TypeError):
                        pass
                
                feature_properties = {
                    "line_id": properties.get("ID"),
                    "type": properties.get("TYPE"),
                    "status": properties.get("STATUS"),
                    "owner": properties.get("OWNER"),
                    "voltage": properties.get("VOLTAGE"),
                    "voltage_class": properties.get("VOLT_CLASS"),
                    "from_substation": properties.get("SUB_1"),
                    "to_substation": properties.get("SUB_2"),
                }
                features.append(
                    {
                        "type": "Feature",
                        "id": record.row_index + 1,
                        "geometry": {
                            "type": "LineString" if len(lines) == 1 else "MultiLineString",
                            "coordinates": lines[0] if len(lines) == 1 else lines,
                        },
                        "properties": feature_properties,
                    }
                )
        return {"type": "FeatureCollection", "features": features}


def _parse_bbox(query: dict[str, list[str]]) -> tuple[float, float, float, float]:
    values = query.get("bbox", [])
    if len(values) != 1:
        raise ValueError("A bbox query parameter is required")
    parts = values[0].split(",")
    if len(parts) != 4:
        raise ValueError("bbox must contain west,south,east,north")
    bbox = tuple(_number(value) for value in parts)
    if any(value is None for value in bbox):
        raise ValueError("bbox coordinates must be finite numbers")
    west, south, east, north = bbox
    west = max(-180, min(180, west))
    east = max(-180, min(180, east))
    south = max(-90, min(90, south))
    north = max(-90, min(90, north))
    if not (west < east and south < north):
        raise ValueError("bbox bounds are degenerate (west must be less than east, south less than north)")
    return west, south, east, north


class MapHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    repository: MapRepository


class MapRequestHandler(BaseHTTPRequestHandler):
    server: MapHTTPServer

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/":
            token = os.environ.get("MAPBOX_TOKEN", "").strip() or DEFAULT_MAPBOX_TOKEN
            html = HTML_PATH.read_text(encoding="utf-8").replace(
                f'"{MAPBOX_TOKEN_PLACEHOLDER}"', json.dumps(token)
            )
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if request.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if request.path == "/health":
            self._send_bytes(b"ok", "text/plain; charset=utf-8")
            return

        if request.path == "/api/power-plants":
            result = self.server.repository.points("power-plants")
        elif request.path == "/api/substations":
            result = self.server.repository.points("substations")
        elif request.path == "/api/iso-regions":
            result = self.server.repository.iso_regions()
        elif request.path == "/api/transmission-lines-geojson":
            # Serve pre-computed transmission lines GeoJSON
            if not GEOJSON_PATH.exists():
                self.send_error(503, "Transmission lines data not ready")
                return
            self._send_file(GEOJSON_PATH, "application/geo+json; charset=utf-8")
            return
        elif request.path == "/api/transmission-lines":
            query = parse_qs(request.query)
            iso = None
            bbox = None
            zoom = None
            
            iso_values = query.get("iso", [])
            if iso_values and len(iso_values) == 1:
                iso = iso_values[0]
            
            if not iso:
                try:
                    bbox = _parse_bbox(query)
                    zoom_values = query.get("zoom", [])
                    if len(zoom_values) == 1:
                        zoom = _number(zoom_values[0])
                        if zoom is None or not 0 <= zoom <= 22:
                            raise ValueError("zoom must be a finite number between 0 and 22")
                except ValueError as error:
                    self.send_error(400, str(error))
                    return
            
            try:
                result = self.server.repository.transmission_lines(iso=iso, bbox=bbox, zoom=zoom)
            except ValueError as error:
                self.send_error(400, str(error))
                return
        else:
            self.send_error(404, "Not found")
            return

        payload = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if "gzip" in self.headers.get("Accept-Encoding", "").lower():
            payload = gzip.compress(payload, compresslevel=5)
            self._send_bytes(
                payload,
                "application/geo+json; charset=utf-8",
                content_encoding="gzip",
            )
        else:
            self._send_bytes(payload, "application/geo+json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str) -> None:
        self._send_bytes(path.read_bytes(), content_type)

    def _send_bytes(
        self,
        payload: bytes,
        content_type: str,
        content_encoding: str | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[map] {self.address_string()} - {format_string % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DART U.S. power map")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser automatically"
    )
    args = parser.parse_args()

    server = MapHTTPServer(("127.0.0.1", args.port), MapRequestHandler)
    server.repository = MapRepository()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Power map ready at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the power map.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
