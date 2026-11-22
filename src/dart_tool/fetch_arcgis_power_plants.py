"""Download NETL's public U.S. power plants layer as GeoJSON and CSV."""

import csv
import json
import os
from pathlib import Path
from typing import Any

if __package__:
    from .arcgis_rest import fetch_object_ids, iter_feature_batches
else:
    from arcgis_rest import fetch_object_ids, iter_feature_batches

LAYER_URL = (
    "https://arcgis.netl.doe.gov/server/rest/services/Hosted/"
    "US_Power_Plants/FeatureServer/0"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "power_plants"
GEOJSON_PATH = OUTPUT_DIR / "power_plants_arcgis_data.geojson"
CSV_PATH = OUTPUT_DIR / "power_plants_arcgis_data.csv"
EXPECTED_RECORD_COUNT = 14_006
OBJECT_ID_FIELD = "objectid"
RECORDS_PER_REQUEST = 2_000


def export_power_plant_files(
    object_ids: list[int],
    geojson_path: Path = GEOJSON_PATH,
    csv_path: Path = CSV_PATH,
) -> int:
    geojson_temp = geojson_path.with_name(geojson_path.name + ".tmp")
    csv_temp = csv_path.with_name(csv_path.name + ".tmp")
    batch_count = (len(object_ids) + RECORDS_PER_REQUEST - 1) // RECORDS_PER_REQUEST
    geojson_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    exported_count = 0

    try:
        with (
            geojson_temp.open("w", encoding="utf-8", newline="\n") as geojson_output,
            csv_temp.open("w", encoding="utf-8", newline="") as csv_output,
        ):
            geojson_output.write('{"type":"FeatureCollection","features":[')
            csv_writer: csv.DictWriter | None = None
            csv_fieldnames: list[str] | None = None
            is_first = True

            batches = iter_feature_batches(
                LAYER_URL,
                object_ids,
                object_id_field=OBJECT_ID_FIELD,
                records_per_request=RECORDS_PER_REQUEST,
            )
            for batch_number, features in enumerate(batches, start=1):
                for feature in features:
                    geometry = feature.get("geometry")
                    properties = feature.get("properties")
                    if geometry is not None and (
                        not isinstance(geometry, dict) or geometry.get("type") != "Point"
                    ):
                        raise RuntimeError("ArcGIS returned non-point power plant geometry")
                    if not isinstance(properties, dict):
                        raise RuntimeError("ArcGIS returned a power plant without attributes")  # noqa: TRY004

                    if geometry is not None:
                        geometry.pop("crs", None)
                    if csv_writer is None:
                        csv_fieldnames = [*properties.keys(), "geometry"]
                        csv_writer = csv.DictWriter(csv_output, fieldnames=csv_fieldnames)
                        csv_writer.writeheader()
                    if csv_fieldnames is None:
                        raise RuntimeError("CSV field names were not initialized")

                    csv_row: dict[str, Any] = dict(properties)
                    csv_row["geometry"] = (
                        json.dumps(
                            geometry,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        if geometry is not None
                        else "null"
                    )
                    csv_writer.writerow(csv_row)

                    if not is_first:
                        geojson_output.write(",")
                    json.dump(
                        feature,
                        geojson_output,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    is_first = False
                    exported_count += 1

                print(
                    f"Downloaded batch {batch_number}/{batch_count} "
                    f"({exported_count:,}/{len(object_ids):,} plants)"
                )

            if exported_count != len(object_ids):
                raise RuntimeError(
                    f"Export wrote {exported_count} plants; expected {len(object_ids)}"
                )
            geojson_output.write("]}\n")

        os.replace(geojson_temp, geojson_path)
        os.replace(csv_temp, csv_path)
        completed = True
    finally:
        if not completed:
            geojson_temp.unlink(missing_ok=True)
            csv_temp.unlink(missing_ok=True)

    return exported_count


def main() -> None:
    object_ids = fetch_object_ids(LAYER_URL)
    if len(object_ids) != EXPECTED_RECORD_COUNT:
        print(
            f"Notice: the source currently returns {len(object_ids):,} plants; "
            f"{EXPECTED_RECORD_COUNT:,} was expected. Exporting all records available."
        )
    else:
        print(f"Source reports {len(object_ids):,} power plants.")

    count = export_power_plant_files(object_ids)
    print(f"Saved {count:,} plants to:")
    print(f"  {GEOJSON_PATH}")
    print(f"  {CSV_PATH}")


if __name__ == "__main__":
    main()
