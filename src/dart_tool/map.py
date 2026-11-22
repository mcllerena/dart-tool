"""Serve the local U.S. power infrastructure map."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import os
import posixpath
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
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
GEOJSON_PATH = DATA_DIR / "tx_lines" / "tx_lines_complete_hifld.geojson"
PLANT_SHEET_STATUSES = {
    "Operating": "operating",
    "Operating_PR": "operating",
    "Planned": "planned",
    "Planned_PR": "planned",
    "Retired": "retired",
    "Retired_PR": "retired",
    "Canceled or Postponed": "canceled",
}
PLANT_STATUS_FILES = {
    "operating": ("us_operating_pp.geojson", "pr_operating_pp.geojson"),
    "planned": ("us_planned_pp.geojson", "pr_planned_pp.geojson"),
    "retired": ("us_retired_pp.geojson", "pr_retired_pp.geojson"),
    "canceled": ("us_canceled_pp.geojson",),
}
XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
logger = logging.getLogger(__name__)

POINT_LAYERS = {
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


def _number(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _column_index(cell_reference: str) -> int:
    column = 0
    for character in cell_reference:
        if not character.isalpha():
            break
        column = column * 26 + ord(character.upper()) - ord("A") + 1
    if column == 0:
        raise ValueError(f"Invalid Excel cell reference: {cell_reference}")
    return column - 1


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    shared_strings_path = "xl/sharedStrings.xml"
    if shared_strings_path not in archive.namelist():
        return []

    shared_strings = []
    item_tag = f"{{{XLSX_MAIN_NS}}}si"
    text_tag = f"{{{XLSX_MAIN_NS}}}t"
    with archive.open(shared_strings_path) as source:
        for _, element in ET.iterparse(source, events=("end",)):
            if element.tag == item_tag:
                shared_strings.append(
                    "".join(text.text or "" for text in element.iter(text_tag))
                )
                element.clear()
    return shared_strings


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        text_tag = f"{{{XLSX_MAIN_NS}}}t"
        return "".join(text.text or "" for text in cell.iter(text_tag))

    value_element = cell.find(f"{{{XLSX_MAIN_NS}}}v")
    if value_element is None or value_element.text is None:
        return None

    value = value_element.text
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as error:
            raise ValueError(f"Invalid shared string index in cell {cell.attrib.get('r')}") from error
    if cell_type in {"str", "e"}:
        return value

    number = _number(value)
    if number is None:
        return value
    return int(number) if number.is_integer() else number


def _workbook_sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    relationship_id = f"{{{XLSX_REL_NS}}}id"
    sheet_tag = f"{{{XLSX_MAIN_NS}}}sheet"
    relationship_tag = f"{{{XLSX_PACKAGE_REL_NS}}}Relationship"

    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = next(
        (element for element in workbook.iter(sheet_tag) if element.attrib.get("name") == sheet_name),
        None,
    )
    if sheet is None:
        raise ValueError(f"Workbook does not contain a {sheet_name!r} sheet")
    relationship_id_value = sheet.attrib.get(relationship_id)
    if not relationship_id_value:
        raise ValueError(f"Workbook sheet {sheet_name!r} has no relationship")

    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = next(
        (
            element.attrib.get("Target")
            for element in relationships.iter(relationship_tag)
            if element.attrib.get("Id") == relationship_id_value
        ),
        None,
    )
    if not target:
        raise ValueError(f"Workbook sheet {sheet_name!r} has no worksheet target")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_plant_sheet_features(xlsx_path: Path, sheet_name: str) -> dict[str, Any]:
    """Create one point feature for each coordinate-valid row in a plant sheet."""
    required_columns = {
        "Plant ID",
        "Plant Name",
        "Plant State",
        "County",
        "Generator ID",
        "Technology",
        "Energy Source Code",
        "Prime Mover Code",
        "Net Summer Capacity (MW)",
        "Latitude",
        "Longitude",
    }
    columns = {
        "name": "Plant Name",
        "entity_id": "Entity ID",
        "entity_name": "Entity Name",
        "state": "Plant State",
        "county": "County",
        "status": "Status",
        "technology": "Technology",
        "energy_source": "Energy Source Code",
        "prime_mover": "Prime Mover Code",
        "balancing_authority": "Balancing Authority Code",
        "sector": "Sector",
        "operating_year": "Operating Year",
        "planned_retirement_year": "Planned Retirement Year",
    }
    capacity_columns = {
        "nameplate_capacity": "Nameplate Capacity (MW)",
        "net_summer_capacity": "Net Summer Capacity (MW)",
        "net_winter_capacity": "Net Winter Capacity (MW)",
        "nameplate_energy_capacity": "Nameplate Energy Capacity (MWh)",
        "dc_net_capacity": "DC Net Capacity (MW)",
    }
    cell_tag = f"{{{XLSX_MAIN_NS}}}c"
    row_tag = f"{{{XLSX_MAIN_NS}}}row"
    features = []
    rows_without_coordinates = 0

    with zipfile.ZipFile(xlsx_path) as archive:
        shared_strings = _read_shared_strings(archive)
        worksheet_path = _workbook_sheet_path(archive, sheet_name)
        with archive.open(worksheet_path) as worksheet:
            headers: dict[int, str] | None = None
            for _, row_element in ET.iterparse(worksheet, events=("end",)):
                if row_element.tag != row_tag:
                    continue
                row_number = int(row_element.attrib.get("r", "0"))
                values = {
                    _column_index(cell.attrib["r"]): _xlsx_cell_value(cell, shared_strings)
                    for cell in row_element.findall(cell_tag)
                    if "r" in cell.attrib
                }

                if row_number == 3:
                    headers = {
                        index: text
                        for index, value in values.items()
                        if (text := _clean_text(value)) is not None
                    }
                    missing_columns = required_columns - set(headers.values())
                    if missing_columns:
                        raise ValueError(
                            "Operating sheet is missing required columns: "
                            + ", ".join(sorted(missing_columns))
                        )
                elif row_number > 3:
                    if headers is None:
                        raise ValueError("Operating sheet has no column header row")
                    row = {
                        header: values.get(index)
                        for index, header in headers.items()
                    }
                    plant_id = _clean_text(row.get("Plant ID"))
                    plant_number = _number(plant_id)
                    if plant_number is not None and plant_number.is_integer():
                        plant_id = str(int(plant_number))

                    latitude = _number(row.get("Latitude"))
                    longitude = _number(row.get("Longitude"))
                    if (
                        latitude is None
                        or longitude is None
                        or not -90 <= latitude <= 90
                        or not -180 <= longitude <= 180
                    ):
                        rows_without_coordinates += 1
                        row_element.clear()
                        continue

                    workbook_fields = {}
                    for column_name in headers.values():
                        value = _clean_text(row.get(column_name))
                        if value is not None:
                            workbook_fields[column_name] = [value]

                    properties: dict[str, Any] = {
                        "plant_id": plant_id,
                        "sheet_status": PLANT_SHEET_STATUSES.get(
                            sheet_name, sheet_name.lower()
                        ),
                        "workbook_fields": workbook_fields,
                    }
                    for output_name, column_name in columns.items():
                        value = _clean_text(row.get(column_name))
                        properties[output_name] = value

                    status = properties["status"]
                    technology = properties["technology"]
                    energy_source = properties["energy_source"]
                    prime_mover = properties["prime_mover"]
                    balancing_authority = properties["balancing_authority"]
                    sector = properties["sector"]
                    properties.update(
                        {
                            "statuses": [status] if status else [],
                            "type": technology,
                            "technologies": [technology] if technology else [],
                            "primary_fuel": energy_source,
                            "energy_sources": [energy_source] if energy_source else [],
                            "prime_movers": [prime_mover] if prime_mover else [],
                            "balancing_authorities": (
                                [balancing_authority] if balancing_authority else []
                            ),
                            "sectors": [sector] if sector else [],
                        }
                    )

                    for output_name, column_name in capacity_columns.items():
                        properties[output_name] = _number(row.get(column_name))

                    operating_year = _number(row.get("Operating Year"))
                    retirement_year = _number(row.get("Planned Retirement Year"))
                    properties["operating_years"] = (
                        [int(operating_year)] if operating_year is not None else []
                    )
                    properties["planned_retirement_years"] = (
                        [int(retirement_year)] if retirement_year is not None else []
                    )
                    properties["summer_capacity"] = properties["net_summer_capacity"]

                    features.append(
                        {
                            "type": "Feature",
                            "id": f"{sheet_name.lower()}-{row_number}",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [longitude, latitude],
                            },
                            "properties": properties,
                        }
                    )

                row_element.clear()

    if headers is None:
        raise ValueError("Operating sheet has no column header row")

    if rows_without_coordinates:
        logger.warning(
            "Skipped %s %s rows without valid coordinates from %s",
            rows_without_coordinates,
            sheet_name,
            xlsx_path,
        )
    return {"type": "FeatureCollection", "features": features}


def read_operating_plant_features(xlsx_path: Path) -> dict[str, Any]:
    """Create one point feature for each coordinate-valid Operating sheet row."""
    return read_plant_sheet_features(xlsx_path, "Operating")


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


class MapRepository:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._point_cache: dict[str, dict[str, Any]] = {}

    def points(self, layer_name: str) -> dict[str, Any]:
        if layer_name == "power-plants":
            return self.plant_features("operating")
        if layer_name not in POINT_LAYERS:
            raise ValueError(f"Unsupported point layer: {layer_name}")
        if layer_name not in self._point_cache:
            spec = POINT_LAYERS[layer_name]
            self._point_cache[layer_name] = read_point_features(
                self.data_dir / spec["filename"].relative_to(DATA_DIR),
                layer_name,
            )
        return self._point_cache[layer_name]

    def plant_features(self, status: str) -> dict[str, Any]:
        if status not in PLANT_STATUS_FILES:
            raise ValueError(f"Unsupported power plant status: {status}")
        cache_key = f"power-plants:{status}"
        if cache_key not in self._point_cache:
            features = []
            for filename in PLANT_STATUS_FILES[status]:
                path = self.data_dir / "power_plants" / filename
                collection = json.loads(path.read_text(encoding="utf-8"))
                if collection.get("type") != "FeatureCollection":
                    raise ValueError(f"{path.name} is not a GeoJSON FeatureCollection")
                sheet_features = collection.get("features")
                if not isinstance(sheet_features, list):
                    raise ValueError(f"{path.name} has no GeoJSON features array")  # noqa: TRY004
                if any(
                    feature.get("properties", {}).get("sheet_status") != status
                    for feature in sheet_features
                ):
                    raise ValueError(f"{path.name} contains an unexpected plant status")
                features.extend(sheet_features)
            self._point_cache[cache_key] = {
                "type": "FeatureCollection",
                "features": features,
            }
        return self._point_cache[cache_key]

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
            status = parse_qs(request.query, keep_blank_values=True).get(
                "status", ["operating"]
            )[0]
            if status not in PLANT_STATUS_FILES:
                self.send_error(400, "Unsupported power plant status")
                return
            result = self.server.repository.plant_features(status)
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
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as error:
            logger.debug("Client disconnected while sending %s response: %s", content_type, error)

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
