"""Export the workbook's U.S. and Puerto Rico plant sheets as GeoJSON."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

if __package__:
    from .map import (
        PLANT_SHEET_STATUSES,
        XLSX_MAIN_NS,
        _column_index,
        _number,
        _read_shared_strings,
        _workbook_sheet_path,
        _xlsx_cell_value,
    )
else:
    from map import (
        PLANT_SHEET_STATUSES,
        XLSX_MAIN_NS,
        _column_index,
        _number,
        _read_shared_strings,
        _workbook_sheet_path,
        _xlsx_cell_value,
    )

DATA_DIR = Path(__file__).resolve().parent / "data" / "power_plants"
WORKBOOK_PATH = DATA_DIR / "september_generator2026.xlsx"
OUTPUT_SHEETS = (
    ("Operating", "us_operating_pp.geojson"),
    ("Planned", "us_planned_pp.geojson"),
    ("Retired", "us_retired_pp.geojson"),
    ("Canceled or Postponed", "us_canceled_pp.geojson"),
    ("Operating_PR", "pr_operating_pp.geojson"),
    ("Planned_PR", "pr_planned_pp.geojson"),
    ("Retired_PR", "pr_retired_pp.geojson"),
)
ROW_TAG = f"{{{XLSX_MAIN_NS}}}row"
CELL_TAG = f"{{{XLSX_MAIN_NS}}}c"
REQUIRED_COLUMNS = {"Latitude", "Longitude"}


def _clean_cell_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _write_sheet(
    archive: zipfile.ZipFile,
    shared_strings: list[str],
    sheet_name: str,
    output_path: Path,
) -> tuple[int, int]:
    worksheet_path = _workbook_sheet_path(archive, sheet_name)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    exported_count = 0
    skipped_count = 0
    completed = False

    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write('{"type":"FeatureCollection","features":[')
            first_feature = True
            headers: dict[int, str] | None = None

            with archive.open(worksheet_path) as worksheet:
                for _, row_element in ET.iterparse(worksheet, events=("end",)):
                    if row_element.tag != ROW_TAG:
                        continue

                    row_number = int(row_element.attrib.get("r", "0"))
                    values = {
                        _column_index(cell.attrib["r"]): _xlsx_cell_value(
                            cell, shared_strings
                        )
                        for cell in row_element.findall(CELL_TAG)
                        if "r" in cell.attrib
                    }

                    if row_number == 3:
                        headers = {
                            index: text
                            for index, value in values.items()
                            if (text := _clean_cell_value(value)) is not None
                        }
                        missing_columns = REQUIRED_COLUMNS - set(headers.values())
                        if missing_columns:
                            raise ValueError(
                                f"{sheet_name} sheet is missing required columns: "
                                + ", ".join(sorted(missing_columns))
                            )
                    elif row_number > 3:
                        if headers is None:
                            raise ValueError(
                                f"{sheet_name} sheet has no column header row"
                            )
                        properties = {
                            header: _clean_cell_value(values.get(index))
                            for index, header in headers.items()
                        }
                        latitude = _number(properties.get("Latitude"))
                        longitude = _number(properties.get("Longitude"))
                        if (
                            latitude is None
                            or longitude is None
                            or not -90 <= latitude <= 90
                            or not -180 <= longitude <= 180
                        ):
                            skipped_count += 1
                            row_element.clear()
                            continue

                        properties["Latitude"] = latitude
                        properties["Longitude"] = longitude
                        properties["sheet_status"] = PLANT_SHEET_STATUSES[sheet_name]
                        feature = {
                            "type": "Feature",
                            "id": f"{sheet_name.lower().replace(' ', '-')}-{row_number}",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [longitude, latitude],
                            },
                            "properties": properties,
                        }
                        if not first_feature:
                            output.write(",")
                        json.dump(
                            feature,
                            output,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        first_feature = False
                        exported_count += 1

                    row_element.clear()

            if headers is None:
                raise ValueError(f"{sheet_name} sheet has no column header row")
            output.write("]}\n")

        os.replace(temp_path, output_path)
        completed = True
    finally:
        if not completed:
            temp_path.unlink(missing_ok=True)

    return exported_count, skipped_count


def export_sheet(
    workbook_path: Path,
    sheet_name: str,
    output_path: Path,
) -> tuple[int, int]:
    """Export one workbook sheet, returning (features written, rows skipped)."""
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = _read_shared_strings(archive)
        return _write_sheet(archive, shared_strings, sheet_name, output_path)


def export_workbook(
    workbook_path: Path = WORKBOOK_PATH,
    output_dir: Path = DATA_DIR,
) -> dict[str, tuple[int, int]]:
    """Export all requested workbook sheets to separate GeoJSON files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = _read_shared_strings(archive)
        for sheet_name, filename in OUTPUT_SHEETS:
            result = _write_sheet(
                archive,
                shared_strings,
                sheet_name,
                output_dir / filename,
            )
            results[filename] = result
            exported_count, skipped_count = result
            print(
                f"{sheet_name}: exported {exported_count:,} features; "
                f"skipped {skipped_count:,} rows with invalid coordinates "
                f"to {output_dir / filename}"
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert power-plant workbook sheets to GeoJSON."
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=WORKBOOK_PATH,
        help="Path to the September 2026 generator workbook.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DATA_DIR,
        help="Directory for the exported GeoJSON files.",
    )
    args = parser.parse_args()
    export_workbook(args.workbook, args.output_dir)


if __name__ == "__main__":
    main()
