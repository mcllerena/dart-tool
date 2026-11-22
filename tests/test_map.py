import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.map import (
    MapRepository,
    MapRequestHandler,
    offshore_cable_properties,
    read_operating_plant_features,
    read_plant_sheet_features,
    read_point_features,
)


class MapDataTests(unittest.TestCase):
    def test_offshore_cables_classify_current_and_voltage(self):
        dc = offshore_cable_properties(
            {"ELECTRIC_CURRENT_TYPE": "DC", "KILOVOLT_RATING": "320-525", "PROJECT_NAME": "<Null>"}
        )
        self.assertEqual(
            (dc["current"], dc["dc_capable"], dc["voltage"], dc["name"]),
            ("DC", True, 525.0, "Offshore wind export cable"),
        )
        hybrid = offshore_cable_properties(
            {
                "ELECTRIC_CURRENT_TYPE": "AC and/or DC",
                "KILOVOLT_RATING": "230-275 kV (HVAC) and/or 320-525 kV (HVDC)",
                "PROJECT_NAME": "Beacon Wind 1 ",
            }
        )
        self.assertEqual(
            (hybrid["current"], hybrid["dc_capable"], hybrid["voltage"], hybrid["name"]),
            ("AC / DC", True, 230.0, "Beacon Wind 1"),
        )
        ac = offshore_cable_properties({"ELECTRIC_CURRENT_TYPE": "AC", "KILOVOLT_RATING": "275"})
        self.assertEqual((ac["current"], ac["dc_capable"], ac["voltage"]), ("AC", False, 275.0))

    def test_offshore_cables_skip_missing_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tx_lines" / "offshore_wind_tx_cables.geojson"
            path.parent.mkdir()
            line = {"type": "LineString", "coordinates": [[-70, 41], [-71, 41.5]]}
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": line, "properties": {"ELECTRIC_CURRENT_TYPE": "DC"}},
                {"type": "Feature", "geometry": None, "properties": {"ELECTRIC_CURRENT_TYPE": "AC"}},
            ]}), encoding="utf-8")
            features = MapRepository(Path(tmp)).offshore_cables()["features"]
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["current"], "DC")

    def test_sunzia_asset_is_one_linestring(self):
        source_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "dart_tool"
            / "data"
            / "tx_lines"
            / "sunzia_transmission_project.geojson"
        )
        collection = json.loads(source_path.read_text(encoding="utf-8"))

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 1)
        self.assertEqual(collection["features"][0]["geometry"]["type"], "LineString")

    def test_sunzia_route_is_exposed_and_reloaded_when_source_changes(self):
        source_feature = {
            "type": "Feature",
            "id": "sunzia-route",
            "geometry": {
                "type": "LineString",
                "coordinates": [[0, 0], [1, 1]],
            },
            "properties": {
                "name": "SunZia Transmission Project",
                "o": "Pattern Energy",
                "power": "line",
                "v": 525000,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            tx_lines_dir = Path(directory) / "tx_lines"
            tx_lines_dir.mkdir()
            source_path = tx_lines_dir / "sunzia_transmission_project.geojson"

            def write_source():
                source_path.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [source_feature],
                    }
                ),
                encoding="utf-8",
            )
 
            write_source()

            repository = MapRepository(Path(directory))
            collection = repository.hvdc_lines()

            self.assertIs(repository.hvdc_lines(), collection)
            source_mtime = source_path.stat().st_mtime_ns
            source_feature["geometry"]["coordinates"] = [[0, 0], [2, 2]]
            write_source()
            os.utime(
                source_path,
                ns=(source_mtime, source_mtime + 1_000_000_000),
            )
            updated_collection = repository.hvdc_lines()

        self.assertEqual(len(collection["features"]), 1)
        feature = collection["features"][0]
        self.assertEqual(feature["id"], "sunzia-route")
        self.assertEqual(feature["geometry"]["type"], "LineString")
        self.assertEqual(feature["properties"]["ID"], "SunZia Transmission Project")
        self.assertEqual(feature["properties"]["TYPE"], "DC")
        self.assertEqual(feature["properties"]["VOLT_CLASS"], "DC")
        self.assertEqual(feature["properties"]["VOLTAGE"], 525)
        self.assertEqual(feature["properties"]["OWNER"], "Pattern Energy")
        self.assertIsNot(updated_collection, collection)
        self.assertEqual(
            updated_collection["features"][0]["geometry"]["coordinates"],
            [[0, 0], [2, 2]],
        )

    def test_sunzia_repository_rejects_multiline_geometry(self):
        source_feature = {
            "type": "Feature",
            "geometry": {
                "type": "MultiLineString",
                "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]],
            },
            "properties": {"v": 525000},
        }
        with tempfile.TemporaryDirectory() as directory:
            tx_lines_dir = Path(directory) / "tx_lines"
            tx_lines_dir.mkdir()
            source_path = tx_lines_dir / "sunzia_transmission_project.geojson"
            source_path.write_text(
                json.dumps({"type": "FeatureCollection", "features": [source_feature]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "single LineString"):
                MapRepository(Path(directory)).hvdc_lines()

    def test_dc_transmission_lines_loads_pre_extracted_records(self):
        features = [
            {
                "type": "Feature",
                "id": "dc-by-type",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {"TYPE": "DC; OVERHEAD", "VOLT_CLASS": "Unknown"},
            },
            {
                "type": "Feature",
                "id": "dc-by-class",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {"TYPE": "NOT AVAILABLE", "VOLT_CLASS": "DC"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            tx_lines_dir = Path(directory) / "tx_lines"
            tx_lines_dir.mkdir()
            source_path = tx_lines_dir / "tx_lines_dc.geojson"
            source_path.write_text(
                json.dumps({"type": "FeatureCollection", "features": features}),
                encoding="utf-8",
            )

            result = MapRepository(Path(directory)).dc_transmission_lines()

        self.assertEqual(
            [feature["id"] for feature in result["features"]],
            ["dc-by-type", "dc-by-class"],
        )

    def test_dc_transmission_lines_preserves_custom_name_and_operator(self):
        feature = {
            "type": "Feature",
            "id": 7276,
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {
                "ID": "108354",
                "NAME": "Square Butte HVDC 250kV transmission line",
                "OPERATOR": "ALLETE, Inc.",
                "TYPE": "DC; OVERHEAD",
                "VOLTAGE": 250,
                "VOLT_CLASS": "DC",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            tx_lines_dir = Path(directory) / "tx_lines"
            tx_lines_dir.mkdir()
            source_path = tx_lines_dir / "tx_lines_dc.geojson"
            source_path.write_text(
                json.dumps({"type": "FeatureCollection", "features": [feature]}),
                encoding="utf-8",
            )

            result = MapRepository(Path(directory)).dc_transmission_lines()

        properties = result["features"][0]["properties"]
        self.assertEqual(properties["NAME"], "Square Butte HVDC 250kV transmission line")
        self.assertEqual(properties["OPERATOR"], "ALLETE, Inc.")

    def test_dc_substations_includes_only_dc_line_endpoints(self):
        line = {
            "type": "Feature",
            "id": "dc-line",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {
                "TYPE": "DC; OVERHEAD",
                "VOLT_CLASS": "DC",
                "SUB_1": "NORTH STATION",
                "SUB_2": "SOUTH STATION",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            tx_lines_dir = data_dir / "tx_lines"
            tx_lines_dir.mkdir()
            (tx_lines_dir / "tx_lines_dc.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": [line]}),
                encoding="utf-8",
            )
            substation_dir = data_dir / "substations"
            substation_dir.mkdir()
            with (substation_dir / "Substations.csv").open(
                "w", encoding="utf-8", newline=""
            ) as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=[
                        "ID", "NAME", "LATITUDE", "LONGITUDE", "CITY", "STATE",
                        "STATUS", "TYPE", "MAX_VOLT", "MIN_VOLT",
                    ],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "ID": "1", "NAME": "NORTH STATION", "LATITUDE": 0,
                            "LONGITUDE": 0, "MAX_VOLT": 500,
                        },
                        {
                            "ID": "2", "NAME": "SOUTH STATION", "LATITUDE": 1,
                            "LONGITUDE": 1, "MAX_VOLT": 500,
                        },
                        {
                            "ID": "3", "NAME": "UNRELATED STATION", "LATITUDE": 0.5,
                            "LONGITUDE": 0.5, "MAX_VOLT": 230,
                        },
                        {
                            "ID": "4", "NAME": "NORTH STATION", "LATITUDE": 0,
                            "LONGITUDE": 0.01, "MAX_VOLT": 230,
                        },
                    ]
                )

            result = MapRepository(data_dir).dc_substations()

        self.assertEqual(
            [feature["properties"]["station_id"] for feature in result["features"]],
            ["1", "2"],
        )

    def test_power_plant_repository_loads_and_caches_status_geojson(self):
        feature = {
            "type": "Feature",
            "id": "sample",
            "geometry": {"type": "Point", "coordinates": [-75.0, 40.0]},
            "properties": {"sheet_status": "operating"},
        }
        with tempfile.TemporaryDirectory() as directory:
            plant_dir = Path(directory) / "power_plants"
            plant_dir.mkdir()
            for filename in ("us_operating_pp.geojson", "pr_operating_pp.geojson"):
                (plant_dir / filename).write_text(
                    json.dumps({"type": "FeatureCollection", "features": [feature]}),
                    encoding="utf-8",
                )
            planned_feature = {
                **feature,
                "properties": {"sheet_status": "planned"},
            }
            planned_collection = {
                "type": "FeatureCollection",
                "features": [planned_feature],
            }
            for filename in ("us_planned_pp.geojson", "pr_planned_pp.geojson"):
                (plant_dir / filename).write_text(
                    json.dumps(planned_collection), encoding="utf-8"
                )

            repository = MapRepository(Path(directory))
            operating = repository.plant_features("operating")
            planned = repository.plant_features("planned")

            self.assertIs(repository.plant_features("operating"), operating)

        self.assertEqual(len(operating["features"]), 2)
        self.assertEqual(len(planned["features"]), 2)
        self.assertTrue(
            all(
                item["properties"]["sheet_status"] == "planned"
                for item in planned["features"]
            )
        )

    def test_response_write_handles_client_disconnects(self):
        for error_type in (
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
        ):
            with self.subTest(error=error_type.__name__):
                handler = Mock()
                handler.wfile.write.side_effect = error_type()

                with self.assertLogs("dart_tool.map", level="DEBUG"):
                    MapRequestHandler._send_bytes(
                        handler, b"response", "application/geo+json"
                    )

                handler.wfile.write.assert_called_once_with(b"response")

    def test_substation_csv_becomes_geojson_and_skips_invalid_coordinates(self):
        columns = [
            "LATITUDE",
            "LONGITUDE",
            "ID",
            "NAME",
            "CITY",
            "STATE",
            "STATUS",
            "TYPE",
            "MAX_VOLT",
            "MIN_VOLT",
        ]
        rows = [
            {
                "LATITUDE": "40.5",
                "LONGITUDE": "-75.25",
                "ID": "42",
                "NAME": "Example Station",
                "CITY": "Sample City",
                "STATE": "PA",
                "STATUS": "OP",
                "TYPE": "SUBSTATION",
                "MAX_VOLT": "345",
                "MIN_VOLT": "115",
            },
            {
                "LATITUDE": "not available",
                "LONGITUDE": "-75",
                "ID": "43",
                "NAME": "Invalid Station",
            },
            {
                "LATITUDE": "91",
                "LONGITUDE": "0",
                "ID": "44",
                "NAME": "Out of range",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plants.csv"
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)

            collection = read_point_features(path, "substations")

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 1)
        feature = collection["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [-75.25, 40.5])
        self.assertEqual(feature["properties"]["station_id"], "42")
        self.assertEqual(feature["properties"]["max_voltage"], 345)
        self.assertEqual(feature["properties"]["name"], "Example Station")


    def test_operating_workbook_keeps_each_coordinate_valid_row_independent(self):
        main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        headers = [
            "Plant ID", "Plant Name", "Plant State", "County", "Generator ID",
            "Status", "Technology", "Energy Source Code", "Prime Mover Code",
            "Net Summer Capacity (MW)", "Nameplate Capacity (MW)",
            "Net Winter Capacity (MW)", "Latitude", "Longitude",
        ]
        rows = [
            [42, "Mixed Plant", "PA", "Sample County", "G1", "Operating",
             "Natural Gas", "NG", "CC", 40, 50, 45, 40.5, -75.25],
            [42, "Mixed Plant", "PA", "Sample County", "G2", "Operating",
             "Solar Photovoltaic", "SUN", "PV", 60, 70, 55, 40.6, -75.35],
            [43, "Second Plant", "NY", "Other County", "A", "Operating",
             "Hydroelectric", "WAT", "HY", 25, 30, 25, 42.0, -74.0],
            [None, "Plant Without ID", "PA", "Sample County", "U1", "Operating",
             "Solar Photovoltaic", "SUN", "PV", 5, 6, 5, 39.8, -75.1],
        ]
        strings = []
        string_indexes = {}

        def shared_string(value):
            if value not in string_indexes:
                string_indexes[value] = len(strings)
                strings.append(value)
            return string_indexes[value]

        def column_name(index):
            name = ""
            while index:
                index, remainder = divmod(index - 1, 26)
                name = chr(ord("A") + remainder) + name
            return name

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plants.xlsx"
            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                workbook = ET.Element(f"{{{main_ns}}}workbook")
                sheets = ET.SubElement(workbook, f"{{{main_ns}}}sheets")
                sheet_names = (
                    "Operating",
                    "Operating_PR",
                    "Planned",
                    "Planned_PR",
                    "Retired",
                    "Retired_PR",
                    "Canceled or Postponed",
                )
                for sheet_number, sheet_name in enumerate(sheet_names, start=1):
                    ET.SubElement(
                        sheets,
                        f"{{{main_ns}}}sheet",
                        {
                            "name": sheet_name,
                            "sheetId": str(sheet_number),
                            f"{{{rel_ns}}}id": f"rId{sheet_number}",
                        },
                    )
                archive.writestr("xl/workbook.xml", ET.tostring(workbook, xml_declaration=True))

                relationships = ET.Element(f"{{{package_rel_ns}}}Relationships")
                for sheet_number in range(1, len(sheet_names) + 1):
                    ET.SubElement(
                        relationships,
                        f"{{{package_rel_ns}}}Relationship",
                        {
                            "Id": f"rId{sheet_number}",
                            "Type": f"{rel_ns}/worksheet",
                            "Target": f"worksheets/sheet{sheet_number}.xml",
                        },
                    )
                archive.writestr(
                    "xl/_rels/workbook.xml.rels",
                    ET.tostring(relationships, xml_declaration=True),
                )

                all_rows = [(3, headers)] + [
                    (row_number, values)
                    for row_number, values in enumerate(rows, start=4)
                ]
                for sheet_number in range(1, len(sheet_names) + 1):
                    worksheet = ET.Element(f"{{{main_ns}}}worksheet")
                    sheet_data = ET.SubElement(worksheet, f"{{{main_ns}}}sheetData")
                    for row_number, values in all_rows:
                        row_element = ET.SubElement(
                            sheet_data, f"{{{main_ns}}}row", {"r": str(row_number)}
                        )
                        for column_index, value in enumerate(values, start=1):
                            if value is None:
                                continue
                            cell = ET.SubElement(
                                row_element,
                                f"{{{main_ns}}}c",
                                {"r": f"{column_name(column_index)}{row_number}"},
                            )
                            if isinstance(value, str):
                                cell.set("t", "s")
                                cell_value = shared_string(value)
                                ET.SubElement(cell, f"{{{main_ns}}}v").text = str(cell_value)
                            else:
                                cell.set("t", "n")
                                ET.SubElement(cell, f"{{{main_ns}}}v").text = str(value)
                    archive.writestr(
                        f"xl/worksheets/sheet{sheet_number}.xml",
                        ET.tostring(worksheet, xml_declaration=True),
                    )

                shared_root = ET.Element(
                    f"{{{main_ns}}}sst",
                    {"count": str(len(strings)), "uniqueCount": str(len(strings))},
                )
                for value in strings:
                    item = ET.SubElement(shared_root, f"{{{main_ns}}}si")
                    ET.SubElement(item, f"{{{main_ns}}}t").text = value
                archive.writestr(
                    "xl/sharedStrings.xml",
                    ET.tostring(shared_root, xml_declaration=True),
                )

            collection = read_operating_plant_features(path)
            sheet_features = {
                sheet_name: read_plant_sheet_features(path, sheet_name)
                for sheet_name in sheet_names
            }

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 4)
        first, second, _, missing_id = collection["features"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["properties"]["plant_id"], second["properties"]["plant_id"])
        self.assertEqual(first["properties"]["net_summer_capacity"], 40)
        self.assertEqual(second["properties"]["net_summer_capacity"], 60)
        self.assertNotEqual(
            first["geometry"]["coordinates"], second["geometry"]["coordinates"]
        )
        self.assertNotIn("units_number", first["properties"])
        self.assertEqual(
            first["properties"]["workbook_fields"]["Generator ID"], ["G1"]
        )
        self.assertEqual(first["geometry"]["coordinates"], [-75.25, 40.5])
        self.assertIsNone(missing_id["properties"]["plant_id"])
        expected_statuses = {
            "Operating": "operating",
            "Operating_PR": "operating",
            "Planned": "planned",
            "Planned_PR": "planned",
            "Retired": "retired",
            "Retired_PR": "retired",
            "Canceled or Postponed": "canceled",
        }
        for sheet_name, expected_status in expected_statuses.items():
            feature = sheet_features[sheet_name]["features"][0]
            self.assertEqual(feature["properties"]["sheet_status"], expected_status)
            self.assertTrue(feature["id"].startswith(f"{sheet_name.lower()}-"))


if __name__ == "__main__":
    unittest.main()
