"""Orbit propagation and pass prediction.

Model: two-body Keplerian motion with secular J2 perturbations (nodal
regression, apsidal precession, mean-motion correction). This is accurate to
roughly tens of kilometres within a day of a fresh TLE, which is enough to
answer "which imaging satellites pass over this place, and when?".

Precision work (for example, tasking a commercial capture to the minute)
should swap in the full SGP4 model; the interface here is designed so a
propagator can be replaced without touching callers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .tle import TLE

MU = 398600.4418  # km^3 / s^2
RE = 6378.137  # km, WGS84 equatorial radius
J2 = 1.08262668e-3
WGS84_E2 = 6.69437999014e-3
PROPAGATOR = "two-body + secular J2 (approximate)"


def julian_date(t: datetime) -> float:
    t = t.astimezone(timezone.utc)
    return t.timestamp() / 86400.0 + 2440587.5


def gmst_rad(t: datetime) -> float:
    """Greenwich mean sidereal time (IAU 1982, low-order form)."""
    d = julian_date(t) - 2451545.0
    deg = 280.46061837 + 360.98564736629 * d
    return math.radians(deg % 360.0)


def _solve_kepler(m: float, e: float) -> float:
    ecc_anom = m if e < 0.8 else math.pi
    for _ in range(50):
        delta = (ecc_anom - e * math.sin(ecc_anom) - m) / (1 - e * math.cos(ecc_anom))
        ecc_anom -= delta
        if abs(delta) < 1e-12:
            break
    return ecc_anom


def nodal_precession_deg_per_day(tle: TLE) -> float:
    """Secular drift of the ascending node from Earth's oblateness (J2).

    A sun-synchronous imaging orbit drifts about +0.9856 deg/day, keeping the
    same local solar time on every pass.
    """
    i = math.radians(tle.inclination_deg)
    n = tle.mean_motion_rev_per_day * 2 * math.pi / 86400.0
    a = (MU / n**2) ** (1 / 3)
    p = a * (1 - tle.eccentricity**2)
    return math.degrees(-1.5 * J2 * (RE / p) ** 2 * n * math.cos(i)) * 86400


def position_eci(tle: TLE, t: datetime) -> tuple[float, float, float]:
    """Earth-centred inertial position (km) at time t."""
    dt = (t - tle.epoch).total_seconds()
    i = math.radians(tle.inclination_deg)
    e = tle.eccentricity
    n = tle.mean_motion_rev_per_day * 2 * math.pi / 86400.0
    a = (MU / n**2) ** (1 / 3)
    p = a * (1 - e**2)
    k = 1.5 * J2 * (RE / p) ** 2
    raan = math.radians(tle.raan_deg) - k * n * math.cos(i) * dt
    argp = math.radians(tle.arg_perigee_deg) + 0.5 * k * n * (5 * math.cos(i) ** 2 - 1) * dt
    m = math.radians(tle.mean_anomaly_deg) + (n + 0.5 * k * n * math.sqrt(1 - e**2) * (3 * math.cos(i) ** 2 - 1)) * dt
    m %= 2 * math.pi

    ecc_anom = _solve_kepler(m, e)
    nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(ecc_anom / 2), math.sqrt(1 - e) * math.cos(ecc_anom / 2))
    r = a * (1 - e * math.cos(ecc_anom))
    xp, yp = r * math.cos(nu), r * math.sin(nu)

    cO, sO = math.cos(raan), math.sin(raan)
    cw, sw = math.cos(argp), math.sin(argp)
    ci, si = math.cos(i), math.sin(i)
    x = (cO * cw - sO * sw * ci) * xp + (-cO * sw - sO * cw * ci) * yp
    y = (sO * cw + cO * sw * ci) * xp + (-sO * sw + cO * cw * ci) * yp
    z = (sw * si) * xp + (cw * si) * yp
    return x, y, z


def eci_to_ecef(r: tuple[float, float, float], t: datetime) -> tuple[float, float, float]:
    th = gmst_rad(t)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r
    return c * x + s * y, -s * x + c * y, z


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = RE / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    return (
        (n + alt_km) * math.cos(lat) * math.cos(lon),
        (n + alt_km) * math.cos(lat) * math.sin(lon),
        (n * (1 - WGS84_E2) + alt_km) * math.sin(lat),
    )


def ecef_to_geodetic(r: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = r
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    alt = 0.0
    for _ in range(10):
        n = RE / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


def subpoint(tle: TLE, t: datetime) -> tuple[float, float, float]:
    """Latitude, longitude (deg) and altitude (km) directly beneath the satellite."""
    return ecef_to_geodetic(eci_to_ecef(position_eci(tle, t), t))


def look_angles(tle: TLE, t: datetime, lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> tuple[float, float, float]:
    """Elevation (deg), azimuth (deg) and range (km) of the satellite from an observer."""
    sat = eci_to_ecef(position_eci(tle, t), t)
    obs = geodetic_to_ecef(lat_deg, lon_deg, alt_km)
    rx, ry, rz = sat[0] - obs[0], sat[1] - obs[1], sat[2] - obs[2]
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    south = math.sin(lat) * math.cos(lon) * rx + math.sin(lat) * math.sin(lon) * ry - math.cos(lat) * rz
    east = -math.sin(lon) * rx + math.cos(lon) * ry
    up = math.cos(lat) * math.cos(lon) * rx + math.cos(lat) * math.sin(lon) * ry + math.sin(lat) * rz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    el = math.degrees(math.asin(up / rng))
    az = math.degrees(math.atan2(east, -south)) % 360.0
    return el, az, rng


@dataclass
class Pass:
    name: str
    norad_id: int
    rise: datetime
    culmination: datetime
    set: datetime
    max_elevation_deg: float
    rise_azimuth_deg: float
    set_azimuth_deg: float

    @property
    def duration_s(self) -> float:
        return (self.set - self.rise).total_seconds()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "norad_id": self.norad_id,
            "rise": self.rise.replace(microsecond=0).isoformat(),
            "culmination": self.culmination.replace(microsecond=0).isoformat(),
            "set": self.set.replace(microsecond=0).isoformat(),
            "max_elevation_deg": round(self.max_elevation_deg, 1),
            "rise_azimuth_deg": round(self.rise_azimuth_deg, 1),
            "set_azimuth_deg": round(self.set_azimuth_deg, 1),
            "duration_s": round(self.duration_s),
        }


def find_passes(
    tle: TLE,
    lat_deg: float,
    lon_deg: float,
    start: datetime,
    hours: float = 24.0,
    min_elevation_deg: float = 10.0,
    step_s: float = 30.0,
) -> list[Pass]:
    """Passes above `min_elevation_deg` over an observer in [start, start+hours]."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=hours)
    el_at = lambda t: look_angles(tle, t, lat_deg, lon_deg)[0]  # noqa: E731

    def crossing(t0: datetime, t1: datetime, rising: bool) -> datetime:
        for _ in range(20):  # bisection to ~1 s
            mid = t0 + (t1 - t0) / 2
            above = el_at(mid) >= min_elevation_deg
            if above == rising:
                t1 = mid
            else:
                t0 = mid
        return t1 if rising else t0

    passes: list[Pass] = []
    t = start
    prev_above = el_at(t) >= min_elevation_deg
    rise: datetime | None = start if prev_above else None
    best_t, best_el = t, el_at(t)
    step = timedelta(seconds=step_s)
    while t < end:
        t_next = min(t + step, end)
        el = el_at(t_next)
        above = el >= min_elevation_deg
        if above and not prev_above:
            rise = crossing(t, t_next, rising=True)
            best_t, best_el = t_next, el
        elif above and el > best_el:
            best_t, best_el = t_next, el
        if prev_above and not above and rise is not None:
            set_t = crossing(t, t_next, rising=False)
            passes.append(Pass(
                tle.name, tle.norad_id, rise, best_t, set_t, best_el,
                look_angles(tle, rise, lat_deg, lon_deg)[1],
                look_angles(tle, set_t, lat_deg, lon_deg)[1],
            ))
            rise = None
        prev_above = above
        t = t_next
    return passes
