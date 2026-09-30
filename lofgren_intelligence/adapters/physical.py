"""Physical-world adapters: orbital passes, satellite imagery catalogs, IoT sensors.

Legal boundary, enforced by design:
  * orbital data comes from public element sets; no spacecraft is contacted
  * imagery comes from open archives or licensed providers, read-only
  * sensors must be owned by, or explicitly authorized for, the user; read-only
"""

from __future__ import annotations

import csv
import json
import statistics
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..evidence.types import Evidence, EvidenceKind, Location, Source, SourceKind
from ..orbital.catalog import CELESTRAK_GP, IMAGING_SATELLITES, by_norad, fetch_tles, load_tles
from ..orbital.propagate import PROPAGATOR, find_passes
from ..orbital.tle import TLE
from .base import Adapter, GatherResult

if TYPE_CHECKING:
    from ..intent.compiler import OutcomeContract, Question


def _coords(contract: "OutcomeContract") -> tuple[float, float] | None:
    loc = contract.location or {}
    if loc.get("lat") is None or loc.get("lon") is None:
        return None
    return float(loc["lat"]), float(loc["lon"])


class OrbitalPassAdapter(Adapter):
    """Which open-data imaging satellites pass over the location, and when."""

    id = "orbital_passes"
    capabilities = frozenset({"orbital_passes"})
    license = "CelesTrak GP data (redistribution permitted)"
    description = "Predicts imaging-satellite passes over a location from public orbital elements."

    def __init__(self, tles: list[TLE] | None = None, tle_path: str | Path | None = None,
                 fetch: bool = False, hours: float = 24.0, start: datetime | None = None,
                 min_elevation_deg: float = 30.0) -> None:
        self._tles = list(tles or [])
        self.tle_path = tle_path
        self.fetch = fetch
        self.hours = hours
        self.start = start
        self.min_elevation_deg = min_elevation_deg
        self._fetch_error: str | None = None

    def tles(self) -> list[TLE]:
        if not self._tles and self.tle_path:
            self._tles = load_tles(self.tle_path)
        if not self._tles and self.fetch:
            try:
                self._tles = fetch_tles([s.norad_id for s in IMAGING_SATELLITES])
            except Exception as exc:  # network failure is a gap
                self._fetch_error = str(exc)
        return self._tles

    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        coords = _coords(contract)
        if coords is None:
            result.notes.append("orbital_passes: no coordinates in the objective; add 'lat, lon' to predict passes")
            return result
        tles = self.tles()
        if not tles:
            why = f" ({self._fetch_error})" if self._fetch_error else ""
            result.notes.append(f"orbital_passes: no orbital elements available{why}")
            return result
        lat, lon = coords
        start = self.start or datetime.now(timezone.utc)
        for tle in tles:
            passes = find_passes(tle, lat, lon, start, self.hours, self.min_elevation_deg)
            sat = by_norad(tle.norad_id)
            age_days = abs((start - tle.epoch).total_seconds()) / 86400
            src = Source(
                kind=SourceKind.ORBITAL,
                title=f"Orbital elements: {tle.name} (NORAD {tle.norad_id})",
                uri=CELESTRAK_GP.format(norad=tle.norad_id),
                publisher="CelesTrak",
                published_at=tle.epoch.isoformat(),
                license=self.license,
                quality=0.9 if age_days <= 3 else 0.6,
                independence_group=f"orbit-{tle.norad_id}",
            )
            if passes:
                best = max(passes, key=lambda p: p.max_elevation_deg)
                content = (f"{tle.name} passes over ({lat:.4f}, {lon:.4f}) {len(passes)} time(s) above "
                           f"{self.min_elevation_deg:.0f} deg elevation in the {self.hours:.0f} h from "
                           f"{start:%Y-%m-%d %H:%M} UTC; highest {best.max_elevation_deg:.1f} deg at "
                           f"{best.culmination:%Y-%m-%d %H:%M} UTC.")
            else:
                content = (f"{tle.name} does not pass above {self.min_elevation_deg:.0f} deg over "
                           f"({lat:.4f}, {lon:.4f}) in the {self.hours:.0f} h from {start:%Y-%m-%d %H:%M} UTC.")
            data: dict[str, Any] = {
                "passes": [p.to_dict() for p in passes],
                "propagator": PROPAGATOR,
                "element_age_days": round(age_days, 2),
                "observed_claims": [{"statement": content, "value": float(len(passes)), "unit": "passes",
                                     "subject": f"passes:{tle.norad_id}:{lat:.3f},{lon:.3f}:{start:%Y-%m-%dT%H}"}],
            }
            if sat:
                data["sensor"] = sat.sensor
                data["data_access"] = sat.data_access
            ev = Evidence(
                source_id=src.id, kind=EvidenceKind.CALCULATION, content=content, data=data,
                observed_at=start.isoformat(), location=Location(lat, lon, contract.location.get("name")),
                valid_from=start.isoformat(), valid_to=(start + timedelta(hours=self.hours)).isoformat(),
                transformations=[f"propagated with {PROPAGATOR} from epoch {tle.epoch.isoformat()}"],
            )
            result.items.append((src, ev))
        return result


FetchJSON = Callable[[str, dict], dict]


def _post_json(url: str, body: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "lofgren-intelligence/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed public host
        return json.loads(resp.read().decode())


class ImageryCatalogAdapter(Adapter):
    """Searches open satellite imagery catalogs (STAC) for scenes over a location."""

    id = "imagery_catalog"
    capabilities = frozenset({"imagery_catalog"})
    license = "Copernicus Sentinel data terms / USGS Landsat public domain"
    description = "Finds open Sentinel-2 and Landsat scenes over a location via Earth Search (STAC)."
    endpoint = "https://earth-search.aws.element84.com/v1/search"

    def __init__(self, collections: tuple[str, ...] = ("sentinel-2-l2a", "landsat-c2-l2"),
                 days: int = 30, max_cloud: float = 20.0, fetch_json: FetchJSON | None = None,
                 now: datetime | None = None) -> None:
        self.collections = collections
        self.days = days
        self.max_cloud = max_cloud
        self.fetch_json = fetch_json or _post_json
        self.now = now

    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        coords = _coords(contract)
        if coords is None:
            result.notes.append("imagery_catalog: no coordinates in the objective; add 'lat, lon' to search imagery")
            return result
        lat, lon = coords
        end = self.now or datetime.now(timezone.utc)
        start = end - timedelta(days=self.days)
        window = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
        for collection in self.collections:
            body = {
                "collections": [collection],
                "intersects": {"type": "Point", "coordinates": [lon, lat]},
                "datetime": window,
                "limit": 20,
                "query": {"eo:cloud_cover": {"lt": self.max_cloud}},
            }
            try:
                payload = self.fetch_json(self.endpoint, body)
            except Exception as exc:
                result.notes.append(f"imagery_catalog: {collection} search unavailable ({exc})")
                continue
            scenes = [
                {
                    "id": f.get("id"),
                    "datetime": f.get("properties", {}).get("datetime"),
                    "cloud_cover": f.get("properties", {}).get("eo:cloud_cover"),
                    "platform": f.get("properties", {}).get("platform"),
                }
                for f in payload.get("features", [])
            ]
            src = Source(kind=SourceKind.IMAGERY, title=f"STAC catalog: {collection}", uri=self.endpoint,
                         publisher="Element 84 Earth Search", license=self.license, quality=0.9,
                         independence_group=f"imagery-{collection}")
            content = (f"{len(scenes)} {collection} scene(s) under {self.max_cloud:.0f}% cloud cover cover "
                       f"({lat:.4f}, {lon:.4f}) between {start:%Y-%m-%d} and {end:%Y-%m-%d}.")
            ev = Evidence(
                source_id=src.id, kind=EvidenceKind.DATASET, content=content,
                data={"scenes": scenes, "query": body,
                      "observed_claims": [{"statement": content, "value": float(len(scenes)), "unit": "scenes",
                                          "subject": f"scenes:{collection}:{lat:.3f},{lon:.3f}:{end:%Y-%m-%d}"}]},
                observed_at=end.isoformat(), location=Location(lat, lon, contract.location.get("name")),
                valid_from=start.isoformat(), valid_to=end.isoformat(),
                transformations=["STAC search; scene metadata only, pixels not downloaded"],
            )
            result.items.append((src, ev))
        return result


class SensorAdapter(Adapter):
    """Reads IoT / sensor readings the user owns or is authorized to read.

    CSV columns: timestamp, sensor_id, metric, value, unit [, lat, lon]
    """

    id = "sensors"
    capabilities = frozenset({"sensor"})
    license = "user-owned device data"
    description = "Summarizes readings from the user's own sensors (read-only)."

    def __init__(self, csv_paths: list[str | Path], authorized: bool = False, owner: str = "user") -> None:
        self.csv_paths = [Path(p) for p in csv_paths]
        self.authorized = authorized
        self.owner = owner

    def available(self) -> bool:
        return bool(self.csv_paths)

    def gather(self, question: "Question", contract: "OutcomeContract") -> GatherResult:
        result = GatherResult()
        if not self.authorized:
            result.notes.append("sensors: readings not used; the user has not confirmed they own or are "
                                "authorized to read these devices")
            return result
        for path in self.csv_paths:
            series: dict[tuple[str, str], list[tuple[str, float, str]]] = defaultdict(list)
            coords: dict[str, tuple[float, float]] = {}
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        key = (row["sensor_id"], row["metric"])
                        series[key].append((row["timestamp"], float(row["value"]), row.get("unit", "")))
                        if row.get("lat") and row.get("lon"):
                            coords[row["sensor_id"]] = (float(row["lat"]), float(row["lon"]))
                    except (KeyError, ValueError):
                        continue
            src = Source(kind=SourceKind.SENSOR, title=f"Sensor log: {path.name}", uri=path.resolve().as_uri(),
                         publisher=self.owner, license=self.license, quality=0.85,
                         independence_group=f"sensor-{path.name}")
            for (sensor, metric), rows in sorted(series.items()):
                rows.sort(key=lambda r: r[0])
                values = [v for _, v, _ in rows]
                unit = rows[0][2]
                mean = statistics.fmean(values)
                first_t, last_t = rows[0][0], rows[-1][0]
                content = (f"{metric} at sensor {sensor} averaged {mean:.2f} {unit} over {len(values)} readings "
                           f"from {first_t} to {last_t} (min {min(values):.2f}, max {max(values):.2f}); "
                           f"it moved from {values[0]:.2f} to {values[-1]:.2f}.")
                lat_lon = coords.get(sensor)
                ev = Evidence(
                    source_id=src.id, kind=EvidenceKind.TIME_SERIES, content=content,
                    data={"sensor_id": sensor, "metric": metric, "unit": unit, "count": len(values),
                          "mean": mean, "min": min(values), "max": max(values),
                          "first": values[0], "last": values[-1],
                          "observed_claims": [{"statement": content, "value": mean, "unit": unit,
                                              "subject": f"sensor:{sensor}:{metric}:{first_t}/{last_t}"}]},
                    observed_at=last_t, valid_from=first_t, valid_to=last_t,
                    location=Location(*lat_lon) if lat_lon else None,
                    transformations=["aggregated: count, mean, min, max, first, last"],
                )
                result.items.append((src, ev))
        return result
