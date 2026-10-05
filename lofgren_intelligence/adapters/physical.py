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
                content = (f"{tle.name} is predicted to pass over ({lat:.4f}, {lon:.4f}) {len(passes)} time(s) above "
                           f"{self.min_elevation_deg:.0f} deg elevation in the {self.hours:.0f} h from "
                           f"{start:%Y-%m-%d %H:%M} UTC; highest {best.max_elevation_deg:.1f} deg at "
                           f"{best.culmination:%Y-%m-%d %H:%M} UTC.")
            else:
                content = (f"{tle.name} is predicted not to pass above {self.min_elevation_deg:.0f} deg over "
                           f"({lat:.4f}, {lon:.4f}) in the {self.hours:.0f} h from {start:%Y-%m-%d %H:%M} UTC.")
            calc = {"name": f"predicted pass count: {tle.name}",
                    "formula": "count(passes with max elevation >= min_elevation within [start, start + hours])",
                    "inputs": [{"name": "min_elevation", "value": self.min_elevation_deg, "unit": "deg"},
                               {"name": "hours", "value": self.hours, "unit": "h"},
                               {"name": "element_age", "value": round(age_days, 2), "unit": "days"}],
                    "result": float(len(passes)), "unit": "passes"}
            data: dict[str, Any] = {
                "passes": [p.to_dict() for p in passes],
                "propagator": PROPAGATOR,
                # A prediction from public elements, not a provider acquisition schedule:
                # a pass is an opportunity to image, not proof an image was taken.
                "prediction_kind": "approximate propagation (not a provider acquisition schedule)",
                "element_age_days": round(age_days, 2),
                "observed_claims": [{"statement": content, "value": float(len(passes)), "unit": "passes",
                                     "subject": f"passes:{tle.norad_id}:{lat:.3f},{lon:.3f}:{start:%Y-%m-%dT%H}",
                                     "calculation": calc}],
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


COLLECTION_INFO = {
    "sentinel-2-l2a": {"provider": "ESA Copernicus", "license": "Copernicus Sentinel data terms (free, open)",
                       "gsd_m": 10.0},
    "landsat-c2-l2": {"provider": "USGS / NASA", "license": "USGS Landsat (public domain)", "gsd_m": 30.0},
}


def normalize_scene(feature: dict, collection: str) -> dict:
    """One schema for every imagery provider: what, when, where, how clear, how sharp, whose."""
    props = feature.get("properties", {}) or {}
    info = COLLECTION_INFO.get(collection, {})
    bbox = feature.get("bbox")
    return {
        "id": feature.get("id"),
        "collection": collection,
        "provider": info.get("provider", props.get("providers", "unknown")),
        "platform": props.get("platform"),
        "acquired_at": props.get("datetime"),
        "cloud_cover_pct": props.get("eo:cloud_cover"),
        "gsd_m": props.get("gsd", info.get("gsd_m")),
        "footprint_bbox": list(bbox) if bbox else None,
        "epsg": props.get("proj:epsg"),
        "license": info.get("license", props.get("license", "unknown")),
        "access": "open" if collection in COLLECTION_INFO else "check provider terms",
    }


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
            scenes = [normalize_scene(f, collection) for f in payload.get("features", [])]
            src = Source(kind=SourceKind.IMAGERY, title=f"STAC catalog: {collection}", uri=self.endpoint,
                         publisher="Element 84 Earth Search", license=self.license, quality=0.9,
                         independence_group=f"imagery-{collection}")
            content = (f"{len(scenes)} {collection} scene(s) under {self.max_cloud:.0f}% cloud cover cover "
                       f"({lat:.4f}, {lon:.4f}) between {start:%Y-%m-%d} and {end:%Y-%m-%d}.")
            ev = Evidence(
                source_id=src.id, kind=EvidenceKind.DATASET, content=content,
                data={"scenes": scenes, "query": body,
                      "observed_claims": [{
                          "statement": content, "value": float(len(scenes)), "unit": "scenes",
                          "subject": f"scenes:{collection}:{lat:.3f},{lon:.3f}:{end:%Y-%m-%d}",
                          "calculation": {"name": f"scene count: {collection}",
                                          "formula": "count(scenes intersecting point with cloud_cover < max_cloud in window)",
                                          "inputs": [{"name": "max_cloud", "value": self.max_cloud, "unit": "%"},
                                                     {"name": "days", "value": float(self.days), "unit": "days"}],
                                          "result": float(len(scenes)), "unit": "scenes"}}]},
                observed_at=end.isoformat(), location=Location(lat, lon, contract.location.get("name")),
                valid_from=start.isoformat(), valid_to=end.isoformat(),
                transformations=["STAC search; scene metadata only, pixels not downloaded"],
            )
            result.items.append((src, ev))
        return result


# Unit normalization: every reading is stored in one canonical unit per quantity.
UNIT_ALIASES: dict[str, tuple[str, Callable[[float], float]]] = {
    "%": ("%", lambda v: v), "percent": ("%", lambda v: v), "pct": ("%", lambda v: v),
    "c": ("°C", lambda v: v), "°c": ("°C", lambda v: v), "degc": ("°C", lambda v: v), "celsius": ("°C", lambda v: v),
    "f": ("°C", lambda v: (v - 32) * 5 / 9), "°f": ("°C", lambda v: (v - 32) * 5 / 9),
    "degf": ("°C", lambda v: (v - 32) * 5 / 9), "fahrenheit": ("°C", lambda v: (v - 32) * 5 / 9),
    "k": ("°C", lambda v: v - 273.15),
    "pa": ("kPa", lambda v: v / 1000), "kpa": ("kPa", lambda v: v), "hpa": ("kPa", lambda v: v / 10),
    "mm": ("mm", lambda v: v), "cm": ("mm", lambda v: v * 10), "in": ("mm", lambda v: v * 25.4),
    "w": ("W", lambda v: v), "kw": ("W", lambda v: v * 1000), "kwh": ("kWh", lambda v: v), "wh": ("kWh", lambda v: v / 1000),
    "ppm": ("ppm", lambda v: v), "ug/m3": ("µg/m³", lambda v: v), "µg/m³": ("µg/m³", lambda v: v),
    "m/s": ("m/s", lambda v: v), "km/h": ("m/s", lambda v: v / 3.6), "mph": ("m/s", lambda v: v * 0.44704),
    "v": ("V", lambda v: v), "a": ("A", lambda v: v), "lux": ("lux", lambda v: v),
}


def normalize_unit(unit: str) -> tuple[str, Callable[[float], float]] | None:
    return UNIT_ALIASES.get(unit.strip().lower()) if unit and unit.strip() else None


def _parse_time(stamp: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class SensorAdapter(Adapter):
    """Reads IoT / sensor readings the user owns or is authorized to read. Read-only.

    CSV columns: timestamp, sensor_id, metric, value, unit
    optional:    lat, lon, calibrated_at, uncertainty

    Every series is normalized to a canonical unit. A series with a missing or
    unknown unit, or mixing quantities that cannot be converted, is rejected
    with a reason rather than guessed. Health (sampling gaps, calibration age)
    is reported with the evidence. Device control is not possible here; it
    belongs to V4 behind the authority engine.
    """

    id = "sensors"
    capabilities = frozenset({"sensor"})
    license = "user-owned device data"
    description = "Summarizes readings from the user's own sensors (read-only, unit-normalized)."
    calibration_max_age_days = 365

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
            series: dict[tuple[str, str], list[dict]] = defaultdict(list)
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    if not row.get("sensor_id") or not row.get("metric"):
                        continue
                    series[(row["sensor_id"], row["metric"])].append(row)
            src = Source(kind=SourceKind.SENSOR, title=f"Sensor log: {path.name}", uri=path.resolve().as_uri(),
                         publisher=self.owner, license=self.license, quality=0.85,
                         independence_group=f"sensor-{path.name}")
            for (sensor, metric), rows in sorted(series.items()):
                item = self._summarize(sensor, metric, rows, src, result)
                if item:
                    result.items.append(item)
        return result

    def _summarize(self, sensor: str, metric: str, rows: list[dict], src: Source, result: GatherResult):
        readings: list[tuple[datetime, float]] = []
        canonical: set[str] = set()
        rejected = 0
        for row in rows:
            norm = normalize_unit(row.get("unit", ""))
            t = _parse_time(row.get("timestamp", ""))
            try:
                raw = float(row["value"])
            except (KeyError, ValueError, TypeError):
                rejected += 1
                continue
            if norm is None or t is None:
                rejected += 1
                continue
            canonical.add(norm[0])
            readings.append((t, norm[1](raw)))
        if len(canonical) > 1:
            result.notes.append(f"sensors: {sensor}/{metric} mixes quantities {sorted(canonical)}; series rejected")
            return None
        if not readings:
            result.notes.append(f"sensors: {sensor}/{metric} has no readings with a known unit and valid "
                                f"timestamp ({rejected} rejected); series not used")
            return None
        unit = canonical.pop()
        readings.sort()
        values = [v for _, v in readings]
        n, mean = len(values), statistics.fmean(values)
        first_t, last_t = readings[0][0], readings[-1][0]
        gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(readings, readings[1:])]
        health: list[str] = []
        if rejected:
            health.append(f"{rejected} reading(s) rejected (unknown unit, bad value or timestamp)")
        if len(gaps) >= 2:
            typical = statistics.median(gaps)
            if typical > 0 and max(gaps) > 3 * typical:
                health.append(f"irregular sampling: longest gap {max(gaps) / 3600:.1f} h vs typical {typical / 3600:.1f} h")
        cal = next((r.get("calibrated_at") for r in reversed(rows) if r.get("calibrated_at")), None)
        cal_t = _parse_time(cal) if cal else None
        if cal_t is None:
            health.append("calibration date unknown")
        elif (last_t - cal_t).days > self.calibration_max_age_days:
            health.append(f"calibration older than {self.calibration_max_age_days} days")
        uncertainty = next((r.get("uncertainty") for r in rows if r.get("uncertainty")), None)
        loc_row = next((r for r in rows if r.get("lat") and r.get("lon")), None)
        location = Location(float(loc_row["lat"]), float(loc_row["lon"])) if loc_row else None
        f0, f1 = first_t.isoformat(), last_t.isoformat()
        content = (f"{metric} at sensor {sensor} averaged {mean:.2f} {unit} over {n} readings from {f0} to {f1} "
                   f"(min {min(values):.2f}, max {max(values):.2f}); it moved from {values[0]:.2f} to {values[-1]:.2f}.")
        calc = {"name": f"mean {metric} at {sensor}", "formula": "sum(values) / n",
                "inputs": [{"name": "n", "value": float(n), "unit": "readings"},
                           {"name": "sum", "value": round(sum(values), 6), "unit": unit}],
                "result": round(mean, 6), "unit": unit}
        ev = Evidence(
            source_id=src.id, kind=EvidenceKind.TIME_SERIES, content=content,
            data={"sensor_id": sensor, "owner": self.owner, "authorization": "read-only, confirmed by user",
                  "metric": metric, "unit": unit, "count": n, "mean": mean, "min": min(values), "max": max(values),
                  "first": values[0], "last": values[-1], "uncertainty": uncertainty,
                  "calibrated_at": cal, "health": health or ["ok"],
                  "observed_claims": [{"statement": content, "value": mean, "unit": unit,
                                       "subject": f"sensor:{sensor}:{metric}:{f0}/{f1}", "calculation": calc}]},
            observed_at=f1, valid_from=f0, valid_to=f1, location=location,
            transformations=[f"normalized to {unit}", "aggregated: count, mean, min, max, first, last"],
        )
        return src, ev
