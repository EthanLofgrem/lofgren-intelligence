"""Lofgren Intelligence — a Lofgren Enterprise project.

A governed intelligence layer that senses the physical and digital world,
researches problems, verifies evidence, generates new possibilities, designs
solutions, builds usable artifacts, executes authorized actions, measures
real-world outcomes, and learns from the results.

This release is V1, Evidence Intelligence.
"""

from __future__ import annotations

from pathlib import Path

from .adapters import (
    AdapterRegistry,
    DocumentAdapter,
    ImageryCatalogAdapter,
    OrbitalPassAdapter,
    SensorAdapter,
    WebPageAdapter,
)
from .intent import compile_intent
from .kernel import RunResult, run_investigation
from .models import default_provider
from .report import render_json, render_markdown

__version__ = "0.1.0"


def build_registry(
    files: list[str | Path] | None = None,
    texts: dict[str, str] | None = None,
    urls: list[str] | None = None,
    tle_path: str | Path | None = None,
    fetch_orbits: bool = False,
    imagery: bool = False,
    sensor_csvs: list[str | Path] | None = None,
    sensors_authorized: bool = False,
) -> AdapterRegistry:
    reg = AdapterRegistry()
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
    return reg


def investigate(objective: str, plan: str = "payg", max_spend_usd: float = 5.0,
                location: dict | None = None, approved: bool = False, **sources) -> RunResult:
    """One call: objective in, verified evidence report out."""
    contract = compile_intent(objective, max_spend_usd=max_spend_usd, location=location)
    return run_investigation(contract, build_registry(**sources), default_provider(), plan, approved=approved)


__all__ = ["__version__", "build_registry", "compile_intent", "investigate", "render_json",
           "render_markdown", "run_investigation"]
