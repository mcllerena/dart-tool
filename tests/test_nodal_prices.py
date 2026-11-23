import runpy
import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dart_tool.nodal_prices import (
    GRIDSTATUS_METHODS,
    ISO_TIMEZONES,
    NodalPriceError,
    _coordinate_nodes_for_prices,
    _gridstatus_client,
    _normalize_gridstatus_rows,
    _reduce_gridstatus_frame,
    fetch_nodal_price_geojson,
    make_nodal_price_geojson,
)


class FakeFrame:
    def __init__(self, rows):
        self.frame = pd.DataFrame(rows)

    def __getattr__(self, name):
        return getattr(self.frame, name)

    def __getitem__(self, key):
        return self.frame[key]

    def __setitem__(self, key, value):
        self.frame[key] = value

    def to_dict(self, orient):
        return self.frame.to_dict(orient=orient)


class NodalPriceTests(unittest.TestCase):
    def setUp(self):
        self.coordinate_nodes = [
            {
                "node_id": "PJM-HUB-1",
                "name": "Western Hub",
                "station_name": "Western Hub",
                "lat": 40.0,
                "lng": -79.0,
            }
        ]

    def test_gridstatus_uses_all_node_methods_for_each_iso(self):
        self.assertEqual(
            GRIDSTATUS_METHODS["ERCOT"],
            {"da": "get_lmp_by_bus_dam", "rt": "get_lmp_by_bus"},
        )
        self.assertEqual(
            GRIDSTATUS_METHODS["SPP"]["rt"],
            "get_lmp_real_time_5_min_by_bus",
        )
        self.assertEqual(
            GRIDSTATUS_METHODS["PJM"]["rt"],
            "get_lmp_real_time_5_min",
        )

    def test_ercot_bus_directory_enriches_prices_with_substation(self):
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        frame_rows = [{
            "Interval Start": datetime(2026, 11, 22, 12, 0, tzinfo=zone),
            "Location": "0001",
            "Location Type": "Electrical Bus",
            "LMP": 22.5,
            "Energy": 20.0,
            "Congestion": 2.0,
            "Loss": 0.5,
        }]
        mapping_rows = [{
            "Electrical Bus": "0001",
            "Node Name": "0001",
            "PSSE Bus Name": "L_BUBORA_1Y",
            "Substation": "BUBORA",
            "Settlement Load Zone": "LZ_SOUTH",
        }]

        rows = _normalize_gridstatus_rows(frame_rows, "ERCOT", mapping_rows)

        self.assertEqual(rows[0]["node_id"], "0001")
        self.assertEqual(rows[0]["node_name"], "L_BUBORA_1Y")
        self.assertEqual(rows[0]["station_name"], "BUBORA")
        self.assertEqual(rows[0]["zone"], "LZ_SOUTH")
        self.assertEqual(rows[0]["lmp"], 22.5)
        day_ahead_rows = _normalize_gridstatus_rows(
            [{
                "Interval Start": datetime(2026, 11, 22, 12, 0, tzinfo=zone),
                "Location": "0001",
                "LMP": 25.0,
            }],
            "ERCOT",
            mapping_rows,
        )
        self.assertEqual(day_ahead_rows[0]["station_name"], "BUBORA")

    def test_gridstatus_reduces_daily_intervals_before_normalizing(self):
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        frame = FakeFrame([
            {
                "Interval Start": datetime(2026, 11, 22, 12, 0, tzinfo=zone),
                "Location": "0001",
                "LMP": 10,
                "Energy": 8,
            },
            {
                "Interval Start": datetime(2026, 11, 22, 12, 5, tzinfo=zone),
                "Location": "0001",
                "LMP": 30,
                "Energy": 28,
            },
        ])

        reduced = _reduce_gridstatus_frame(
            frame, "ERCOT", date(2026, 11, 22), "average"
        )
        normalized = _normalize_gridstatus_rows(
            reduced,
            "ERCOT",
            [{
                "Electrical Bus": "0001",
                "PSSE Bus Name": "L_BUBORA_1Y",
                "Substation": "BUBORA",
                "Settlement Load Zone": "LZ_SOUTH",
            }],
        )

        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0]["LMP"], 20)
        self.assertEqual(reduced[0]["Energy"], 18)
        self.assertEqual(reduced[0]["__interval_count"], 2)
        self.assertEqual(normalized[0]["station_name"], "BUBORA")

    def test_coordinate_join_requires_one_exact_iso_local_station_match(self):
        rows = [
            {"node_id": "BUS-1", "node_name": "Node 1", "station_name": "BUB-ORA"},
            {"node_id": "BUS-2", "node_name": "Node 2", "station_name": "DUPLICATE"},
            {"node_id": "BUS-3", "node_name": "Node 3", "station_name": "NO MATCH"},
        ]
        coordinate_index = {
            "BUBORA": [{
                "name": "BUBORA",
                "state": "TX",
                "lat": 30.0,
                "lng": -97.0,
            }],
            "DUPLICATE": [
                {"name": "DUPLICATE", "state": "TX", "lat": 31.0, "lng": -98.0},
                {"name": "DUPLICATE", "state": "TX", "lat": 32.0, "lng": -99.0},
            ],
        }

        nodes = _coordinate_nodes_for_prices(rows, "ERCOT", coordinate_index)

        self.assertEqual([node["node_id"] for node in nodes], ["BUS-1"])
        self.assertEqual(nodes[0]["station_name"], "BUBORA")

    def test_unmatched_and_ambiguous_prices_remain_exportable(self):
        day = date(2026, 11, 22)
        rows = [
            {
                "node_id": "PJM-HUB-1",
                "node_name": "Western Hub",
                "ts": "2026-11-22T13:00:00Z",
                "lmp": 30,
                "energy": 20,
            },
            {
                "node_id": "UNMAPPED-1",
                "node_name": "Unmapped Node",
                "ts": "2026-11-22T13:00:00Z",
                "lmp": 50,
            },
        ]
        duplicate_coordinates = self.coordinate_nodes + [
            {**self.coordinate_nodes[0], "lat": 41.0}
        ]

        latest = make_nodal_price_geojson(
            rows, self.coordinate_nodes, "PJM", "rt", day
        )
        average = make_nodal_price_geojson(
            rows, self.coordinate_nodes, "PJM", "rt", day, aggregation="average"
        )
        ambiguous = make_nodal_price_geojson(
            rows[:1], duplicate_coordinates, "PJM", "rt", day
        )

        self.assertEqual(latest["features"][0]["properties"]["price"], 30.0)
        self.assertEqual(
            latest["features"][0]["geometry"]["coordinates"], [-79.0, 40.0]
        )
        self.assertEqual(average["features"][0]["properties"]["price"], 30.0)
        self.assertEqual(latest["metadata"]["unmatched_nodes"], 1)
        self.assertEqual(latest["prices"][1]["node_id"], "UNMAPPED-1")
        self.assertEqual(ambiguous["features"], [])
        self.assertIsNone(ambiguous["prices"][0]["latitude"])

    def test_fetch_uses_live_ercot_snapshot_and_bus_station_directory(self):
        day = datetime.now(ZoneInfo(ISO_TIMEZONES["ERCOT"])).date()
        interval = datetime.combine(day, time(12), ZoneInfo(ISO_TIMEZONES["ERCOT"]))

        class FakeErcot:
            calls = []

            def get_lmp_by_bus(self, query_date):
                self.calls.append(("prices", query_date))
                return FakeFrame([{
                    "Interval Start": interval,
                    "Location": "0001",
                    "Location Type": "Electrical Bus",
                    "LMP": 22.5,
                    "Energy": 20,
                    "Congestion": 2,
                    "Loss": 0.5,
                }])

            def get_settlement_points_electrical_bus_mapping(self, query_date):
                self.calls.append(("mapping", query_date))
                return FakeFrame([{
                    "Electrical Bus": "0001",
                    "PSSE Bus Name": "L_BUBORA_1Y",
                    "Substation": "BUBORA",
                    "Settlement Load Zone": "LZ_SOUTH",
                }])

        client = FakeErcot()
        with (
            patch("dart_tool.nodal_prices._gridstatus_client", return_value=client),
            patch(
                "dart_tool.nodal_prices._load_substation_coordinate_index",
                return_value={
                    "BUBORA": [{
                        "name": "BUBORA",
                        "state": "TX",
                        "lat": 30.0,
                        "lng": -97.0,
                    }]
                },
            ),
        ):
            result = fetch_nodal_price_geojson("ERCOT", day, "rt", "latest")

        self.assertEqual(client.calls, [("prices", "latest"), ("mapping", "latest")])
        self.assertEqual(result["metadata"]["source"], "GridStatus")
        self.assertEqual(result["metadata"]["source_nodes"], 1)
        self.assertEqual(result["metadata"]["matched_nodes"], 1)
        self.assertEqual(
            result["features"][0]["properties"]["mapped_station"], "BUBORA"
        )

    def test_pjm_missing_key_has_actionable_error(self):
        class MissingKey:
            def __init__(self):
                raise ValueError("api_key must be provided or set in PJM_API_KEY env var")

        with patch(
            "dart_tool.nodal_prices.importlib.import_module",
            return_value=SimpleNamespace(PJM=MissingKey),
        ):
            with self.assertRaisesRegex(NodalPriceError, "PJM_API_KEY"):
                _gridstatus_client("PJM")

    def test_map_repository_imports_nodal_module_when_run_as_script(self):
        package_dir = Path(__file__).resolve().parents[1] / "src" / "dart_tool"
        package_path = str(package_dir)
        path_was_present = package_path in sys.path
        if not path_was_present:
            sys.path.insert(0, package_path)
        try:
            import nodal_prices as direct_nodal_prices

            map_scope = runpy.run_path(
                str(package_dir / "map.py"),
                run_name="map_entry_import_test",
            )
            repository = map_scope["MapRepository"]()
            day = datetime.now(ZoneInfo(ISO_TIMEZONES["ERCOT"])).date()
            interval = datetime.combine(
                day, time(12), ZoneInfo(ISO_TIMEZONES["ERCOT"])
            )

            class FakeErcot:
                def get_lmp_by_bus(self, query_date):
                    return FakeFrame([{
                        "Interval Start": interval,
                        "Location": "0001",
                        "LMP": 22.5,
                    }])

                def get_settlement_points_electrical_bus_mapping(self, query_date):
                    return FakeFrame([{
                        "Electrical Bus": "0001",
                        "Substation": "BUBORA",
                    }])

            with (
                patch.object(
                    direct_nodal_prices, "_gridstatus_client", return_value=FakeErcot()
                ),
                patch.object(
                    direct_nodal_prices,
                    "_load_substation_coordinate_index",
                    return_value={
                        "BUBORA": [{
                            "name": "BUBORA",
                            "state": "TX",
                            "lat": 30.0,
                            "lng": -97.0,
                        }]
                    },
                ),
            ):
                result = repository.nodal_prices("ERCOT", day, "rt", "latest")
        finally:
            if not path_was_present:
                sys.path.remove(package_path)

        self.assertEqual(result["metadata"]["matched_nodes"], 1)


if __name__ == "__main__":
    unittest.main()
