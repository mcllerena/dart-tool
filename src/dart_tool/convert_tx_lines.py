"""Convert transmission lines shapefile to GeoJSON."""

import json
import math
import struct
from pathlib import Path
from typing import Any

EARTH_RADIUS_METERS = 6_378_137.0
WEB_MERCATOR_LIMIT = 20_037_508.342789244
WEB_MERCATOR_CIRCUMFERENCE = 2 * math.pi * EARTH_RADIUS_METERS

DATA_DIR = Path(__file__).resolve().parent / "data"


def _lonlat_to_projected(lon: float, lat: float) -> tuple[float, float]:
    """Convert lon/lat to Web Mercator projection."""
    x = lon * WEB_MERCATOR_LIMIT / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * WEB_MERCATOR_LIMIT / math.pi
    return x, y


def _projected_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert Web Mercator projection back to lon/lat."""
    lon = x * 180.0 / WEB_MERCATOR_LIMIT
    lat = math.degrees(2.0 * math.atan(math.exp(y * math.pi / WEB_MERCATOR_LIMIT)) - math.pi / 2.0)
    return lon, lat


def _simplify_line(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Simplify line using Douglas-Peucker algorithm."""
    if len(points) <= 2:
        return points

    def perpendicular_distance(point: tuple[float, float], line_start: tuple[float, float], line_end: tuple[float, float]) -> float:
        px, py = point
        x1, y1 = line_start
        x2, y2 = line_end
        if x1 == x2 and y1 == y2:
            return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
        t = max(0, min(1, ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / ((x2 - x1) ** 2 + (y2 - y1) ** 2)))
        closest_x = x1 + t * (x2 - x1)
        closest_y = y1 + t * (y2 - y1)
        return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)

    max_distance = 0
    max_index = 0
    for i in range(1, len(points) - 1):
        distance = perpendicular_distance(points[i], points[0], points[-1])
        if distance > max_distance:
            max_distance = distance
            max_index = i

    if max_distance > tolerance:
        left_part = _simplify_line(points[:max_index + 1], tolerance)
        right_part = _simplify_line(points[max_index:], tolerance)
        return left_part[:-1] + right_part
    else:
        return [points[0], points[-1]]


def parse_polyline_content(content: bytes, tolerance: float) -> list[list[tuple[float, float]]]:
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
            lines.append([_projected_to_lonlat(x, y) for x, y in simplified])
    return lines


def _read_dbf_rows(dbf_path: Path) -> list[dict[str, Any]]:
    """Read transmission line attributes from DBF."""
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


def _build_line_index(shapefile_path: Path) -> tuple[list[tuple[int, int, float, float, float, float]], int]:
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
                    (
                        record_count,
                        content_offset,
                        content_length,
                        xmin,
                        ymin,
                        xmax,
                        ymax,
                    )
                )
            source.seek(content_offset + content_length)
            record_count += 1
    return index, record_count


def convert_to_geojson() -> dict[str, Any]:
    """Convert transmission lines shapefile to GeoJSON."""
    line_dir = DATA_DIR / "tx_lines"
    shapefile_path = line_dir / "Electric_Power_Transmission_Lines_A.shp"
    dbf_path = line_dir / "Electric_Power_Transmission_Lines_A.dbf"
    
    print("Building shapefile index...")
    line_index, record_count = _build_line_index(shapefile_path)
    
    print(f"Reading {record_count} DBF records...")
    line_properties = _read_dbf_rows(dbf_path)
    
    if record_count != len(line_properties):
        raise ValueError(
            f"Record count mismatch: {record_count} != {len(line_properties)}"
        )
    
    print(f"Converting {len(line_index)} transmission lines to GeoJSON...")
    features = []
    tolerance = 100.0  # Moderate simplification
    
    with shapefile_path.open("rb") as source:
        for idx, (row_index, content_offset, content_length, xmin, ymin, xmax, ymax) in enumerate(line_index):
            if (idx + 1) % 1000 == 0:
                print(f"  Processed {idx + 1} / {len(line_index)} lines...")
            
            source.seek(content_offset)
            content = source.read(content_length)
            lines = parse_polyline_content(content, tolerance)
            
            if not lines:
                continue
            
            properties = line_properties[row_index]
            
            # Filter by voltage level (keep 115 kV and above for now)
            voltage = properties.get("VOLTAGE")
            if voltage:
                try:
                    v = float(voltage)
                    if v < 115:
                        continue
                except (ValueError, TypeError):
                    pass
            
            feature_properties = {
                "line_id": properties.get("ID"),
                "type": properties.get("TYPE"),
                "status": properties.get("STATUS"),
                "owner": properties.get("OWNER"),
                "voltage": int(float(properties.get("VOLTAGE", 0))) if properties.get("VOLTAGE") else None,
                "voltage_class": properties.get("VOLT_CLASS"),
                "from_substation": properties.get("SUB_1"),
                "to_substation": properties.get("SUB_2"),
            }
            
            features.append(
                {
                    "type": "Feature",
                    "id": row_index + 1,
                    "geometry": {
                        "type": "LineString" if len(lines) == 1 else "MultiLineString",
                        "coordinates": lines[0] if len(lines) == 1 else lines,
                    },
                    "properties": feature_properties,
                }
            )
    
    result = {"type": "FeatureCollection", "features": features}
    print(f"Generated {len(features)} features")
    return result


if __name__ == "__main__":
    geojson = convert_to_geojson()
    output_path = DATA_DIR / "transmission_lines.geojson"
    
    print(f"Writing to {output_path}...")
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"), ensure_ascii=False)
    
    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Done! File size: {file_size_mb:.2f} MB")
