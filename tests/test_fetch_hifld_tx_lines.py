import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.fetch_hifld_tx_lines import export_geojson


class HifldTransmissionExportTests(unittest.TestCase):
    def test_export_preserves_features_and_attributes(self):
        features = [
            {
                "type": "Feature",
                "id": object_id,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-100.0, 40.0], [-99.5, 40.5]],
                },
                "properties": {"OBJECTID_1": object_id, "VOLTAGE": 345},
            }
            for object_id in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "lines.geojson"
            with patch(
                "dart_tool.fetch_hifld_tx_lines.iter_feature_batches",
                return_value=[features],
            ):
                exported_count = export_geojson([1, 2], output_path)

            collection = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exported_count, 2)
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual([feature["id"] for feature in collection["features"]], [1, 2])
        self.assertEqual(collection["features"][0]["properties"]["VOLTAGE"], 345)
        self.assertEqual(
            collection["features"][0]["geometry"]["coordinates"],
            [[-100.0, 40.0], [-99.5, 40.5]],
        )

    def test_incomplete_batch_does_not_replace_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "lines.geojson"
            with patch("dart_tool.fetch_hifld_tx_lines.iter_feature_batches", return_value=[]):  # noqa: SIM117
                with self.assertRaisesRegex(RuntimeError, "wrote 0 features"):
                    export_geojson([1], output_path)

            self.assertFalse(output_path.exists())
            self.assertFalse(output_path.with_name("lines.geojson.tmp").exists())


if __name__ == "__main__":
    unittest.main()
