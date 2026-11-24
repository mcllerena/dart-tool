"""GridStatus nodal-price retrieval and conservative coordinate matching."""

from __future__ import annotations

import csv
import importlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
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

PRICE_PRODUCT_DETAILS = {
    f"{market}_{component}": (market, component)
    for market in ("rt", "da", "dart")
    for component in ("lmp", "congestion", "loss", "energy")
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
MISO_BOUNDARY_FILE = (
    Path(__file__).resolve().parent / "data" / "iso_regions" / "miso.json"
)
NYISO_BOUNDARY_FILE = (
    Path(__file__).resolve().parent / "data" / "iso_regions" / "nyiso.json"
)
US_POWER_PLANT_FILES = tuple(sorted(
    (Path(__file__).resolve().parent / "data" / "power_plants").glob("us_*_pp.geojson")
))
CAISO_NORTHERNGRID_BOUNDARY_FILES = (
    Path(__file__).resolve().parent / "data" / "iso_regions" / "caiso.json",
    Path(__file__).resolve().parent / "data" / "iso_regions" / "northerngrid.json",
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


def _caiso_substation_from_pnode(value: Any) -> str:
    match = re.fullmatch(r"(.+)_\d+(?:\.\d+)?_N\d+", _text(value), flags=re.IGNORECASE)
    return match.group(1) if match else ""


_ISONE_VOLTAGE_PATTERN = (
    r"(?:500|345|230|161|138|115|69|46|34\.5|27\.6|23|18\.0|13\.8|"
    r"13\.2|12\.47|12|6\.9|4\.16)"
)
_ISONE_NODE_PATTERN = re.compile(
    rf"^(?:UN\.)?(?P<station>.*?)(?:\s*){_ISONE_VOLTAGE_PATTERN}"
    r"(?P<suffix>[A-Z0-9]+)$",
    re.IGNORECASE,
)


def _isone_substation_from_node(value: Any) -> str:
    match = _ISONE_NODE_PATTERN.fullmatch(_text(value))
    return match.group("station").strip() if match else ""


def _spp_substation_from_pnode(value: Any) -> str:
    pnode = _text(value).upper()
    has_voltage_suffix = bool(
        re.search(r"_\d{2,3}(?:\.\d+)?_\d+$", pnode)
    )
    pnode = re.sub(r"_\d{2,3}(?:\.\d+)?_\d+$", "", pnode)

    owner_prefix_length = 3 if pnode.startswith(("SPS", "LES", "MPS")) else 4
    if len(pnode) <= owner_prefix_length:
        return ""
    station = pnode[owner_prefix_length:]
    station = re.sub(r"^\d{1,3}_", "", station)
    if "_" not in station:
        station = re.sub(r"^\d{1,3}(?=[A-Z]{3,})", "", station)

    equipment = re.search(r"(?:_T)?(?:LD|GEN)", station[4:])
    if equipment:
        station = station[:4 + equipment.start()]
    elif not has_voltage_suffix:
        return ""
    return station.strip("_")


@lru_cache(maxsize=4)
def _load_footprint_polygons(
    paths: tuple[str, ...],
    footprint_name: str,
) -> tuple[
    tuple[
        float,
        float,
        float,
        float,
        tuple[tuple[tuple[float, float], ...], ...],
    ],
    ...,
]:
    polygons = []
    for path_string in paths:
        path = Path(path_string)
        try:
            with path.open(encoding="utf-8") as source:
                collection = json.load(source)
        except (OSError, json.JSONDecodeError) as error:
            raise NodalPriceError(
                f"Unable to load the {footprint_name} footprint from {path}: {error}"
            ) from error
        for feature in collection.get("features", []):
            geometry = feature.get("geometry") or {}
            geometry_type = geometry.get("type")
            coordinates = geometry.get("coordinates") or []
            if geometry_type == "Polygon":
                polygon_coordinates = [coordinates]
            elif geometry_type == "MultiPolygon":
                polygon_coordinates = coordinates
            else:
                continue
            for polygon_coordinates_item in polygon_coordinates:
                rings = tuple(
                    tuple((float(point[0]), float(point[1])) for point in ring)
                    for ring in polygon_coordinates_item
                    if len(ring) >= 4
                )
                if not rings or not rings[0]:
                    continue
                longitudes = [point[0] for point in rings[0]]
                latitudes = [point[1] for point in rings[0]]
                polygons.append((
                    min(longitudes),
                    min(latitudes),
                    max(longitudes),
                    max(latitudes),
                    rings,
                ))
    if not polygons:
        raise NodalPriceError(f"The {footprint_name} footprint files contain no polygons.")
    return tuple(polygons)


def _point_in_ring(
    longitude: float, latitude: float, ring: tuple[tuple[float, float], ...]
) -> bool:
    inside = False
    previous_longitude, previous_latitude = ring[-1]
    for current_longitude, current_latitude in ring:
        cross = (
            (longitude - previous_longitude)
            * (current_latitude - previous_latitude)
            - (latitude - previous_latitude)
            * (current_longitude - previous_longitude)
        )
        if (
            abs(cross) <= 1e-10
            and min(previous_longitude, current_longitude) - 1e-10
            <= longitude
            <= max(previous_longitude, current_longitude) + 1e-10
            and min(previous_latitude, current_latitude) - 1e-10
            <= latitude
            <= max(previous_latitude, current_latitude) + 1e-10
        ):
            return True
        if (current_latitude > latitude) != (previous_latitude > latitude):
            crossing_longitude = (
                (previous_longitude - current_longitude)
                * (latitude - current_latitude)
                / (previous_latitude - current_latitude)
                + current_longitude
            )
            if longitude < crossing_longitude:
                inside = not inside
        previous_longitude, previous_latitude = current_longitude, current_latitude
    return inside


def _is_in_footprint(
    longitude: float,
    latitude: float,
    polygons: tuple[
        tuple[
            float,
            float,
            float,
            float,
            tuple[tuple[tuple[float, float], ...], ...],
        ],
        ...,
    ],
) -> bool:
    for min_longitude, min_latitude, max_longitude, max_latitude, rings in polygons:
        if not (
            min_longitude <= longitude <= max_longitude
            and min_latitude <= latitude <= max_latitude
            and _point_in_ring(longitude, latitude, rings[0])
        ):
            continue
        if not any(_point_in_ring(longitude, latitude, hole) for hole in rings[1:]):
            return True
    return False


def _is_in_caiso_northerngrid_footprint(longitude: float, latitude: float) -> bool:
    polygons = _load_footprint_polygons(
        tuple(str(path) for path in CAISO_NORTHERNGRID_BOUNDARY_FILES),
        "CAISO/NorthernGrid",
    )
    return _is_in_footprint(longitude, latitude, polygons)


def _is_in_miso_footprint(longitude: float, latitude: float) -> bool:
    polygons = _load_footprint_polygons((str(MISO_BOUNDARY_FILE),), "MISO")
    return _is_in_footprint(longitude, latitude, polygons)


def _is_in_nyiso_footprint(longitude: float, latitude: float) -> bool:
    polygons = _load_footprint_polygons((str(NYISO_BOUNDARY_FILE),), "NYISO")
    return _is_in_footprint(longitude, latitude, polygons)


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
                    "city": _text(row.get("CITY")),
                    "county": _text(row.get("COUNTY")),
                    "type": _text(row.get("TYPE")).upper(),
                    "state": state,
                    "lat": latitude,
                    "lng": longitude,
                },
            )
    return {key: list(points.values()) for key, points in index.items()}


@lru_cache(maxsize=4)
def _load_miso_substation_alias_index(
    path: str,
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    aliases: dict[
        str, dict[tuple[str, float, float], tuple[str, dict[str, Any]]]
    ] = defaultdict(dict)
    for points in _load_substation_coordinate_index(path).values():
        for point in points:
            if point.get("type") != "SUBSTATION":
                continue
            for field, value in (
                ("substation_name", point.get("name")),
                ("city", point.get("city")),
                ("county", point.get("county")),
            ):
                for key in _miso_alias_keys(value):
                    aliases[key][(field, point["lat"], point["lng"])] = (field, point)
    return {key: list(matches.values()) for key, matches in aliases.items()}


@lru_cache(maxsize=4)
def _load_nyiso_substation_alias_index(
    path: str,
) -> dict[str, list[dict[str, Any]]]:
    aliases: dict[str, dict[tuple[float, float], dict[str, Any]]] = defaultdict(dict)
    for points in _load_substation_coordinate_index(path).values():
        for point in points:
            name = _text(point.get("name"))
            if (
                point.get("state") != "NY"
                or point.get("type") != "SUBSTATION"
                or not name
                or name.upper().startswith("UNKNOWN")
                or not _is_in_nyiso_footprint(point["lng"], point["lat"])
            ):
                continue
            key = _normalize_name(name)
            if key:
                aliases[key][(point["lat"], point["lng"])] = point
    return {key: list(points.values()) for key, points in aliases.items()}


def _nyiso_plant_alias_keys(value: str) -> set[str]:
    aliases = {_normalize_name(value)}
    name = re.sub(r"^NY\d+\s*[-_]\s*", "", value, flags=re.IGNORECASE)
    name = re.sub(
        r"\s+(?:HYDRO(?:ELECTRIC)?|WIND(?:\s+(?:POWER|FARM))?|SOLAR|"
        r"GENERATING\s+STATION|GENERATION\s+STATION|POWER\s+PLANT|"
        r"COGEN(?:ERATION)?|CGEN|LANDFILL\s+GAS)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    stem_key = _normalize_name(name)
    if len(stem_key) >= 4:
        aliases.add(stem_key)
    return {key for key in aliases if len(key) >= 4}


@lru_cache(maxsize=1)
def _load_miso_plant_alias_index() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, dict[tuple[str, float, float], dict[str, Any]]] = defaultdict(dict)
    for path in US_POWER_PLANT_FILES:
        try:
            with path.open(encoding="utf-8") as source:
                collection = json.load(source)
        except (OSError, json.JSONDecodeError) as error:
            raise NodalPriceError(
                f"Unable to load power-plant coordinates from {path}: {error}"
            ) from error
        for feature in collection.get("features", []):
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            properties = feature.get("properties") or {}
            if geometry.get("type") != "Point" or len(coordinates) < 2:
                continue
            longitude = _number(coordinates[0])
            latitude = _number(coordinates[1])
            state = _text(properties.get("Plant State")).upper()
            name = _first_text(properties, "Plant Name")
            if (
                not name
                or not state
                or longitude is None
                or latitude is None
                or not -180 <= longitude <= 180
                or not -90 <= latitude <= 90
            ):
                continue
            point = {
                "name": name,
                "state": state,
                "county": _text(properties.get("County")),
                "lat": latitude,
                "lng": longitude,
            }
            for key in _miso_alias_keys(name):
                index[key][(name, latitude, longitude)] = point
    return {key: list(points.values()) for key, points in index.items()}


@lru_cache(maxsize=1)
def _load_nyiso_plant_alias_index() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, dict[tuple[str, float, float], dict[str, Any]]] = defaultdict(dict)
    for path in US_POWER_PLANT_FILES:
        try:
            with path.open(encoding="utf-8") as source:
                collection = json.load(source)
        except (OSError, json.JSONDecodeError) as error:
            raise NodalPriceError(
                f"Unable to load power-plant coordinates from {path}: {error}"
            ) from error
        for feature in collection.get("features", []):
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            properties = feature.get("properties") or {}
            if geometry.get("type") != "Point" or len(coordinates) < 2:
                continue
            longitude = _number(coordinates[0])
            latitude = _number(coordinates[1])
            state = _text(properties.get("Plant State")).upper()
            name = _first_text(properties, "Plant Name")
            if (
                state != "NY"
                or not name
                or name.upper().startswith("UNKNOWN")
                or longitude is None
                or latitude is None
                or not -180 <= longitude <= 180
                or not -90 <= latitude <= 90
                or not _is_in_nyiso_footprint(longitude, latitude)
            ):
                continue
            point = {
                "name": name,
                "state": state,
                "county": _text(properties.get("County")),
                "lat": latitude,
                "lng": longitude,
            }
            for key in _nyiso_plant_alias_keys(name):
                index[key][(name, latitude, longitude)] = point
    return {key: list(points.values()) for key, points in index.items()}


def _nyiso_node_name_tokens(node_id: str, node_name: str) -> set[str]:
    tokens = set()
    equipment_suffix = re.compile(
        r"_(?:GT|GTG|IC|WT_PWR|WINDPWR|WIND|SOLAR|HYDRO|HYD|ESR|LFGE|"
        r"COGEN|DIESEL|DRP|LBMP)(?:_.*)?$",
        re.IGNORECASE,
    )
    for value in {node_id, node_name}:
        full_key = _normalize_name(value)
        if len(full_key) >= 4:
            tokens.add(full_key)
        stem = re.split(r"_{3,}", value, maxsplit=1)[0].rstrip("_")
        stem_key = _normalize_name(stem)
        if len(stem_key) >= 4:
            tokens.add(stem_key)
        equipment_key = _normalize_name(equipment_suffix.sub("", value))
        if len(equipment_key) >= 4:
            tokens.add(equipment_key)
    return tokens


def _nyiso_fuzzy_alias_key_index(
    substation_aliases: dict[str, list[dict[str, Any]]],
    plant_aliases: dict[str, list[dict[str, Any]]],
) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for key in sorted(set(substation_aliases) | set(plant_aliases)):
        if len(key) >= 5:
            index[key[:2]].append(key)
    return index


def _nyiso_coordinate_for_node(
    row: dict[str, Any],
    substation_aliases: dict[str, list[dict[str, Any]]],
    plant_aliases: dict[str, list[dict[str, Any]]],
    fuzzy_alias_keys: dict[str, list[str]],
) -> dict[str, Any] | None:
    if _text(row.get("node_type")).casefold() != "generator":
        return None

    scores: dict[tuple[float, float], dict[str, Any]] = {}
    source_weights = {"plant": 120, "substation_name": 100}

    def add_candidate(
        point: dict[str, Any],
        source: str,
        token: str,
        quality: float = 1.0,
        fuzzy: bool = False,
    ) -> None:
        if point["state"] != "NY":
            return
        if not _is_in_nyiso_footprint(point["lng"], point["lat"]):
            return
        coordinate = (point["lat"], point["lng"])
        candidate = scores.setdefault(
            coordinate,
            {
                "point": point,
                "score": 0.0,
                "token_scores": {},
                "source": source,
                "source_score": 0.0,
            },
        )
        contribution = source_weights[source] * quality
        previous = candidate["token_scores"].get(token, 0.0)
        if contribution > previous:
            candidate["score"] += contribution - previous
            candidate["token_scores"][token] = contribution
        if contribution > candidate["source_score"]:
            candidate["source_score"] = contribution
            candidate["source"] = f"fuzzy-{source}" if fuzzy else source

    tokens = _nyiso_node_name_tokens(
        _text(row.get("node_id")), _text(row.get("node_name"))
    )
    for token in tokens:
        has_exact_match = False
        for point in plant_aliases.get(token, []):
            add_candidate(point, "plant", token)
            has_exact_match = True
        for point in substation_aliases.get(token, []):
            add_candidate(point, "substation_name", token)
            has_exact_match = True
        if has_exact_match or len(token) < 5:
            continue

        fuzzy_matches = []
        for key in fuzzy_alias_keys.get(token[:2], []):
            if abs(len(key) - len(token)) > 4:
                continue
            similarity = SequenceMatcher(
                None, token, key, autojunk=False
            ).ratio()
            if similarity >= 0.88:
                fuzzy_matches.append((similarity, key))
        if not fuzzy_matches:
            continue
        fuzzy_matches.sort(reverse=True)
        best_similarity = fuzzy_matches[0][0]
        for similarity, key in fuzzy_matches:
            if best_similarity - similarity > 0.04:
                break
            for point in plant_aliases.get(key, []):
                add_candidate(point, "plant", token, similarity, fuzzy=True)
            for point in substation_aliases.get(key, []):
                add_candidate(
                    point, "substation_name", token, similarity, fuzzy=True
                )

    eligible = [
        candidate
        for candidate in scores.values()
        if candidate["score"] >= source_weights["substation_name"] * 0.9
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda candidate: candidate["score"], reverse=True)
    if (
        len(eligible) > 1
        and eligible[0]["score"] - eligible[1]["score"] < 5
    ):
        return None

    best = eligible[0]
    point = best["point"]
    location_name = point.get("name") or point.get("city") or point.get("county")
    return {
        "name": location_name,
        "station_name": location_name,
        "lat": point["lat"],
        "lng": point["lng"],
        "mapping_method": f"approximate-nyiso-{best['source']}",
    }


def _miso_alias_keys(value: Any) -> set[str]:
    key = _normalize_name(value)
    if not key:
        return set()
    aliases = {key}
    for abbreviation, expansion in (
        ("CRK", "CREEK"),
        ("CTR", "CENTER"),
        ("CO", "COUNTY"),
        ("FT", "FORT"),
    ):
        if key.endswith(abbreviation):
            aliases.add(key[:-len(abbreviation)] + expansion)
    if key.startswith("ST") and len(key) > 3:
        aliases.add("SAINT" + key[2:])
    return aliases


def _miso_node_name_tokens(node_id: str, node_name: str) -> set[str]:
    tokens = set()
    for value in {node_id, node_name}:
        parts = [part for part in re.split(r"[.\s/_-]+", value) if part]
        if len(parts) > 1:
            parts = parts[1:]
        while len(parts) > 1 and parts[-1].isdigit():
            parts.pop()
        for start in range(len(parts)):
            token = "".join(parts[start:])
            if len(token) >= 3:
                tokens.update(_miso_alias_keys(token))
        for part in parts:
            if len(part) >= 3:
                tokens.update(_miso_alias_keys(part))
    return tokens


def _miso_fuzzy_alias_key_index(
    substation_aliases: dict[str, list[tuple[str, dict[str, Any]]]],
    plant_aliases: dict[str, list[dict[str, Any]]],
) -> dict[str, list[str]]:
    keys = set(plant_aliases)
    keys.update(
        key
        for key, entries in substation_aliases.items()
        if any(source == "substation_name" for source, _ in entries)
    )
    index: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        if len(key) >= 5:
            index[key[:2]].append(key)
    return index


def _miso_coordinate_for_node(
    row: dict[str, Any],
    substation_aliases: dict[str, list[tuple[str, dict[str, Any]]]],
    plant_aliases: dict[str, list[dict[str, Any]]],
    fuzzy_alias_keys: dict[str, list[str]],
) -> dict[str, Any] | None:
    if _text(row.get("node_type")).casefold() != "gennode":
        return None

    scores: dict[tuple[float, float], dict[str, Any]] = {}
    source_weights = {
        "plant": 120,
        "substation_name": 100,
        "city": 75,
        "county": 35,
    }

    def add_candidate(
        point: dict[str, Any],
        source: str,
        token: str,
        quality: float = 1.0,
        fuzzy: bool = False,
    ) -> None:
        if point["state"] not in ISO_STATES["MISO"]:
            return
        if not _is_in_miso_footprint(point["lng"], point["lat"]):
            return
        coordinate = (point["lat"], point["lng"])
        candidate = scores.setdefault(
            coordinate,
            {
                "point": point,
                "score": 0.0,
                "token_scores": {},
                "source": source,
                "source_score": 0.0,
            },
        )
        contribution = source_weights[source] * quality
        previous = candidate["token_scores"].get(token, 0.0)
        if contribution > previous:
            candidate["score"] += contribution - previous
            candidate["token_scores"][token] = contribution
        if contribution > candidate["source_score"]:
            candidate["source_score"] = contribution
            candidate["source"] = f"fuzzy-{source}" if fuzzy else source

    tokens = _miso_node_name_tokens(
        _text(row.get("node_id")), _text(row.get("node_name"))
    )
    for token in tokens:
        has_exact_match = False
        for point in plant_aliases.get(token, []):
            add_candidate(point, "plant", token)
            has_exact_match = True
        for source, point in substation_aliases.get(token, []):
            add_candidate(point, source, token)
            has_exact_match = True
        if has_exact_match or len(token) < 5:
            continue

        fuzzy_matches = []
        for key in fuzzy_alias_keys.get(token[:2], []):
            if abs(len(key) - len(token)) > 3:
                continue
            similarity = SequenceMatcher(
                None, token, key, autojunk=False
            ).ratio()
            if similarity >= 0.84:
                fuzzy_matches.append((similarity, key))
        if not fuzzy_matches:
            continue
        fuzzy_matches.sort(reverse=True)
        best_similarity = fuzzy_matches[0][0]
        for similarity, key in fuzzy_matches:
            if best_similarity - similarity > 0.04:
                break
            for point in plant_aliases.get(key, []):
                add_candidate(point, "plant", token, similarity, fuzzy=True)
            for source, point in substation_aliases.get(key, []):
                if source == "substation_name":
                    add_candidate(point, source, token, similarity, fuzzy=True)

    eligible = [
        candidate
        for candidate in scores.values()
        if candidate["score"] >= source_weights["city"]
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda candidate: candidate["score"], reverse=True)
    if (
        len(eligible) > 1
        and eligible[0]["score"] - eligible[1]["score"] < 5
    ):
        return None

    best = eligible[0]
    point = best["point"]
    location_name = point.get("name")
    if not location_name or location_name.upper().startswith("UNKNOWN"):
        location_name = point.get("city") or point.get("county") or location_name
    return {
        "name": location_name,
        "station_name": location_name,
        "lat": point["lat"],
        "lng": point["lng"],
        "mapping_method": f"approximate-miso-{best['source']}",
    }


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
        node_name = (
            mapping.get("node_name")
            or _first_text(
                row,
                "Location Name",
                "Node Name",
                "Bus Name",
                "Location",
            )
            or node_id
        )
        station_name = (
            mapping.get("station_name")
            or _first_text(row, "Substation", "Station")
        )
        if iso == "CAISO":
            station_name = _caiso_substation_from_pnode(node_id) or station_name or node_name
        elif iso == "ISONE":
            station_name = (
                _isone_substation_from_node(node_name)
                or station_name
                or node_name
            )
        elif iso == "SPP":
            station_name = (
                _spp_substation_from_pnode(node_id)
                or station_name
                or node_name
            )
        elif not station_name:
            station_name = node_name
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
                "node_name": node_name,
                "station_name": station_name,
                "node_type": _first_text(row, "Location Type", "Type") or None,
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
    frame: Any,
    iso: str,
    day: date,
    aggregation: str,
    as_of_time: time | None = None,
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
    local_timestamps = timestamps.dt.tz_convert(ISO_TIMEZONES[iso])
    mask = local_timestamps.dt.date == day
    if aggregation == "latest" and as_of_time is not None:
        selected_minute = as_of_time.hour * 60 + as_of_time.minute
        row_minutes = local_timestamps.dt.hour * 60 + local_timestamps.dt.minute
        mask &= row_minutes <= selected_minute
    selected = frame.loc[mask].copy()
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
    miso_substation_aliases = (
        _load_miso_substation_alias_index(str(SUBSTATIONS_FILE))
        if iso == "MISO"
        else {}
    )
    miso_plant_aliases = _load_miso_plant_alias_index() if iso == "MISO" else {}
    miso_fuzzy_alias_keys = (
        _miso_fuzzy_alias_key_index(miso_substation_aliases, miso_plant_aliases)
        if iso == "MISO"
        else {}
    )
    nyiso_substation_aliases = (
        _load_nyiso_substation_alias_index(str(SUBSTATIONS_FILE))
        if iso == "NYISO"
        else {}
    )
    nyiso_plant_aliases = (
        _load_nyiso_plant_alias_index() if iso == "NYISO" else {}
    )
    nyiso_fuzzy_alias_keys = (
        _nyiso_fuzzy_alias_key_index(nyiso_substation_aliases, nyiso_plant_aliases)
        if iso == "NYISO"
        else {}
    )
    for row in rows:
        node_id = _text(row.get("node_id"))
        station_name = _text(row.get("station_name"))
        if not node_id or node_id in ambiguous_nodes:
            continue
        if (
            iso == "NYISO"
            and _text(row.get("node_type")).casefold() == "generator"
        ):
            point = _nyiso_coordinate_for_node(
                row,
                nyiso_substation_aliases,
                nyiso_plant_aliases,
                nyiso_fuzzy_alias_keys,
            )
            if point is None:
                continue
            point["node_id"] = node_id
            point["name"] = row.get("node_name") or point["name"]
            existing = nodes.get(node_id)
            if existing and (existing["lat"], existing["lng"]) != (
                point["lat"], point["lng"]
            ):
                nodes.pop(node_id)
                ambiguous_nodes.add(node_id)
                continue
            nodes[node_id] = point
            continue
        if iso == "MISO":
            point = _miso_coordinate_for_node(
                row,
                miso_substation_aliases,
                miso_plant_aliases,
                miso_fuzzy_alias_keys,
            )
            if point is None:
                continue
            point["node_id"] = node_id
            point["name"] = row.get("node_name") or point["name"]
            existing = nodes.get(node_id)
            if existing and (existing["lat"], existing["lng"]) != (
                point["lat"], point["lng"]
            ):
                nodes.pop(node_id)
                ambiguous_nodes.add(node_id)
                continue
            nodes[node_id] = point
            continue
        if not station_name:
            continue
        candidates = coordinate_index.get(_normalize_name(station_name), [])
        if iso == "CAISO":
            candidates = [
                point
                for point in candidates
                if _is_in_caiso_northerngrid_footprint(point["lng"], point["lat"])
            ]
            caiso_candidates = [
                point for point in candidates if point["state"] in ISO_STATES[iso]
            ]
            if len(caiso_candidates) == 1:
                candidates = caiso_candidates
        else:
            candidates = [
                point for point in candidates if point["state"] in allowed_states
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
    iso: str,
    day: date,
    market: str,
    aggregation: str,
    as_of_time: time | None = None,
) -> list[dict[str, Any]]:
    client = _gridstatus_client(iso)
    gridstatus = importlib.import_module("gridstatus")
    query_date = day.isoformat()
    current_time = datetime.now(ZoneInfo(ISO_TIMEZONES[iso]))
    if (
        market == "rt"
        and aggregation == "latest"
        and as_of_time is None
        and day == current_time.date()
    ):
        as_of_time = current_time.time().replace(second=0, microsecond=0)

    method_name = GRIDSTATUS_METHODS[iso][market]
    try:
        if market == "rt" and aggregation == "latest" and as_of_time:
            query_start = datetime.combine(
                day,
                as_of_time.replace(
                    minute=(as_of_time.minute // 5) * 5,
                    second=0,
                    microsecond=0,
                ),
                ZoneInfo(ISO_TIMEZONES[iso]),
            )
            if day == current_time.date():
                current_interval = current_time.replace(
                    minute=(current_time.minute // 5) * 5,
                    second=0,
                    microsecond=0,
                )
                query_start = min(query_start, current_interval)
            query_end = query_start + timedelta(minutes=5)
            query_kwargs = {"end": query_end.isoformat()}
            if iso == "CAISO":
                query_kwargs["sleep"] = 0
            frame = getattr(client, method_name)(query_start.isoformat(), **query_kwargs)
        elif market == "da" and aggregation == "latest" and as_of_time:
            query_start = datetime.combine(
                day,
                as_of_time.replace(minute=0, second=0, microsecond=0),
                ZoneInfo(ISO_TIMEZONES[iso]),
            )
            query_end = query_start + timedelta(hours=1)
            frame = getattr(client, method_name)(
                query_start.isoformat(), end=query_end.isoformat()
            )
        else:
            frame = getattr(client, method_name)(query_date)
        mapping_rows = []
        if iso == "ERCOT":
            mapping_date = "latest" if day == current_time.date() else day.isoformat()
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
    frame_rows = _reduce_gridstatus_frame(
        frame, iso, day, aggregation, as_of_time
    )
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
    as_of_time: time | None = None,
) -> dict[str, Any]:
    if iso not in GRIDSTATUS_CLASSES:
        raise ValueError(f"Unsupported ISO: {iso}")
    if aggregation not in {"latest", "average"}:
        raise ValueError(f"Unsupported price aggregation: {aggregation}")
    requested_local_time = (
        as_of_time.strftime("%H:%M")
        if aggregation == "latest" and as_of_time
        else None
    )

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
            local_timestamp = None
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
            local_timestamp = latest_timestamp.astimezone(zone).isoformat()
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
        mapping_method = (node or {}).get("mapping_method")
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
            "lmp": round(price, 4),
            "requested_local_time": requested_local_time,
            "energy": round(components["energy"], 4) if components["energy"] is not None else None,
            "congestion": round(components["congestion"], 4) if components["congestion"] is not None else None,
            "loss": round(components["loss"], 4) if components["loss"] is not None else None,
            "timestamp": timestamp,
            "local_timestamp": local_timestamp,
            "interval_count": interval_count,
            "mapped_station": mapped_station,
            "mapping_method": mapping_method,
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
                    "mapping_method": mapping_method,
                    "price": record["price"],
                    "lmp": record["lmp"],
                    "energy": record["energy"],
                    "congestion": record["congestion"],
                    "loss": record["loss"],
                    "price_timestamp": timestamp,
                    "price_local_timestamp": local_timestamp,
                    "requested_local_time": requested_local_time,
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
            "requested_local_time": requested_local_time,
            "aggregation": aggregation,
            "source": "GridStatus",
            "matched_nodes": len(features),
            "approximate_mapped_nodes": sum(
                bool(feature["properties"].get("mapping_method"))
                for feature in features
            ),
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
    as_of_time: time | None = None,
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
    current_time = datetime.now(ZoneInfo(ISO_TIMEZONES[iso]))
    if as_of_time is None and aggregation == "latest" and day == current_time.date():
        as_of_time = current_time.time().replace(second=0, microsecond=0)
    if as_of_time is not None and not isinstance(as_of_time, time):
        raise ValueError("Selected local time must use HH:MM format.")

    rows = _fetch_gridstatus_rows(iso, day, market, aggregation, as_of_time)
    if not rows:
        raise NodalPriceError(
            f"No {market.upper()} LMP data is available for {iso} on {day.isoformat()}."
        )
    coordinate_index = _load_substation_coordinate_index(str(SUBSTATIONS_FILE))
    coordinate_nodes = _coordinate_nodes_for_prices(rows, iso, coordinate_index)
    result = make_nodal_price_geojson(
        rows, coordinate_nodes, iso, market, day, aggregation, as_of_time
    )
    if not result["prices"]:
        raise NodalPriceError(
            f"No recognizable LMP records were returned for {iso} on {day.isoformat()}."
        )
    result["metadata"]["source_nodes"] = len(
        {row["node_id"] for row in rows if row.get("node_id")}
    )
    return result


def _set_price_metric(
    result: dict[str, Any],
    product: str,
    market: str,
    component: str,
) -> dict[str, Any]:
    prices_by_node: dict[str, dict[str, Any]] = {}
    records = []
    for source_record in result["prices"]:
        value = _number(source_record.get(component))
        if value is None:
            continue
        record = dict(source_record)
        record["price"] = round(value, 4)
        record["price_product"] = product
        records.append(record)
        prices_by_node[record["node_id"]] = record

    if not records:
        raise NodalPriceError(
            f"{market.upper()} {component.upper()} component prices are unavailable "
            f"for {result['metadata']['iso']}."
        )

    features = []
    for source_feature in result["features"]:
        node_id = source_feature["properties"]["node_id"]
        record = prices_by_node.get(node_id)
        if record is None:
            continue
        features.append({
            **source_feature,
            "properties": {
                **source_feature["properties"],
                "price": record["price"],
                "price_product": product,
            },
        })

    values = [record["price"] for record in records]
    metadata = {
        **result["metadata"],
        "market": market,
        "product": product,
        "matched_nodes": len(features),
        "unmatched_nodes": len(records) - len(features),
        "min_price": min(values),
        "max_price": max(values),
    }
    return {
        **result,
        "features": features,
        "prices": records,
        "metadata": metadata,
    }


def _make_dart_product(
    day_ahead: dict[str, Any],
    real_time: dict[str, Any],
    product: str,
    component: str,
) -> dict[str, Any]:
    da_by_node = {record["node_id"]: record for record in day_ahead["prices"]}
    rt_by_node = {record["node_id"]: record for record in real_time["prices"]}
    da_features = {
        feature["properties"]["node_id"]: feature
        for feature in day_ahead["features"]
    }
    rt_features = {
        feature["properties"]["node_id"]: feature
        for feature in real_time["features"]
    }

    prices = []
    features = []
    unmatched = []
    for node_id in da_by_node.keys() & rt_by_node.keys():
        da_record = da_by_node[node_id]
        rt_record = rt_by_node[node_id]
        component_differences = {}
        for field in ("lmp", "energy", "congestion", "loss"):
            da_value = _number(da_record.get(field))
            rt_value = _number(rt_record.get(field))
            component_differences[field] = (
                round(da_value - rt_value, 4)
                if da_value is not None and rt_value is not None
                else None
            )
        price = component_differences[component]
        if price is None:
            continue

        record = dict(rt_record)
        record.update(component_differences)
        record.update({
            "price": price,
            "price_product": product,
            "real_time_value": rt_record.get(component),
            "day_ahead_value": da_record.get(component),
            "real_time_timestamp": rt_record.get("timestamp"),
            "real_time_local_timestamp": rt_record.get("local_timestamp"),
            "day_ahead_timestamp": da_record.get("timestamp"),
            "day_ahead_local_timestamp": da_record.get("local_timestamp"),
            "real_time_interval_count": rt_record.get("interval_count"),
            "day_ahead_interval_count": da_record.get("interval_count"),
        })
        for field in ("lmp", "energy", "congestion", "loss"):
            record[f"real_time_{field}"] = rt_record.get(field)
            record[f"day_ahead_{field}"] = da_record.get(field)
        prices.append(record)

        feature = rt_features.get(node_id) or da_features.get(node_id)
        if feature is None:
            unmatched.append(node_id)
            continue
        feature = {
            **feature,
            "properties": {
                **feature["properties"],
                **component_differences,
                "price": price,
                "price_product": product,
                "price_market": "dart",
                "price_timestamp": rt_record.get("timestamp"),
                "price_local_timestamp": rt_record.get("local_timestamp"),
                "real_time_value": record["real_time_value"],
                "day_ahead_value": record["day_ahead_value"],
                "real_time_local_timestamp": record["real_time_local_timestamp"],
                "day_ahead_timestamp": record["day_ahead_timestamp"],
                "day_ahead_local_timestamp": record["day_ahead_local_timestamp"],
            },
        }
        for field in ("lmp", "energy", "congestion", "loss"):
            feature["properties"][f"real_time_{field}"] = record[f"real_time_{field}"]
            feature["properties"][f"day_ahead_{field}"] = record[f"day_ahead_{field}"]
        features.append(feature)

    if not prices:
        raise NodalPriceError(
            f"No matching day-ahead and real-time {component.upper()} prices are "
            f"available for {day_ahead['metadata']['iso']}."
        )

    values = [record["price"] for record in prices]
    metadata = {
        **real_time["metadata"],
        "market": "dart",
        "product": product,
        "derived_from": ["day-ahead", "real-time"],
        "source_nodes": len(prices),
        "matched_nodes": len(features),
        "approximate_mapped_nodes": sum(
            bool(feature["properties"].get("mapping_method"))
            for feature in features
        ),
        "unmatched_nodes": len(unmatched),
        "unmatched_examples": unmatched[:12],
        "min_price": min(values),
        "max_price": max(values),
    }
    return {
        "type": "FeatureCollection",
        "features": features,
        "prices": prices,
        "metadata": metadata,
    }


def fetch_nodal_price_product_geojson(
    iso: str,
    day: date,
    product: str,
    aggregation: str,
    as_of_time: time | None = None,
    base_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    product = product.lower()
    details = PRICE_PRODUCT_DETAILS.get(product)
    if details is None:
        raise ValueError(f"Unsupported nodal price product: {product}")
    market, component = details
    cached = base_results or {}
    if market == "dart":
        day_ahead = cached.get("da") or fetch_nodal_price_geojson(
            iso, day, "da", aggregation, as_of_time
        )
        real_time = cached.get("rt") or fetch_nodal_price_geojson(
            iso, day, "rt", aggregation, as_of_time
        )
        return _make_dart_product(day_ahead, real_time, product, component)

    result = cached.get(market) or fetch_nodal_price_geojson(
        iso, day, market, aggregation, as_of_time
    )
    return _set_price_metric(result, product, market, component)
