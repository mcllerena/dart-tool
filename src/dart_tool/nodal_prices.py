"""GridStatus nodal-price retrieval and conservative coordinate matching."""

from __future__ import annotations

import csv
import importlib
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from numbers import Real
from pathlib import Path
from typing import Any
from urllib.error import URLError
from zoneinfo import ZoneInfo

from requests.exceptions import RequestException

GRIDSTATUS_CLASSES = {
    "CAISO": "CAISO",
    "ERCOT": "Ercot",
    "MISO": "MISO",
    "NYISO": "NYISO",
    "PJM": "PJM",
    "ISONE": "ISONE",
    "SPP": "SPP",
}

GRIDSTATUS_METHODS = {
    "CAISO": {
        "da": "get_lmp_day_ahead_hourly",
        "rt": "get_lmp_real_time_5_min",
    },
    "ERCOT": {
        "da": "get_lmp_by_bus_dam",
        "rt": "get_lmp_by_bus",
    },
    "MISO": {
        "da": "get_lmp_day_ahead_hourly",
        "rt": "get_lmp_real_time_5_min",
    },
    "NYISO": {
        "da": "get_lmp_day_ahead_hourly",
        "rt": "get_lmp_real_time_5_min",
    },
    "PJM": {
        "da": "get_lmp_day_ahead_hourly",
        "rt": "get_lmp_real_time_5_min",
    },
    "ISONE": {
        "da": "get_lmp_day_ahead_hourly",
        "rt": "get_lmp_real_time_5_min",
    },
    "SPP": {
        "da": "get_lmp_day_ahead_hourly_by_bus",
        "rt": "get_lmp_real_time_5_min_by_bus",
    },
}

ISO_TIMEZONES = {
    "CAISO": "America/Los_Angeles",
    "ERCOT": "America/Chicago",
    "MISO": "America/Chicago",
    "NYISO": "America/New_York",
    "PJM": "America/New_York",
    "ISONE": "America/New_York",
    "SPP": "America/Chicago",
}

ISO_STATES = {
    "CAISO": {"CA", "NV"},
    "ERCOT": {"TX"},
    "MISO": {
        "AR", "IA", "IL", "IN", "KY", "LA", "MI", "MN", "MO", "MS",
        "MT", "ND", "SD", "WI",
    },
    "NYISO": {"NY"},
    "PJM": {
        "DE", "IL", "IN", "KY", "MD", "MI", "NC", "NJ", "OH", "PA",
        "TN", "VA", "WV",
    },
    "ISONE": {"CT", "ME", "MA", "NH", "RI", "VT"},
    "SPP": {
        "AR", "IA", "KS", "LA", "MN", "MO", "MT", "NE", "NM", "ND",
        "OK", "SD", "TX",
    },
}

SUBSTATIONS_FILE = (
    Path(__file__).resolve().parent / "data" / "substations" / "Substations.csv"
)


class NodalPriceError(RuntimeError):
    """A price request could not be completed or normalized."""


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Real):
        numeric = float(value)
        if not math.isfinite(numeric):
            return ""
        if numeric.is_integer():
            return str(int(numeric))
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "<na>", "nat"} else text


def _first_text(row: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = _text(row.get(field))
        if value:
            return value
    return ""


def _timestamp_utc(value: Any, default_timezone: str | None = None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo(default_timezone) if default_timezone else timezone.utc
        )
    return parsed.astimezone(timezone.utc)


def _normalize_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    ascii_name = normalized.encode("ascii", errors="ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]", "", ascii_name.upper())


@lru_cache(maxsize=4)
def _load_substation_coordinate_index(path: str) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, dict[tuple[float, float], dict[str, Any]]] = defaultdict(dict)
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            name = _text(row.get("NAME"))
            state = _text(row.get("STATE")).upper()
            latitude = _number(row.get("LATITUDE"))
            longitude = _number(row.get("LONGITUDE"))
            if (
                not name
                or not state
                or _text(row.get("COUNTRY")).upper() not in {"USA", "US"}
                or latitude is None
                or longitude is None
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                continue
            key = _normalize_name(name)
            if not key:
                continue
            index[key].setdefault(
                (latitude, longitude),
                {
                    "name": name,
                    "state": state,
                    "lat": latitude,
                    "lng": longitude,
                },
            )
    return {key: list(points.values()) for key, points in index.items()}


def _gridstatus_client(iso: str) -> Any:
    try:
        gridstatus = importlib.import_module("gridstatus")
    except ImportError as error:
        raise NodalPriceError(
            "GridStatus is not installed. Install the project requirements first."
        ) from error

    class_name = GRIDSTATUS_CLASSES[iso]
    client_class = getattr(gridstatus, class_name, None)
    if client_class is None:
        raise NodalPriceError(
            f"The installed GridStatus version does not provide {class_name}."
        )
    try:
        return client_class()
    except ValueError as error:
        if iso == "PJM" and "pjm_api_key" in str(error).lower():
            raise NodalPriceError(
                "PJM node prices require a PJM Data Miner API key. "
                "Set PJM_API_KEY in the map server's environment."
            ) from error
        raise NodalPriceError(
            f"GridStatus could not initialize {iso}: {error}"
        ) from error


def _ercot_bus_directory(
    mapping_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    directory: dict[str, dict[str, Any]] = {}
    for row in mapping_rows:
        bus_id = _first_text(row, "Electrical Bus")
        if not bus_id:
            continue
        details = {
            "node_name": _first_text(row, "PSSE Bus Name", "Node Name"),
            "station_name": _first_text(row, "Substation"),
            "zone": _first_text(row, "Settlement Load Zone"),
        }
        directory[bus_id] = details
    return directory


def _normalize_gridstatus_rows(
    frame_rows: list[dict[str, Any]],
    iso: str,
    mapping_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    bus_directory = _ercot_bus_directory(mapping_rows or []) if iso == "ERCOT" else {}
    normalized: list[dict[str, Any]] = []
    for row in frame_rows:
        location = _first_text(
            row, "__node_id", "Location", "Electrical Bus", "Pnode ID"
        )
        mapping = bus_directory.get(location, {})
        node_id = _first_text(
            row,
            "__node_id",
            "Location Id",
            "Location ID",
            "Pnode ID",
            "Electrical Bus",
            "Location",
        )
        if not node_id:
            continue
        timestamp = _timestamp_utc(
            row.get("__timestamp")
            or row.get("Interval Start")
            or row.get("Time")
            or row.get("SCED Timestamp")
            or row.get("Timestamp"),
            ISO_TIMEZONES[iso],
        )
        normalized.append(
            {
                "node_id": node_id,
                "node_name": (
                    mapping.get("node_name")
                    or _first_text(
                        row,
                        "Location Name",
                        "Node Name",
                        "Bus Name",
                        "Location",
                    )
                    or node_id
                ),
                "station_name": (
                    mapping.get("station_name")
                    or _first_text(row, "Substation", "Station")
                    or _first_text(
                        row,
                        "Location Name",
                        "Node Name",
                        "Bus Name",
                        "Location",
                    )
                ),
                "zone": (
                    mapping.get("zone")
                    or _first_text(row, "Zone", "Load Zone", "BAA")
                    or None
                ),
                "ts": timestamp.isoformat().replace("+00:00", "Z") if timestamp else None,
                "lmp": row.get("LMP"),
                "energy": row.get("Energy"),
                "congestion": row.get("Congestion"),
                "loss": row.get("Loss"),
                "interval_count": _number(row.get("__interval_count")) or 1,
            }
        )
    return normalized


def _reduce_gridstatus_frame(
    frame: Any, iso: str, day: date, aggregation: str
) -> list[dict[str, Any]]:
    import pandas as pd

    if frame.empty:
        return []
    time_column = next(
        (
            column
            for column in ("Interval Start", "Time", "SCED Timestamp", "Timestamp")
            if column in frame.columns
        ),
        None,
    )
    location_column = next(
        (
            column
            for column in (
                "Location Id",
                "Location ID",
                "Pnode ID",
                "Electrical Bus",
                "Location",
            )
            if column in frame.columns
        ),
        None,
    )
    if time_column is None or location_column is None or "LMP" not in frame.columns:
        raise NodalPriceError(
            f"GridStatus returned an unexpected {iso} LMP table; "
            "a timestamp, node identifier, and LMP column are required."
        )

    timestamps = pd.to_datetime(frame[time_column], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(ISO_TIMEZONES[iso])
    else:
        timestamps = timestamps.dt.tz_convert(ISO_TIMEZONES[iso])
    local_dates = timestamps.dt.date
    selected = frame.loc[local_dates == day].copy()
    if selected.empty:
        return []

    selected["__timestamp"] = timestamps.loc[selected.index]
    selected["__node_id"] = selected[location_column].map(_text)
    selected = selected.loc[selected["__node_id"] != ""]
    if selected.empty:
        return []
    selected["LMP"] = pd.to_numeric(selected["LMP"], errors="coerce")
    numeric_columns = [
        column
        for column in ("Energy", "Congestion", "Loss")
        if column in selected.columns
    ]
    for column in numeric_columns:
        selected[column] = pd.to_numeric(selected[column], errors="coerce")

    grouped = selected.groupby("__node_id", sort=False)
    if aggregation == "latest":
        latest_indices = grouped["__timestamp"].idxmax()
        reduced = selected.loc[latest_indices].copy()
        reduced["__interval_count"] = 1
    else:
        aggregate_columns = {
            column: "mean" for column in ("LMP", *numeric_columns)
        }
        aggregate_columns["__timestamp"] = "max"
        reduced = grouped.agg(aggregate_columns).reset_index()
        reduced["__interval_count"] = grouped.size().to_numpy()

    return reduced.to_dict(orient="records")


def _coordinate_nodes_for_prices(
    rows: list[dict[str, Any]],
    iso: str,
    coordinate_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    ambiguous_nodes: set[str] = set()
    allowed_states = ISO_STATES[iso]
    for row in rows:
        node_id = _text(row.get("node_id"))
        station_name = _text(row.get("station_name"))
        if not node_id or not station_name or node_id in ambiguous_nodes:
            continue
        candidates = [
            point
            for point in coordinate_index.get(_normalize_name(station_name), [])
            if point["state"] in allowed_states
        ]
        if len(candidates) != 1:
            continue
        point = candidates[0]
        coordinate = (point["lat"], point["lng"])
        existing = nodes.get(node_id)
        if existing and (existing["lat"], existing["lng"]) != coordinate:
            nodes.pop(node_id)
            ambiguous_nodes.add(node_id)
            continue
        nodes[node_id] = {
            "node_id": node_id,
            "name": row.get("node_name") or point["name"],
            "station_name": point["name"],
            "lat": point["lat"],
            "lng": point["lng"],
        }
    return list(nodes.values())


def _fetch_gridstatus_rows(
    iso: str, day: date, market: str, aggregation: str
) -> list[dict[str, Any]]:
    client = _gridstatus_client(iso)
    gridstatus = importlib.import_module("gridstatus")
    query_date = day.isoformat()
    if (
        market == "rt"
        and aggregation == "latest"
        and day == datetime.now(ZoneInfo(ISO_TIMEZONES[iso])).date()
    ):
        query_date = "latest"

    method_name = GRIDSTATUS_METHODS[iso][market]
    try:
        frame = getattr(client, method_name)(query_date)
        mapping_rows = []
        if iso == "ERCOT":
            mapping_date = "latest" if query_date == "latest" else day.isoformat()
            mapping = client.get_settlement_points_electrical_bus_mapping(mapping_date)
            mapping_rows = mapping.to_dict(orient="records")
    except gridstatus.NoDataFoundException as error:
        raise NodalPriceError(
            f"No {market.upper()} LMP data is available for {iso} on {day.isoformat()}."
        ) from error
    except (
        RequestException,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
        gridstatus.NotSupported,
    ) as error:
        raise NodalPriceError(
            f"GridStatus could not fetch {market.upper()} LMP data for "
            f"{iso} on {day.isoformat()}: {error}"
        ) from error

    if frame.empty:
        raise NodalPriceError(
            f"No {market.upper()} LMP data is available for {iso} on {day.isoformat()}."
        )
    frame_rows = _reduce_gridstatus_frame(frame, iso, day, aggregation)
    return _normalize_gridstatus_rows(frame_rows, iso, mapping_rows)


def _average_field(values: list[tuple[dict[str, Any], datetime]], field: str) -> float | None:
    numbers = [
        number
        for row, _ in values
        if (number := _number(row.get(field))) is not None
    ]
    return sum(numbers) / len(numbers) if numbers else None


def make_nodal_price_geojson(
    rows: list[dict[str, Any]],
    coordinate_nodes: list[dict[str, Any]],
    iso: str,
    market: str,
    day: date,
    aggregation: str = "latest",
) -> dict[str, Any]:
    if iso not in GRIDSTATUS_CLASSES:
        raise ValueError(f"Unsupported ISO: {iso}")
    if aggregation not in {"latest", "average"}:
        raise ValueError(f"Unsupported price aggregation: {aggregation}")

    coordinate_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in coordinate_nodes:
        node_id = _text(node.get("node_id"))
        latitude = _number(node.get("lat"))
        longitude = _number(node.get("lng"))
        if (
            node_id
            and latitude is not None
            and longitude is not None
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            coordinate_candidates[node_id].append(node)

    zone = ZoneInfo(ISO_TIMEZONES[iso])
    grouped: dict[str, list[tuple[dict[str, Any], datetime]]] = defaultdict(list)
    for row in rows:
        node_id = _text(row.get("node_id"))
        price = _number(row.get("lmp"))
        timestamp = _timestamp_utc(row.get("ts"), ISO_TIMEZONES[iso])
        if (
            node_id
            and price is not None
            and timestamp is not None
            and timestamp.astimezone(zone).date() == day
        ):
            grouped[node_id].append((row, timestamp))

    features: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for node_id, values in grouped.items():
        if aggregation == "average":
            price = _average_field(values, "lmp")
            timestamp = None
            components = {
                key: _average_field(values, key)
                for key in ("energy", "congestion", "loss")
            }
            interval_count = sum(
                int(_number(row.get("interval_count")) or 1)
                for row, _ in values
            )
        else:
            latest_row, latest_timestamp = max(values, key=lambda value: value[1])
            price = _number(latest_row.get("lmp"))
            timestamp = latest_timestamp.isoformat().replace("+00:00", "Z")
            components = {
                key: _number(latest_row.get(key))
                for key in ("energy", "congestion", "loss")
            }
            interval_count = 1

        if price is None:
            continue
        candidates = coordinate_candidates.get(node_id, [])
        node = candidates[0] if len(candidates) == 1 else None
        node_name = str(
            (node or {}).get("name")
            or next((row.get("node_name") for row, _ in values if row.get("node_name")), "")
            or node_id
        )
        mapped_station = (node or {}).get("station_name")
        zone_name = next(
            (row.get("zone") for row, _ in values if row.get("zone")), None
        )
        coordinates = (
            [_number(node["lng"]), _number(node["lat"])]
            if node is not None
            else None
        )
        record = {
            "iso": iso,
            "node": node_name,
            "node_id": node_id,
            "zone": zone_name,
            "price": round(price, 4),
            "energy": round(components["energy"], 4) if components["energy"] is not None else None,
            "congestion": round(components["congestion"], 4) if components["congestion"] is not None else None,
            "loss": round(components["loss"], 4) if components["loss"] is not None else None,
            "timestamp": timestamp,
            "interval_count": interval_count,
            "mapped_station": mapped_station,
            "longitude": coordinates[0] if coordinates else None,
            "latitude": coordinates[1] if coordinates else None,
        }
        prices.append(record)

        if node is None:
            unmatched.append(node_id)
            continue
        features.append(
            {
                "type": "Feature",
                "id": f"{iso}-{node_id}",
                "geometry": {"type": "Point", "coordinates": coordinates},
                "properties": {
                    "name": node_name,
                    "node_id": node_id,
                    "zone": zone_name,
                    "mapped_station": mapped_station,
                    "price": record["price"],
                    "energy": record["energy"],
                    "congestion": record["congestion"],
                    "loss": record["loss"],
                    "price_timestamp": timestamp,
                    "price_iso": iso,
                    "price_market": market,
                    "price_date": day.isoformat(),
                    "interval_count": interval_count,
                },
            }
        )

    plotted_prices = [feature["properties"]["price"] for feature in features]
    all_prices = plotted_prices or [record["price"] for record in prices]
    return {
        "type": "FeatureCollection",
        "features": features,
        "prices": prices,
        "metadata": {
            "iso": iso,
            "market": market,
            "date": day.isoformat(),
            "aggregation": aggregation,
            "source": "GridStatus",
            "matched_nodes": len(features),
            "mapped_stations": len(
                {
                    tuple(feature["geometry"]["coordinates"])
                    for feature in features
                }
            ),
            "unmatched_nodes": len(unmatched),
            "unmatched_examples": unmatched[:12],
            "min_price": min(all_prices, default=None),
            "max_price": max(all_prices, default=None),
        },
    }


def fetch_nodal_price_geojson(
    iso: str,
    day: date,
    market: str,
    aggregation: str,
) -> dict[str, Any]:
    iso = iso.upper()
    market = market.lower()
    aggregation = aggregation.lower()
    if iso not in GRIDSTATUS_CLASSES:
        raise ValueError(f"Unsupported ISO: {iso}")
    if market not in {"da", "rt"}:
        raise ValueError(f"Unsupported market: {market}")
    if aggregation not in {"latest", "average"}:
        raise ValueError(f"Unsupported price aggregation: {aggregation}")
    if day > datetime.now(ZoneInfo(ISO_TIMEZONES[iso])).date():
        raise ValueError(f"{day.isoformat()} is a future operating date for {iso}.")

    rows = _fetch_gridstatus_rows(iso, day, market, aggregation)
    if not rows:
        raise NodalPriceError(
            f"No {market.upper()} LMP data is available for {iso} on {day.isoformat()}."
        )
    coordinate_index = _load_substation_coordinate_index(str(SUBSTATIONS_FILE))
    coordinate_nodes = _coordinate_nodes_for_prices(rows, iso, coordinate_index)
    result = make_nodal_price_geojson(
        rows, coordinate_nodes, iso, market, day, aggregation
    )
    if not result["prices"]:
        raise NodalPriceError(
            f"No recognizable LMP records were returned for {iso} on {day.isoformat()}."
        )
    result["metadata"]["source_nodes"] = len(
        {row["node_id"] for row in rows if row.get("node_id")}
    )
    return result
