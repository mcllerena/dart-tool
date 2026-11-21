import csv
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.map import (
    EARTH_RADIUS_METERS,
    parse_polyline_content,
    projected_to_lonlat,
    read_point_features,
)


class MapDataTests(unittest.TestCase):
    def test_point_csv_becomes_geojson_and_skips_invalid_coordinates(self):
        columns = [
            "LATITUDE",
            "LONGITUDE",
            "PLANT_CODE",
            "NAME",
            "CITY",
            "STATE",
            "STATUS",
            "TYPE",
            "PRIM_FUEL",
            "SEC_FUEL",
            "SUMMER_CAP",
            "OPERATOR",
        ]
        rows = [
            {
                "LATITUDE": "40.5",
                "LONGITUDE": "-75.25",
                "PLANT_CODE": "42",
                "NAME": "Example Station",
                "CITY": "Sample City",
                "STATE": "PA",
                "STATUS": "OP",
                "TYPE": "NATURAL GAS",
                "PRIM_FUEL": "NG",
                "SEC_FUEL": "",
                "SUMMER_CAP": "125.5",
                "OPERATOR": "Utility",
            },
            {
                "LATITUDE": "not available",
                "LONGITUDE": "-75",
                "PLANT_CODE": "43",
                "NAME": "Invalid Station",
            },
            {
                "LATITUDE": "91",
                "LONGITUDE": "0",
                "PLANT_CODE": "44",
                "NAME": "Out of range",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plants.csv"
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)

            collection = read_point_features(path, "power-plants")

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 1)
        feature = collection["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [-75.25, 40.5])
        self.assertEqual(feature["properties"]["summer_capacity"], 125.5)
        self.assertEqual(feature["properties"]["name"], "Example Station")

    def test_web_mercator_coordinates_convert_to_geojson_order(self):
        longitude, latitude = projected_to_lonlat(EARTH_RADIUS_METERS * math.pi, 0)
        self.assertAlmostEqual(longitude, 180)
        self.assertAlmostEqual(latitude, 0)

    def test_polyline_record_is_simplified_and_keeps_endpoints(self):
        points = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
        content = (
            struct.pack("<i4dii", 3, 0, 0, 20, 0, 1, len(points))
            + struct.pack("<i", 0)
            + struct.pack("<6d", *(coordinate for point in points for coordinate in point))
        )

        lines = parse_polyline_content(content, tolerance=1)

        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]), 2)
        self.assertAlmostEqual(lines[0][0][0], 0)
        self.assertAlmostEqual(lines[0][-1][0], math.degrees(20 / EARTH_RADIUS_METERS))

    def test_invalid_polyline_record_is_reported(self):
        with self.assertRaisesRegex(ValueError, "shorter than its fixed header"):
            parse_polyline_content(struct.pack("<i", 3), tolerance=1)


if __name__ == "__main__":
    unittest.main()
