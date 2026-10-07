"""Lofgren Intelligence — a Lofgren Enterprise project.

A governed intelligence layer that senses the physical and digital world,
researches problems, verifies evidence, generates new possibilities, designs
solutions, builds usable artifacts, executes authorized actions, measures
real-world outcomes, and learns from the results.

This release line contains the governed V1–V6 intelligence stack; hosted public readiness is certified separately.
"""

from __future__ import annotations

from pathlib import Path

from .adapters import (
    AdapterRegistry,
    ClinicalTrialsAdapter,
    DocumentAdapter,
    EuropePMCAdapter,
    ImageryCatalogAdapter,
    OperatorSourcesAdapter,
    OrbitalPassAdapter,
    SearchProvider,
    SensorAdapter,
    WebPageAdapter,
    WebSearchAdapter,
)
from .intent import compile_intent
from .kernel import RunResult, run_investigation
from .models import default_provider
from .report import render_json, render_markdown

__version__ = "0.7.0"


def build_registry(
    files: list[str | Path] | None = None,
    texts: dict[str, str] | None = None,
    urls: list[str] | None = None,
    tle_path: str | Path | None = None,
    fetch_orbits: bool = False,
    imagery: bool = False,
    sensor_csvs: list[str | Path] | None = None,
    sensors_authorized: bool = False,
    search: SearchProvider | str | None = None,
    europepmc: str | None = None,
    trials: str | None = None,
    max_records: int = 20,
    sources_manifest: str | Path | None = None,
) -> AdapterRegistry:
    """Registry of the evidence sources a run may use.

    `europepmc` and `trials` are search queries for the free Europe PMC and ClinicalTrials.gov APIs, each
    bounded to `max_records` records (1-100); `sources_manifest` is a JSON file of operator-selected public
    URLs (see adapters/manifest.py). All of them fetch through the SSRF-protected public fetcher.
    """
    reg = AdapterRegistry()
    if search:
        if isinstance(search, str):
            from .adapters import BraveSearchProvider

            if search != "brave":
                raise ValueError(f"unknown search provider '{search}'")
            search = BraveSearchProvider()
        reg.register(WebSearchAdapter(search))
    if files or texts:
        reg.register(DocumentAdapter(files, texts))
    if urls:
        reg.register(WebPageAdapter(urls))
    if tle_path or fetch_orbits:
        reg.register(OrbitalPassAdapter(tle_path=tle_path, fetch=fetch_orbits))
    if imagery:
        reg.register(ImageryCatalogAdapter())
    if sensor_csvs:
        reg.register(SensorAdapter(sensor_csvs, authorized=sensors_authorized))
    if europepmc:
        reg.register(EuropePMCAdapter(europepmc, max_records=max_records))
    if trials:
        reg.register(ClinicalTrialsAdapter(trials, max_records=max_records))
    if sources_manifest:
        reg.register(OperatorSourcesAdapter(sources_manifest))
    return reg


def investigate(objective: str, plan: str = "payg", max_spend_usd: float = 5.0,
                location: dict | None = None, approved: bool = False, **sources) -> RunResult:
    """One call: objective in, verified evidence report out."""
    contract = compile_intent(objective, max_spend_usd=max_spend_usd, location=location)
    return run_investigation(contract, build_registry(**sources), default_provider(), plan, approved=approved)


__all__ = ["__version__", "build_registry", "compile_intent", "investigate", "render_json",
           "render_markdown", "run_investigation"]
