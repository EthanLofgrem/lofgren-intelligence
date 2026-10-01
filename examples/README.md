# Examples

All sample data here is **fictional** and exists to demonstrate the system.

- `sample-sources/` — three short documents that agree and disagree about Phoenix industrial construction. Run:
  `lofgren investigate "Is industrial construction in the Phoenix metro increasing?" --files examples/sample-sources`
- `sample.tle` — synthetic orbital elements shaped like Sentinel-2A and Landsat 9. For real passes use `--fetch`.
  `lofgren passes --lat 33.4484 --lon -112.0740 --tle examples/sample.tle`
- `sample_sensors.csv` — a soil-moisture sensor log. Run with `--sensors examples/sample_sensors.csv --sensors-authorized`.
