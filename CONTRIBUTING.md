# Contributing

1. Branch from `main` as `build/<version>-<topic>` (for example `build/v2-simulation`).
2. Keep the core on the Python standard library; optional integrations go behind an adapter or provider.
3. Every change ships with tests: `python -m unittest discover -s tests -t .`
4. New data sources are adapters registered with the registry and declare capability, license, cost and read-only access.
5. Nothing may access a system, satellite or device without authorization. Actions go through the authority engine.
6. Sample data must be labeled fictional.
7. Open a pull request with what changed, why, and the test evidence.
