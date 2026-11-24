import runpy
import sys
import unittest
from datetime import date, datetime, time, timedelta
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
    _caiso_substation_from_pnode,
    _coordinate_nodes_for_prices,
    _gridstatus_client,
    _isone_substation_from_node,
    _miso_coordinate_for_node,
    _normalize_gridstatus_rows,
    _nyiso_coordinate_for_node,
    _reduce_gridstatus_frame,
    _spp_substation_from_pnode,
    fetch_nodal_price_geojson,
    fetch_nodal_price_product_geojson,
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

    def test_normalization_preserves_miso_location_type(self):
        rows = _normalize_gridstatus_rows(
            [{
                "Interval Start": datetime(
                    2026, 11, 22, 12, 0, tzinfo=ZoneInfo(ISO_TIMEZONES["MISO"])
                ),
                "Location": "ALTE.PADDOCK",
                "Location Type": "Gennode",
                "LMP": 22.0,
            }],
            "MISO",
        )

        self.assertEqual(rows[0]["node_type"], "Gennode")

    def test_miso_generator_node_prefers_a_plant_location_to_a_name_collision(self):
        row = {
            "node_id": "CIN.GIBSON.1",
            "node_name": "CIN.GIBSON.1",
            "node_type": "Gennode",
        }
        plant = {
            "name": "Gibson",
            "state": "IN",
            "lat": 38.37,
            "lng": -87.77,
        }
        substation = {
            "name": "GIBSON",
            "state": "LA",
            "lat": 29.8,
            "lng": -90.8,
        }

        with patch("dart_tool.nodal_prices._is_in_miso_footprint", return_value=True):
            point = _miso_coordinate_for_node(
                row,
                {"GIBSON": [("substation_name", substation)]},
                {"GIBSON": [plant]},
                {},
            )

        self.assertIsNotNone(point)
        self.assertEqual((point["lat"], point["lng"]), (38.37, -87.77))
        self.assertEqual(point["mapping_method"], "approximate-miso-plant")

    def test_miso_non_generator_nodes_are_not_assigned_to_substations(self):
        row = {
            "node_id": "ALTE.WAUTOMA",
            "node_name": "ALTE.WAUTOMA",
            "node_type": "Loadzone",
        }
        substation = {
            "name": "WAUTOMA",
            "state": "WI",
            "lat": 44.06,
            "lng": -89.29,
        }

        point = _miso_coordinate_for_node(
            row, {"WAUTOMA": [("substation_name", substation)]}, {}, {}
        )

        self.assertIsNone(point)

    def test_miso_candidates_outside_footprint_are_rejected(self):
        row = {
            "node_id": "CIN.CAYUGA.1",
            "node_name": "CIN.CAYUGA.1",
            "node_type": "Gennode",
        }
        plant = {
            "name": "Cayuga",
            "state": "IN",
            "lat": 39.9,
            "lng": -87.4,
        }

        with patch("dart_tool.nodal_prices._is_in_miso_footprint", return_value=False):
            point = _miso_coordinate_for_node(row, {}, {"CAYUGA": [plant]}, {})

        self.assertIsNone(point)

    def test_miso_ambiguous_name_candidates_are_not_assigned(self):
        row = {
            "node_id": "ALTE.ALBANY",
            "node_name": "ALTE.ALBANY",
            "node_type": "Gennode",
        }
        plants = [
            {"name": "Albany", "state": "MO", "lat": 40.2, "lng": -94.3},
            {"name": "Albany", "state": "WI", "lat": 42.6, "lng": -89.4},
        ]

        with patch("dart_tool.nodal_prices._is_in_miso_footprint", return_value=True):
            point = _miso_coordinate_for_node(row, {}, {"ALBANY": plants}, {})

        self.assertIsNone(point)

    def test_miso_generator_node_allows_close_station_name_match(self):
        row = {
            "node_id": "ALTE.SPNGCREEK",
            "node_name": "ALTE.SPNGCREEK",
            "node_type": "Gennode",
        }
        substation = {
            "name": "SPRING CREEK",
            "state": "MN",
            "lat": 46.0,
            "lng": -94.0,
        }

        with patch("dart_tool.nodal_prices._is_in_miso_footprint", return_value=True):
            point = _miso_coordinate_for_node(
                row,
                {"SPRINGCREEK": [("substation_name", substation)]},
                {},
                {"SP": ["SPRINGCREEK"]},
            )

        self.assertIsNotNone(point)
        self.assertEqual(point["station_name"], "SPRING CREEK")
        self.assertEqual(
            point["mapping_method"], "approximate-miso-fuzzy-substation_name"
        )
        self.assertEqual(
            GRIDSTATUS_METHODS["PJM"]["rt"],
            "get_lmp_real_time_5_min",
        )

    def test_nyiso_generator_node_maps_to_plant_from_unit_suffix(self):
        row = {
            "node_id": "BETHLEHEM___GS1",
            "node_name": "BETHLEHEM___GS1",
            "node_type": "Generator",
        }
        plant = {
            "name": "Bethlehem",
            "state": "NY",
            "lat": 42.58,
            "lng": -73.79,
        }
        substation = {
            "name": "BETHLEHEM",
            "state": "NY",
            "lat": 42.6,
            "lng": -73.8,
        }

        with patch("dart_tool.nodal_prices._is_in_nyiso_footprint", return_value=True):
            point = _nyiso_coordinate_for_node(
                row,
                {"BETHLEHEM": [substation]},
                {"BETHLEHEM": [plant]},
                {},
            )

        self.assertIsNotNone(point)
        self.assertEqual((point["lat"], point["lng"]), (42.58, -73.79))
        self.assertEqual(point["mapping_method"], "approximate-nyiso-plant")

    def test_nyiso_generator_matching_is_used_for_price_coordinates(self):
        row = {
            "node_id": "BETHLEHEM___GS1",
            "node_name": "BETHLEHEM___GS1",
            "station_name": "BETHLEHEM___GS1",
            "node_type": "Generator",
        }
        plant = {
            "name": "Bethlehem",
            "state": "NY",
            "lat": 42.58,
            "lng": -73.79,
        }

        with (
            patch(
                "dart_tool.nodal_prices._load_nyiso_substation_alias_index",
                return_value={},
            ),
            patch(
                "dart_tool.nodal_prices._load_nyiso_plant_alias_index",
                return_value={"BETHLEHEM": [plant]},
            ),
            patch("dart_tool.nodal_prices._is_in_nyiso_footprint", return_value=True),
        ):
            points = _coordinate_nodes_for_prices([row], "NYISO", {})

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["mapping_method"], "approximate-nyiso-plant")

    def test_nyiso_generator_node_supports_close_plant_name(self):
        row = {
            "node_id": "CHATEAUG_WT_PWR",
            "node_name": "CHATEAUG_WT_PWR",
            "node_type": "Generator",
        }
        plant = {
            "name": "Chateaugay",
            "state": "NY",
            "lat": 43.09,
            "lng": -74.77,
        }

        with patch("dart_tool.nodal_prices._is_in_nyiso_footprint", return_value=True):
            point = _nyiso_coordinate_for_node(
                row,
                {},
                {"CHATEAUGAY": [plant]},
                {"CH": ["CHATEAUGAY"]},
            )

        self.assertIsNotNone(point)
        self.assertEqual(point["mapping_method"], "approximate-nyiso-fuzzy-plant")

    def test_nyiso_non_generator_and_ambiguous_nodes_are_not_mapped(self):
        zone = {
            "node_id": "ALBANY",
            "node_name": "ALBANY",
            "node_type": "Zone",
        }
        generator = {**zone, "node_type": "Generator"}
        plants = [
            {"name": "Albany", "state": "NY", "lat": 42.6, "lng": -73.8},
            {"name": "Albany", "state": "NY", "lat": 42.7, "lng": -73.7},
        ]

        with patch("dart_tool.nodal_prices._is_in_nyiso_footprint", return_value=True):
            self.assertIsNone(_nyiso_coordinate_for_node(zone, {}, {}, {}))
            self.assertIsNone(
                _nyiso_coordinate_for_node(generator, {}, {"ALBANY": plants}, {})
            )

    def test_nyiso_candidate_outside_footprint_is_rejected(self):
        row = {
            "node_id": "CAYUGA___1",
            "node_name": "CAYUGA___1",
            "node_type": "Generator",
        }
        plant = {
            "name": "Cayuga",
            "state": "NY",
            "lat": 42.9,
            "lng": -76.7,
        }

        with patch("dart_tool.nodal_prices._is_in_nyiso_footprint", return_value=False):
            point = _nyiso_coordinate_for_node(row, {}, {"CAYUGA": [plant]}, {})

        self.assertIsNone(point)

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

    def test_latest_reduction_uses_selected_iso_local_time(self):
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        frame = FakeFrame([
            {
                "Interval Start": datetime(2026, 11, 22, 12, 0, tzinfo=zone),
                "Location": "0001",
                "LMP": 10,
            },
            {
                "Interval Start": datetime(2026, 11, 22, 12, 5, tzinfo=zone),
                "Location": "0001",
                "LMP": 30,
            },
        ])

        reduced = _reduce_gridstatus_frame(
            frame, "ERCOT", date(2026, 11, 22), "latest", time(12, 2)
        )

        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0]["LMP"], 10)
        self.assertEqual(reduced[0]["__timestamp"].hour, 12)
        self.assertEqual(reduced[0]["__timestamp"].minute, 0)

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

    def test_caiso_pnode_bus_suffix_matches_unique_station_name(self):
        zone = ZoneInfo(ISO_TIMEZONES["CAISO"])
        rows = _normalize_gridstatus_rows(
            [{
                "Interval Start": datetime(2026, 11, 22, 12, 0, tzinfo=zone),
                "Location": "LEGRAND_1_N018",
                "Location Type": "Node",
                "LMP": 34.0,
            }],
            "CAISO",
        )
        coordinate_index = {
            "LEGRAND": [{
                "name": "LE GRAND",
                "state": "CA",
                "lat": 37.18,
                "lng": -120.27,
            }]
        }

        nodes = _coordinate_nodes_for_prices(rows, "CAISO", coordinate_index)

        self.assertEqual(
            _caiso_substation_from_pnode("LEGRAND_1_N018"), "LEGRAND"
        )
        self.assertEqual(rows[0]["station_name"], "LEGRAND")
        self.assertEqual([node["node_id"] for node in nodes], ["LEGRAND_1_N018"])
        self.assertEqual(nodes[0]["station_name"], "LE GRAND")

    def test_caiso_station_can_match_inside_northerngrid_footprint(self):
        rows = [{
            "node_id": "NORTHGRID_1_N001",
            "node_name": "NORTHGRID_1_N001",
            "station_name": "NORTHGRID",
        }]
        coordinate_index = {
            "NORTHGRID": [{
                "name": "NORTHGRID",
                "state": "OR",
                "lat": 45.5231,
                "lng": -122.6765,
            }]
        }

        nodes = _coordinate_nodes_for_prices(rows, "CAISO", coordinate_index)

        self.assertEqual([node["node_id"] for node in nodes], ["NORTHGRID_1_N001"])

    def test_caiso_region_match_is_preferred_over_duplicate_northerngrid_name(self):
        rows = [{
            "node_id": "SHASTA_1_N001",
            "node_name": "SHASTA_1_N001",
            "station_name": "SHASTA",
        }]
        coordinate_index = {
            "SHASTA": [
                {
                    "name": "SHASTA",
                    "state": "CA",
                    "lat": 40.5865,
                    "lng": -122.3917,
                },
                {
                    "name": "SHASTA",
                    "state": "OR",
                    "lat": 42.3265,
                    "lng": -122.8756,
                },
            ]
        }

        nodes = _coordinate_nodes_for_prices(rows, "CAISO", coordinate_index)

        self.assertEqual([node["node_id"] for node in nodes], ["SHASTA_1_N001"])
        self.assertEqual(nodes[0]["lat"], 40.5865)

    def test_caiso_station_outside_caiso_and_northerngrid_is_not_matched(self):
        rows = [{
            "node_id": "OUTSIDE_1_N001",
            "node_name": "OUTSIDE_1_N001",
            "station_name": "OUTSIDE",
        }]
        coordinate_index = {
            "OUTSIDE": [{
                "name": "OUTSIDE",
                "state": "AZ",
                "lat": 33.4484,
                "lng": -112.074,
            }]
        }

        nodes = _coordinate_nodes_for_prices(rows, "CAISO", coordinate_index)

        self.assertEqual(nodes, [])

    def test_caiso_generator_pnode_does_not_guess_station(self):
        self.assertEqual(
            _caiso_substation_from_pnode("LSTCR_PH_1_G02GNODE"), ""
        )

    def test_isone_network_node_extracts_station_for_exact_coordinate_match(self):
        rows = _normalize_gridstatus_rows(
            [{
                "Interval Start": datetime(
                    2026, 11, 22, 12, 0, tzinfo=ZoneInfo(ISO_TIMEZONES["ISONE"])
                ),
                "Location Id": "TOPSHAM-ID",
                "Location": "UN.TOPSHAM 34.5BRUN",
                "Location Type": "NETWORK NODE",
                "LMP": 21.0,
            }],
            "ISONE",
        )
        coordinate_index = {
            "TOPSHAM": [{
                "name": "TOPSHAM",
                "state": "ME",
                "lat": 43.96,
                "lng": -69.93,
            }]
        }

        nodes = _coordinate_nodes_for_prices(rows, "ISONE", coordinate_index)

        self.assertEqual(
            _isone_substation_from_node("UN.TOPSHAM 34.5BRUN"), "TOPSHAM"
        )
        self.assertEqual(rows[0]["station_name"], "TOPSHAM")
        self.assertEqual([node["node_id"] for node in nodes], ["TOPSHAM-ID"])

    def test_spp_bus_extracts_station_and_rejects_unstructured_id(self):
        rows = _normalize_gridstatus_rows(
            [{
                "Interval Start": datetime(
                    2026, 11, 22, 12, 0, tzinfo=ZoneInfo(ISO_TIMEZONES["SPP"])
                ),
                "Location": "CSWS36_LEWISLD1",
                "Location Type": "Bus",
                "LMP": 19.0,
            }],
            "SPP",
        )
        coordinate_index = {
            "LEWIS": [{
                "name": "LEWIS",
                "state": "OK",
                "lat": 35.5,
                "lng": -97.5,
            }]
        }

        nodes = _coordinate_nodes_for_prices(rows, "SPP", coordinate_index)

        self.assertEqual(
            _spp_substation_from_pnode("CSWS36_LEWISLD1"), "LEWIS"
        )
        self.assertEqual(
            _spp_substation_from_pnode("UNRELATED-PNODE"), ""
        )
        self.assertEqual(rows[0]["station_name"], "LEWIS")
        self.assertEqual([node["node_id"] for node in nodes], ["CSWS36_LEWISLD1"])

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
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        now = datetime.now(zone)
        current_interval = now.replace(
            minute=(now.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        day = current_interval.date()
        selected_time = current_interval.time()

        class FakeErcot:
            calls = []  # noqa: RUF012

            def get_lmp_by_bus(self, query_start, end):
                self.calls.append(("prices", query_start, end))
                return FakeFrame([{
                    "Interval Start": current_interval,
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
            result = fetch_nodal_price_geojson(
                "ERCOT", day, "rt", "latest", selected_time
            )

        self.assertEqual(
            client.calls,
            [
                (
                    "prices",
                    current_interval.isoformat(),
                    (current_interval + timedelta(minutes=5)).isoformat(),
                ),
                ("mapping", "latest"),
            ],
        )
        self.assertEqual(result["metadata"]["source"], "GridStatus")
        self.assertEqual(result["metadata"]["source_nodes"], 1)
        self.assertEqual(result["metadata"]["matched_nodes"], 1)
        self.assertEqual(
            result["features"][0]["properties"]["mapped_station"], "BUBORA"
        )

    def test_live_caiso_latest_queries_only_selected_five_minute_interval(self):
        zone = ZoneInfo(ISO_TIMEZONES["CAISO"])
        selected_time = datetime.now(zone).time().replace(second=0, microsecond=0)
        query_start = datetime.combine(
            datetime.now(zone).date(),
            selected_time.replace(minute=(selected_time.minute // 5) * 5),
            zone,
        )
        day = query_start.date()

        class FakeCaiso:
            call = None

            def get_lmp_real_time_5_min(self, query_start, end, sleep):
                self.call = (query_start, end, sleep)
                return FakeFrame([{
                    "Interval Start": interval,
                    "Location": "LEGRAND_1_N018",
                    "LMP": 34.0,
                }])

        interval = query_start
        client = FakeCaiso()
        with (
            patch("dart_tool.nodal_prices._gridstatus_client", return_value=client),
            patch(
                "dart_tool.nodal_prices._load_substation_coordinate_index",
                return_value={},
            ),
        ):
            result = fetch_nodal_price_geojson(
                "CAISO", day, "rt", "latest", selected_time
            )

        query_start, query_end_arg, sleep = client.call
        self.assertEqual(datetime.fromisoformat(query_start), interval)
        self.assertEqual(
            datetime.fromisoformat(query_end_arg), interval + timedelta(minutes=5)
        )
        self.assertEqual(sleep, 0)
        self.assertEqual(result["metadata"]["source_nodes"], 1)

    def test_ercot_latest_queries_only_selected_five_minute_interval(self):
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        day = date(2026, 11, 22)
        selected_time = time(12, 22)
        interval = datetime.combine(day, time(12, 20), zone)

        class FakeErcot:
            calls = []  # noqa: RUF012

            def get_lmp_by_bus(self, query_start, end):
                self.calls.append(("prices", query_start, end))
                return FakeFrame([{
                    "Interval Start": interval,
                    "Location": "0001",
                    "Location Type": "Electrical Bus",
                    "LMP": 22.5,
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
                return_value={},
            ),
        ):
            result = fetch_nodal_price_geojson(
                "ERCOT", day, "rt", "latest", selected_time
            )

        self.assertEqual(
            client.calls,
            [
                (
                    "prices",
                    interval.isoformat(),
                    (interval + timedelta(minutes=5)).isoformat(),
                ),
                ("mapping", "latest"),
            ],
        )
        self.assertEqual(result["metadata"]["source_nodes"], 1)

    def test_day_ahead_latest_queries_only_selected_hour(self):
        zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
        day = date(2026, 11, 22)
        selected_time = time(12, 47)
        interval = datetime.combine(day, time(12), zone)

        class FakeErcot:
            calls = []  # noqa: RUF012

            def get_lmp_by_bus_dam(self, query_start, end):
                self.calls.append(("prices", query_start, end))
                return FakeFrame([{
                    "Interval Start": interval,
                    "Location": "0001",
                    "LMP": 22.5,
                }])

            def get_settlement_points_electrical_bus_mapping(self, query_date):
                self.calls.append(("mapping", query_date))
                return FakeFrame([])

        client = FakeErcot()
        with (
            patch("dart_tool.nodal_prices._gridstatus_client", return_value=client),
            patch(
                "dart_tool.nodal_prices._load_substation_coordinate_index",
                return_value={},
            ),
        ):
            result = fetch_nodal_price_geojson(
                "ERCOT", day, "da", "latest", selected_time
            )

        self.assertEqual(
            client.calls,
            [
                (
                    "prices",
                    datetime.combine(day, time(12), zone).isoformat(),
                    datetime.combine(day, time(13), zone).isoformat(),
                ),
                ("mapping", "latest"),
            ],
        )
        self.assertEqual(result["prices"][0]["price"], 22.5)

    def test_price_product_selects_component_for_display(self):
        source = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-97.0, 30.0]},
                "properties": {
                    "node_id": "BUS-1",
                    "price": 25.0,
                    "lmp": 25.0,
                    "energy": 20.0,
                    "congestion": 3.0,
                    "loss": 2.0,
                },
            }],
            "prices": [{
                "node_id": "BUS-1",
                "price": 25.0,
                "lmp": 25.0,
                "energy": 20.0,
                "congestion": 3.0,
                "loss": 2.0,
            }],
            "metadata": {
                "iso": "ERCOT",
                "market": "rt",
                "matched_nodes": 1,
                "unmatched_nodes": 0,
            },
        }
        with patch(
            "dart_tool.nodal_prices.fetch_nodal_price_geojson",
            return_value=source,
        ) as fetch:
            result = fetch_nodal_price_product_geojson(
                "ERCOT", date(2026, 11, 22), "rt_energy", "latest", time(12, 20)
            )

        fetch.assert_called_once_with(
            "ERCOT", date(2026, 11, 22), "rt", "latest", time(12, 20)
        )
        self.assertEqual(result["prices"][0]["price"], 20.0)
        self.assertEqual(result["features"][0]["properties"]["price"], 20.0)
        self.assertEqual(result["metadata"]["product"], "rt_energy")

    def test_price_product_reuses_cached_base_without_mutating_it(self):
        source = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-97.0, 30.0]},
                "properties": {"node_id": "BUS-1", "price": 25.0},
            }],
            "prices": [{
                "node_id": "BUS-1",
                "price": 25.0,
                "lmp": 25.0,
                "energy": 20.0,
                "congestion": 3.0,
                "loss": 2.0,
            }],
            "metadata": {
                "iso": "ERCOT",
                "market": "rt",
                "matched_nodes": 1,
                "unmatched_nodes": 0,
            },
        }
        with patch("dart_tool.nodal_prices.fetch_nodal_price_geojson") as fetch:
            result = fetch_nodal_price_product_geojson(
                "ERCOT",
                date(2026, 11, 22),
                "rt_energy",
                "latest",
                time(12, 20),
                base_results={"rt": source},
            )

        fetch.assert_not_called()
        self.assertEqual(result["prices"][0]["price"], 20.0)
        self.assertEqual(source["prices"][0]["price"], 25.0)
        self.assertNotIn("price_product", source["prices"][0])

    def test_dart_product_is_day_ahead_minus_real_time_by_node(self):
        def result(market, price, energy):
            return {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-97.0, 30.0]},
                    "properties": {
                        "node_id": "BUS-1",
                        "price": price,
                        "lmp": price,
                        "energy": energy,
                        "congestion": 4.0 if market == "da" else 2.0,
                        "loss": 3.0 if market == "da" else 1.0,
                    },
                }],
                "prices": [{
                    "node_id": "BUS-1",
                    "price": price,
                    "lmp": price,
                    "energy": energy,
                    "congestion": 4.0 if market == "da" else 2.0,
                    "loss": 3.0 if market == "da" else 1.0,
                    "timestamp": f"{market}-utc",
                    "local_timestamp": f"{market}-local",
                }],
                "metadata": {
                    "iso": "ERCOT",
                    "market": market,
                    "matched_nodes": 1,
                    "unmatched_nodes": 0,
                },
            }

        with patch(
            "dart_tool.nodal_prices.fetch_nodal_price_geojson",
            side_effect=[result("da", 30.0, 15.0), result("rt", 20.0, 10.0)],
        ):
            dart = fetch_nodal_price_product_geojson(
                "ERCOT", date(2026, 11, 22), "dart_energy", "latest", time(12, 20)
            )

        self.assertEqual(dart["prices"][0]["price"], 5.0)
        self.assertEqual(dart["prices"][0]["day_ahead_value"], 15.0)
        self.assertEqual(dart["prices"][0]["real_time_value"], 10.0)
        self.assertEqual(dart["prices"][0]["lmp"], 10.0)
        self.assertEqual(dart["features"][0]["properties"]["price"], 5.0)
        self.assertEqual(dart["metadata"]["derived_from"], ["day-ahead", "real-time"])

    def test_pjm_missing_key_has_actionable_error(self):
        class MissingKey:
            def __init__(self):
                raise ValueError("api_key must be provided or set in PJM_API_KEY env var")

        with patch(
            "dart_tool.nodal_prices.importlib.import_module",
            return_value=SimpleNamespace(PJM=MissingKey),
        ), self.assertRaisesRegex(NodalPriceError, "PJM_API_KEY"):
            _gridstatus_client("PJM")

    def test_map_repository_imports_nodal_module_when_run_as_script(self):
        package_dir = Path(__file__).resolve().parents[1] / "src" / "dart_tool"
        package_path = str(package_dir)
        path_was_present = package_path in sys.path
        if not path_was_present:
            sys.path.insert(0, package_path)
        try:
            import nodal_prices as direct_nodal_prices  # type: ignore

            map_scope = runpy.run_path(
                str(package_dir / "map.py"),
                run_name="map_entry_import_test",
            )
            repository = map_scope["MapRepository"]()
            zone = ZoneInfo(ISO_TIMEZONES["ERCOT"])
            now = datetime.now(zone)
            interval = now.replace(
                minute=(now.minute // 5) * 5,
                second=0,
                microsecond=0,
            )
            day = interval.date()

            class FakeErcot:
                calls = []  # noqa: RUF012

                def get_lmp_by_bus(self, query_start, end):
                    self.calls.append(("prices", query_start, end))
                    return FakeFrame([{
                        "Interval Start": interval,
                        "Location": "0001",
                        "LMP": 22.5,
                        "Energy": 20.0,
                        "Congestion": 2.0,
                        "Loss": 0.5,
                    }])

                def get_settlement_points_electrical_bus_mapping(self, query_date):
                    self.calls.append(("mapping", query_date))
                    return FakeFrame([{
                        "Electrical Bus": "0001",
                        "Substation": "BUBORA",
                    }])

            client = FakeErcot()
            with (
                patch.object(
                    direct_nodal_prices, "_gridstatus_client", return_value=client
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
                result = repository.nodal_prices(
                    "ERCOT", day, "rt", "latest", interval.time()
                )
                energy = repository.nodal_prices(
                    "ERCOT", day, "rt_energy", "latest", interval.time()
                )
        finally:
            if not path_was_present:
                sys.path.remove(package_path)

        self.assertEqual(result["metadata"]["matched_nodes"], 1)
        self.assertEqual(energy["prices"][0]["price"], 20.0)
        self.assertEqual(len([call for call in client.calls if call[0] == "prices"]), 1)


if __name__ == "__main__":
    unittest.main()
