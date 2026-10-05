"""Two-line element sets (TLE): parsing, validation and formatting.

Orbital elements for tracked objects are published openly (for example by
CelesTrak). This module only reads public orbital data; it never talks to a
spacecraft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def tle_checksum(line: str) -> int:
    """Modulo-10 checksum over the first 68 columns ('-' counts as 1)."""
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


@dataclass(frozen=True)
class TLE:
    name: str
    norad_id: int
    epoch: datetime
    inclination_deg: float
    raan_deg: float
    eccentricity: float
    arg_perigee_deg: float
    mean_anomaly_deg: float
    mean_motion_rev_per_day: float
    bstar: float = 0.0

    @property
    def period_minutes(self) -> float:
        return 1440.0 / self.mean_motion_rev_per_day


class TLEError(ValueError):
    pass


def _epoch(field: str) -> datetime:
    yy = int(field[:2])
    year = 2000 + yy if yy < 57 else 1900 + yy
    day = float(field[2:])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


def _implied_decimal(field: str) -> float:
    """Parse TLE 'implied decimal point' exponent fields such as ' 30306-3'."""
    s = field.strip()
    if not s or s in ("00000-0", "00000+0", "+00000-0", "-00000-0"):
        return 0.0
    sign = -1.0 if s[0] == "-" else 1.0
    s = s.lstrip("+-")
    mantissa, exp = s[:-2], s[-2:]
    return sign * float(f"0.{mantissa}") * 10 ** int(exp)


def parse_tle(line1: str, line2: str, name: str = "", strict: bool = True) -> TLE:
    line1, line2 = line1.rstrip(), line2.rstrip()
    if len(line1) < 69 or len(line2) < 69 or line1[0] != "1" or line2[0] != "2":
        raise TLEError("not a valid two-line element set")
    if strict:
        for line in (line1, line2):
            if tle_checksum(line) != int(line[68]):
                raise TLEError(f"checksum mismatch on line {line[0]}")
    if line1[2:7] != line2[2:7]:
        raise TLEError("catalog numbers on line 1 and line 2 differ")
    return TLE(
        name=name.strip() or f"NORAD {int(line1[2:7])}",
        norad_id=int(line1[2:7]),
        epoch=_epoch(line1[18:32]),
        bstar=_implied_decimal(line1[53:61]),
        inclination_deg=float(line2[8:16]),
        raan_deg=float(line2[17:25]),
        eccentricity=float("0." + line2[26:33].strip()),
        arg_perigee_deg=float(line2[34:42]),
        mean_anomaly_deg=float(line2[43:51]),
        mean_motion_rev_per_day=float(line2[52:63]),
    )


def parse_tle_text(text: str, strict: bool = True) -> list[TLE]:
    """Parse a block of 2-line or 3-line (named) element sets."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[TLE] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append(parse_tle(lines[i], lines[i + 1], strict=strict))
            i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append(parse_tle(lines[i + 1], lines[i + 2], name=lines[i], strict=strict))
            i += 3
        else:
            i += 1
    return out


def format_tle(t: TLE) -> tuple[str, str]:
    """Write a TLE back to two valid lines (used for fixtures and caching)."""
    start = datetime(t.epoch.year, 1, 1, tzinfo=timezone.utc)
    day = (t.epoch - start).total_seconds() / 86400 + 1
    epoch = f"{t.epoch.year % 100:02d}{day:012.8f}"
    l1 = f"1 {t.norad_id:05d}U 00000A   {epoch}  .00000000  00000-0  00000-0 0  999"
    ecc = f"{t.eccentricity:.7f}"[2:]
    l2 = (f"2 {t.norad_id:05d} {t.inclination_deg:8.4f} {t.raan_deg:8.4f} {ecc} "
          f"{t.arg_perigee_deg:8.4f} {t.mean_anomaly_deg:8.4f} {t.mean_motion_rev_per_day:11.8f}    1")
    l1 = l1 + str(tle_checksum(l1))
    l2 = l2 + str(tle_checksum(l2))
    assert len(l1) == 69 and len(l2) == 69, (len(l1), len(l2))
    return l1, l2
