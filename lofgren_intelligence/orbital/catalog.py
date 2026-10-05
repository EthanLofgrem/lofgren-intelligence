"""Known Earth-observation satellites and public orbital-data retrieval.

The catalog lists satellites whose imagery is openly published, so a pass
over a location can be followed by a lawful request for that imagery.
Orbital elements are fetched from CelesTrak's public GP service, which
permits redistribution. Nothing here commands or connects to a spacecraft.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .tle import TLE, parse_tle_text

CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"


@dataclass(frozen=True)
class ImagingSatellite:
    name: str
    norad_id: int
    operator: str
    sensor: str
    data_access: str  # where the imagery can be obtained lawfully
    open_data: bool


IMAGING_SATELLITES: tuple[ImagingSatellite, ...] = (
    ImagingSatellite("Sentinel-2A", 40697, "ESA / Copernicus", "Multispectral optical (10 m)", "Copernicus Data Space; AWS Earth Search (sentinel-2-l2a)", True),
    ImagingSatellite("Sentinel-2B", 42063, "ESA / Copernicus", "Multispectral optical (10 m)", "Copernicus Data Space; AWS Earth Search (sentinel-2-l2a)", True),
    ImagingSatellite("Sentinel-1A", 39634, "ESA / Copernicus", "C-band radar (SAR), sees through cloud", "Copernicus Data Space", True),
    ImagingSatellite("Landsat 8", 39084, "USGS / NASA", "Optical + thermal (30 m)", "USGS EarthExplorer; AWS Earth Search (landsat-c2-l2)", True),
    ImagingSatellite("Landsat 9", 49260, "USGS / NASA", "Optical + thermal (30 m)", "USGS EarthExplorer; AWS Earth Search (landsat-c2-l2)", True),
    ImagingSatellite("Terra", 25994, "NASA", "MODIS / ASTER (250 m - 15 m)", "NASA Earthdata", True),
    ImagingSatellite("Aqua", 27424, "NASA", "MODIS (250 m), fire detection", "NASA Earthdata / FIRMS", True),
    ImagingSatellite("Suomi NPP", 37849, "NASA / NOAA", "VIIRS (375 m), fire and night lights", "NASA Earthdata / FIRMS", True),
    ImagingSatellite("NOAA-20", 43013, "NOAA", "VIIRS (375 m), weather and fire", "NOAA / NASA FIRMS", True),
)


def by_norad(norad_id: int) -> ImagingSatellite | None:
    return next((s for s in IMAGING_SATELLITES if s.norad_id == norad_id), None)


def fetch_tles(norad_ids: list[int], timeout: float = 15.0) -> list[TLE]:
    """Current elements from CelesTrak. Raises on network failure."""
    out: list[TLE] = []
    for norad in norad_ids:
        req = urllib.request.Request(CELESTRAK_GP.format(norad=norad),
                                     headers={"User-Agent": "lofgren-intelligence/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed public host
            out.extend(parse_tle_text(resp.read().decode("utf-8", "replace")))
    return out


def load_tles(path: str | Path) -> list[TLE]:
    return parse_tle_text(Path(path).read_text())
