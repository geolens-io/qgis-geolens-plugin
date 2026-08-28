"""Small, dependency-free client for the GeoLens HTTP API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


class GeoLensError(RuntimeError):
    pass


def _plugin_version() -> str:
    """Version from metadata.txt, the same single source package_plugin.py reads."""
    try:
        metadata = Path(__file__).with_name("metadata.txt").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    for line in metadata.splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip() or "unknown"
    return "unknown"


# Sent on every request. Without it, urllib announces itself as
# "Python-urllib/x.y", which common WAF/bot-mitigation rules block outright
# with 403 before the request reaches GeoLens; a named agent also lets server
# operators recognize plugin traffic in their logs.
USER_AGENT = f"qgis-geolens-plugin/{_plugin_version()}"


@dataclass(frozen=True)
class Dataset:
    id: str
    title: str
    description: str
    record_type: str | None
    geometry_type: str | None
    feature_count: int | None
    band_count: int | None
    bbox: tuple[float, float, float, float] | None

    @property
    def is_raster(self) -> bool:
        return "raster" in (self.record_type or "").lower() or bool(self.band_count)


class GeoLensClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        opener: Callable = urlopen,
    ):
        raw = base_url.strip()
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = "https://" + raw
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("GeoLens URL must be an HTTP(S) server URL")
        self.base_url = raw.rstrip("/")
        self.api_key = api_key.strip()
        self._opener = opener

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _request(self, path_or_url: str, method: str = "GET", body: Any = None) -> Any:
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else self.base_url + path_or_url
        )
        headers = dict(self.headers)
        headers["User-Agent"] = USER_AGENT
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=30) as response:
                payload = response.read()
                return json.loads(payload.decode("utf-8")) if payload else None
        except HTTPError as error:
            detail = ""
            try:
                payload = json.loads(error.read().decode("utf-8"))
                detail = payload.get("detail") or payload.get("title") or ""
            except (ValueError, AttributeError, OSError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise GeoLensError(
                f"GeoLens request failed (HTTP {error.code}){suffix}"
            ) from error
        except OSError as error:
            raise GeoLensError(f"Could not reach GeoLens: {error}") from error

    def search(self, query: str = "", limit: int = 50) -> list[Dataset]:
        params = {"limit": max(1, int(limit))}
        if query.strip():
            params["q"] = query.strip()
        body = self._request("/api/search/datasets/?" + urlencode(params))
        features = body.get("features") if isinstance(body, dict) else None
        if not isinstance(features, list):
            raise GeoLensError("GeoLens search returned no features")
        return [dataset for feature in features if (dataset := parse_dataset(feature))]

    def capabilities(self) -> dict[str, bool]:
        try:
            body = self._request("/api/settings/feature-flags/")
        except GeoLensError:
            return {"dataset_editing": False}
        return {"dataset_editing": body.get("enable_dataset_editing") is True}

    def tile_source(self, dataset_id: str) -> dict[str, Any]:
        body = self._request(f"/api/tiles/token/{quote(dataset_id, safe='')}/")
        if not isinstance(body, dict):
            raise GeoLensError("GeoLens tile response was malformed")
        if body.get("kind") == "raster":
            tile_url = body.get("tile_url")
            if not tile_url:
                raise GeoLensError("GeoLens raster response contained no tile URL")
            return {**body, "url": self.base_url + tile_url}
        required = ("sig", "exp", "scope")
        if not all(body.get(key) is not None for key in required):
            raise GeoLensError("GeoLens vector tile response was malformed")
        table = "data." + str(body["scope"])
        query = urlencode(
            {"sig": body["sig"], "exp": body["exp"], "scope": body["scope"]}
        )
        return {
            **body,
            "url": f"{self.base_url}/api/tiles/{table}/{{z}}/{{x}}/{{y}}.pbf?{query}",
            "source_layer": table,
        }

    def features(
        self, dataset_id: str, limit: int = 10_000, bbox: Iterable[float] | None = None
    ) -> dict[str, Any]:
        requested = max(1, int(limit))
        params: dict[str, Any] = {"limit": min(requested, 10_000)}
        if bbox is not None:
            values = list(bbox)
            if len(values) != 4:
                raise ValueError("bbox must contain four coordinates")
            params["bbox"] = ",".join(str(value) for value in values)
        path = (
            f"/api/collections/{quote(dataset_id, safe='')}/items?{urlencode(params)}"
        )
        output: list[dict[str, Any]] = []
        first: dict[str, Any] = {}
        seen: set[str] = set()
        url = self.base_url + path
        while url and url not in seen and len(output) < requested:
            seen.add(url)
            page = self._request(url)
            if not isinstance(page, dict) or not isinstance(page.get("features"), list):
                raise GeoLensError("GeoLens items response contained no features")
            if not first:
                first = page
            output.extend(page["features"][: requested - len(output)])
            next_link = next(
                (
                    link.get("href")
                    for link in page.get("links", [])
                    if isinstance(link, dict) and link.get("rel") == "next"
                ),
                None,
            )
            if next_link:
                resolved = urlparse(urljoin(url, next_link))
                base = urlparse(self.base_url)
                url = urlunparse(
                    (base.scheme, base.netloc, resolved.path, "", resolved.query, "")
                )
            else:
                url = ""
        return {**first, "type": "FeatureCollection", "features": output}

    def create_feature(self, dataset_id: str, feature: dict[str, Any]) -> int | None:
        body = self._request(
            f"/api/datasets/{quote(dataset_id, safe='')}/features/", "POST", feature
        )
        value = (body or {}).get("id", (body or {}).get("gid"))
        return value if isinstance(value, int) else None

    def update_feature(
        self, dataset_id: str, gid: int, feature: dict[str, Any]
    ) -> None:
        self._request(
            f"/api/datasets/{quote(dataset_id, safe='')}/features/{gid}", "PUT", feature
        )

    def delete_feature(self, dataset_id: str, gid: int) -> None:
        self._request(
            f"/api/datasets/{quote(dataset_id, safe='')}/features/{gid}", "DELETE"
        )

    def metadata_url(self, dataset_id: str) -> str:
        return f"{self.base_url}/datasets/{quote(dataset_id, safe='')}"


def parse_dataset(feature: Any) -> Dataset | None:
    if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
        return None
    props = (
        feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    )
    return Dataset(
        id=feature["id"],
        title=props.get("title") or feature["id"],
        description=props.get("description") or "",
        record_type=props.get("record_type"),
        geometry_type=props.get("geometry_type"),
        feature_count=props.get("feature_count"),
        band_count=props.get("band_count"),
        bbox=_geometry_bbox(feature.get("geometry")),
    )


def _geometry_bbox(geometry: Any) -> tuple[float, float, float, float] | None:
    values: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            values.append((float(node[0]), float(node[1])))
        elif isinstance(node, list):
            for child in node:
                walk(child)

    if isinstance(geometry, dict):
        walk(geometry.get("coordinates"))
    if not values:
        return None
    xs, ys = zip(*values)
    return min(xs), min(ys), max(xs), max(ys)
