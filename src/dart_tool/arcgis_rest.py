"""Helpers for downloading ArcGIS feature layers through the REST API."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

MAX_ATTEMPTS = 4
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def request_json(
    query_url: str,
    params: dict[str, Any],
    *,
    post: bool = False,
) -> dict[str, Any]:
    encoded_params = urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json",
        "User-Agent": "DART Grid Atlas ArcGIS exporter",
    }
    if post:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        query_url if post else f"{query_url}?{encoded_params}",
        data=encoded_params.encode("utf-8") if post else None,
        headers=headers,
    )

    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"ArcGIS request failed with HTTP {error.code}") from error
            time.sleep(2**attempt)
            continue
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"ArcGIS request failed: {error}") from error
            time.sleep(2**attempt)
            continue

        if not isinstance(payload, dict):
            raise RuntimeError("ArcGIS returned a non-object JSON response")  # noqa: TRY004
        if "error" in payload:
            details = payload["error"]
            if isinstance(details, dict):
                raise RuntimeError(
                    f"ArcGIS query error {details.get('code')}: {details.get('message')}"
                )
            raise RuntimeError(f"ArcGIS query error: {details}")
        return payload

    raise RuntimeError("ArcGIS request retries were exhausted")


def fetch_object_ids(layer_url: str) -> list[int]:
    response = request_json(
        f"{layer_url.rstrip('/')}/query",
        {
            "where": "1=1",
            "returnIdsOnly": "true",
            "returnGeometry": "false",
            "f": "json",
        },
    )
    object_ids = response.get("objectIds")
    if not isinstance(object_ids, list):
        raise RuntimeError("ArcGIS did not return an objectIds list")  # noqa: TRY004

    try:
        normalized_ids = [int(object_id) for object_id in object_ids]
    except (TypeError, ValueError) as error:
        raise RuntimeError("ArcGIS returned an invalid object ID") from error
    if len(set(normalized_ids)) != len(normalized_ids):
        raise RuntimeError("ArcGIS returned duplicate object IDs")
    return normalized_ids


def feature_object_id(feature: dict[str, Any], object_id_field: str) -> int:
    object_id = feature.get("id")
    if object_id is None:
        properties = feature.get("properties")
        if isinstance(properties, dict):
            object_id = properties.get(object_id_field)
    try:
        return int(object_id)
    except (TypeError, ValueError) as error:
        raise RuntimeError("GeoJSON feature is missing a valid object ID") from error


def iter_feature_batches(
    layer_url: str,
    object_ids: list[int],
    *,
    object_id_field: str,
    records_per_request: int,
) -> Iterator[list[dict[str, Any]]]:
    if records_per_request <= 0:
        raise ValueError("records_per_request must be positive")

    expected_ids = set(object_ids)
    if len(expected_ids) != len(object_ids):
        raise ValueError("object_ids must not contain duplicates")
    seen_ids: set[int] = set()

    for start in range(0, len(object_ids), records_per_request):
        batch = object_ids[start : start + records_per_request]
        response = request_json(
            f"{layer_url.rstrip('/')}/query",
            {
                "objectIds": ",".join(str(object_id) for object_id in batch),
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
            },
            post=True,
        )
        features = response.get("features")
        if not isinstance(features, list):
            raise RuntimeError("ArcGIS response did not contain a GeoJSON features list")  # noqa: TRY004
        if len(features) != len(batch):
            raise RuntimeError(
                f"ArcGIS returned {len(features)} features for a batch of {len(batch)} IDs"
            )

        for feature in features:
            if not isinstance(feature, dict):
                raise RuntimeError("ArcGIS returned an invalid GeoJSON feature")  # noqa: TRY004
            object_id = feature_object_id(feature, object_id_field)
            if object_id not in expected_ids:
                raise RuntimeError(f"ArcGIS returned unexpected object ID {object_id}")
            if object_id in seen_ids:
                raise RuntimeError(f"ArcGIS returned duplicate feature {object_id}")
            seen_ids.add(object_id)
        yield features

    if seen_ids != expected_ids:
        missing_count = len(expected_ids - seen_ids)
        raise RuntimeError(f"ArcGIS export is missing {missing_count} features")
