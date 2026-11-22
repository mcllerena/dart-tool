"""Download the complete HIFLD transmission-lines layer as GeoJSON."""

import json
import os
from pathlib import Path

if __package__:
    from .arcgis_rest import fetch_object_ids, iter_feature_batches
else:
    from arcgis_rest import fetch_object_ids, iter_feature_batches

LAYER_URL = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "US_Electric_Power_Transmission_Lines/FeatureServer/0"
)
OUTPUT_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "tx_lines"
    / "tx_lines_complete_hifld.geojson"
)
EXPECTED_RECORD_COUNT = 94_621
RECORDS_PER_REQUEST = 1_000


def export_geojson(object_ids: list[int], output_path: Path = OUTPUT_PATH) -> int:
    temp_path = output_path.with_name(output_path.name + ".tmp")
    batch_count = (len(object_ids) + RECORDS_PER_REQUEST - 1) // RECORDS_PER_REQUEST
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    downloaded_count = 0

    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write('{"type":"FeatureCollection","features":[')
            is_first = True
            for batch_number, features in enumerate(
                iter_feature_batches(
                    LAYER_URL,
                    object_ids,
                    object_id_field="OBJECTID_1",
                    records_per_request=RECORDS_PER_REQUEST,
                ),
                start=1,
            ):
                downloaded_count += len(features)
                for feature in features:
                    if not is_first:
                        output.write(",")
                    json.dump(
                        feature,
                        output,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    is_first = False

                print(
                    f"Downloaded batch {batch_number}/{batch_count} "
                    f"({downloaded_count:,}/{len(object_ids):,} features)"
                )
            if downloaded_count != len(object_ids):
                raise RuntimeError(
                    f"Export wrote {downloaded_count} features; expected {len(object_ids)}"
                )
            output.write("]}\n")

        os.replace(temp_path, output_path)
        completed = True
    finally:
        if not completed:
            temp_path.unlink(missing_ok=True)

    return downloaded_count


def main() -> None:
    object_ids = fetch_object_ids(LAYER_URL)
    if len(object_ids) != EXPECTED_RECORD_COUNT:
        print(
            f"Notice: the source currently returns {len(object_ids):,} records; "
            f"{EXPECTED_RECORD_COUNT:,} was expected. Exporting all records available."
        )
    else:
        print(f"Source reports {len(object_ids):,} transmission-line records.")

    count = export_geojson(object_ids)
    print(f"Saved {count:,} features to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
