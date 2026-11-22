import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.fetch_arcgis_power_plants import export_power_plant_files


class ArcGisPowerPlantExportTests(unittest.TestCase):
    def test_export_writes_geojson_and_csv_with_geometry_and_attributes(self):
        feature = {
            "type": "Feature",
            "id": 7,
            "geometry": {
                "type": "Point",
                "coordinates": [-160.4972, 55.339722],
                "crs": None,
            },
            "properties": {
                "objectid": 7,
                "plant_name": "Sand Point",
                "primsource": "Petroleum",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            geojson_path = Path(directory) / "plants.geojson"
            csv_path = Path(directory) / "plants.csv"
            with patch(
                "dart_tool.fetch_arcgis_power_plants.iter_feature_batches",
                return_value=[[feature]],
            ):
                exported_count = export_power_plant_files([7], geojson_path, csv_path)

            geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
            with csv_path.open(encoding="utf-8", newline="") as source:
                csv_row = next(csv.DictReader(source))

        self.assertEqual(exported_count, 1)
        self.assertEqual(len(geojson["features"]), 1)
        self.assertNotIn("crs", geojson["features"][0]["geometry"])
        self.assertEqual(csv_row["plant_name"], "Sand Point")
        self.assertEqual(
            json.loads(csv_row["geometry"])["coordinates"],
            [-160.4972, 55.339722],
        )

    def test_incomplete_export_does_not_leave_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            geojson_path = Path(directory) / "plants.geojson"
            csv_path = Path(directory) / "plants.csv"
            with patch("dart_tool.fetch_arcgis_power_plants.iter_feature_batches", return_value=[]):  # noqa: SIM117
                with self.assertRaisesRegex(RuntimeError, "expected 1"):
                    export_power_plant_files([7], geojson_path, csv_path)

            self.assertFalse(geojson_path.exists())
            self.assertFalse(csv_path.exists())
            self.assertFalse(geojson_path.with_name("plants.geojson.tmp").exists())
            self.assertFalse(csv_path.with_name("plants.csv.tmp").exists())

    def test_export_preserves_records_without_source_geometry(self):
        feature = {
            "type": "Feature",
            "id": 8,
            "geometry": None,
            "properties": {"objectid": 8, "plant_name": "Plant without coordinates"},
        }
        with tempfile.TemporaryDirectory() as directory:
            geojson_path = Path(directory) / "plants.geojson"
            csv_path = Path(directory) / "plants.csv"
            with patch(
                "dart_tool.fetch_arcgis_power_plants.iter_feature_batches",
                return_value=[[feature]],
            ):
                exported_count = export_power_plant_files([8], geojson_path, csv_path)

            geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
            with csv_path.open(encoding="utf-8", newline="") as source:
                csv_row = next(csv.DictReader(source))

        self.assertEqual(exported_count, 1)
        self.assertIsNone(geojson["features"][0]["geometry"])
        self.assertEqual(csv_row["geometry"], "null")


if __name__ == "__main__":
    unittest.main()
