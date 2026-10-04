"""Fixed, fictional fixtures for V2 certification, tests and documentation. Offline and deterministic.

Each design space is plain JSON: the same shape a user, the CLI or an MCP client passes to `run_discovery`.
"""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import Any

AT = "2026-09-30T12:00:00+00:00"  # fixed discovery time: fixtures give the same ids and fingerprints every run


def V(name: str) -> dict:
    return {"op": "var", "name": name}


def K(value: float, unit: str = "") -> dict:
    return {"op": "const", "value": value, "unit": unit}


def add(a: dict, b: dict) -> dict:
    return {"op": "add", "args": [a, b]}


def sub(a: dict, b: dict) -> dict:
    return {"op": "sub", "args": [a, b]}


def mul(a: dict, b: dict) -> dict:
    return {"op": "mul", "args": [a, b]}


def div(a: dict, b: dict) -> dict:
    return {"op": "div", "args": [a, b]}


def rel(lhs: dict, op: str, rhs: dict) -> dict:
    return {"lhs": lhs, "op": op, "rhs": rhs}


# ---- V1 document fixtures --------------------------------------------------------

CONTRADICTED_DOCS = {"report A (fictional)": "Warehouse vacancy in Tucson rose in 2026.",
                     "report B (fictional)": "Warehouse vacancy in Tucson fell in 2026."}
EMPTY_DOCS = {"note (fictional)": "The cafeteria menu changed on Tuesday."}
SOFTWARE_DOCS = {"load test (fictional)": "The order service handled 500 requests per second in 2026 load tests.",
                 "ops review (fictional)": "Ops review: the order service handled 500 requests per second in 2026 "
                                           "load tests."}


def sensor_registry(directory: Path, readings: tuple[float, float] = (40.0, 44.0)):
    from ..adapters import AdapterRegistry, SensorAdapter

    path = Path(directory) / "sensors.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
        w.writerow(["2026-09-01T00:00:00Z", "t-1", "temperature", str(readings[0]), "C", "2026-06-01T00:00:00Z"])
        w.writerow(["2026-09-02T00:00:00Z", "t-1", "temperature", str(readings[1]), "C", "2026-06-01T00:00:00Z"])
    reg = AdapterRegistry()
    reg.register(SensorAdapter([path], authorized=True))
    return reg


def sensor_run(directory: Path | None = None):
    """A V1 run with one verified physical claim: the warehouse's mean temperature (42 degrees C)."""
    from ..certification import _run

    if directory is not None:
        return _run("Is warehouse temperature too high for storage?", sensor_registry(directory))
    with tempfile.TemporaryDirectory() as d:
        return _run("Is warehouse temperature too high for storage?", sensor_registry(Path(d)))


# ---- design spaces ----------------------------------------------------------------

PROFIT = sub(mul(mul(V("sqft"), V("rent")), V("occupancy")), mul(V("sqft"), V("build_cost")))
PROFIT_MODEL = {"name": "warehouse-profit", "version": "1", "outcomes": {"profit": PROFIT},
                "units": {"sqft": "sqft", "rent": "usd/sqft", "occupancy": "", "build_cost": "usd/sqft"}}


def warehouse_design(**overrides: Any) -> dict:
    """Business: choose a warehouse size. Break-even rent is build_cost / occupancy."""
    design = {
        "model": PROFIT_MODEL,
        "value_metric": "profit",
        "success": rel(V("profit"), ">=", K(0, "usd")),
        "distributions": {"occupancy": {"dist": "triangular", "low": 0.6, "mode": 0.85, "high": 0.95}},
        "assumptions": [
            {"statement": "Market rent is 12 USD per square foot per year (fictional)",
             "why_assumed": "the V1 map has no verified rent", "name": "rent", "value": 12, "unit": "usd/sqft"},
            {"statement": "Annualized build cost is 8 USD per square foot (fictional)",
             "why_assumed": "engineering estimate", "name": "build_cost", "value": 8, "unit": "usd/sqft"},
            {"statement": "The budget allows at most 200000 square feet (fictional)",
             "why_assumed": "owner input", "name": "max_sqft", "value": 200000, "unit": "sqft"},
        ],
        "constraints": [{"name": "size within budget", "kind": "resource",
                         "relation": rel(V("sqft"), "<=", V("max_sqft")),
                         "variable_units": {"sqft": "sqft", "max_sqft": "sqft"}, "assumption": "max_sqft"}],
        "candidates": [
            {"description": "Build 100000 sqft (fictional)", "parameters": {"sqft": [100000, "sqft"], "occupancy": [0.85, ""]},
             "test_requirements": ["Pre-leasing commitments for the first phase"]},
            {"description": "Build 150000 sqft (fictional)", "parameters": {"sqft": [150000, "sqft"], "occupancy": [0.85, ""]},
             "test_requirements": ["Pre-leasing commitments for the first phase"]},
            {"description": "Build 250000 sqft (fictional)", "parameters": {"sqft": [250000, "sqft"], "occupancy": [0.85, ""]}},
        ],
        "simulation": {"seed": 11, "iterations": 500},
    }
    design.update(overrides)
    return design


def fragile_design() -> dict:
    """A large lease barely above break-even (higher expected value, but fragile) and a smaller one with a wide
    margin. Neither dominates the other; the default rule's robustness floor decides."""
    d = warehouse_design(distributions={}, constraints=[])
    d["assumptions"] = d["assumptions"][:2]
    d["candidates"] = [
        {"description": "Lease 10000000 sqft at 9.45 USD per sqft (fictional)",
         "parameters": {"sqft": [10000000, "sqft"], "occupancy": [0.85, ""], "rent": [9.45, "usd/sqft"]}},
        {"description": "Lease 100000 sqft at 12 USD per sqft (fictional)",
         "parameters": {"sqft": [100000, "sqft"], "occupancy": [0.85, ""], "rent": [12, "usd/sqft"]}},
    ]
    return d


def engineering_design(temperature_claim_id: str) -> dict:
    """Engineering: size the cooling for a warehouse whose verified mean temperature sets `temp`."""
    residual = sub(V("temp"), mul(V("capacity"), V("per_kw")))
    net = sub(V("budget"), mul(V("capacity"), V("price_per_kw")))
    return {
        "model": {"name": "cooling", "version": "1", "outcomes": {"residual_temp": residual, "net_value": net},
                  "units": {"temp": "°C", "capacity": "kW", "per_kw": "°C/kW", "budget": "usd", "price_per_kw": "usd/kW"}},
        "value_metric": "net_value",
        "facts": {"temp": temperature_claim_id},
        "assumptions": [
            {"statement": "Each kW of cooling lowers the hall by 0.5 degrees C (fictional)",
             "why_assumed": "vendor data sheet", "name": "per_kw", "value": 0.5, "unit": "°C/kW"},
            {"statement": "Stored goods need at most 25 degrees C (fictional)",
             "why_assumed": "product specification", "name": "target", "value": 25, "unit": "°C"},
            {"statement": "The cooling budget is 60000 USD (fictional)",
             "why_assumed": "owner input", "name": "budget", "value": 60000, "unit": "usd"},
            {"statement": "Cooling costs 900 USD per kW installed (fictional)",
             "why_assumed": "vendor quote", "name": "price_per_kw", "value": 900, "unit": "usd/kW"},
        ],
        "constraints": [
            {"name": "goods stay at or below target", "kind": "physical",
             "relation": rel(V("residual_temp"), "<=", V("target")),
             "variable_units": {"residual_temp": "°C", "target": "°C"}, "assumption": "target"},
            {"name": "observed temperature within the equipment rating", "kind": "physical",
             "relation": rel(V("temp"), "<=", K(45, "°C")), "variable_units": {"temp": "°C"},
             "fact": temperature_claim_id},
        ],
        "candidates": [
            {"description": "Install 20 kW of cooling (fictional)", "parameters": {"capacity": [20, "kW"]}},
            {"description": "Install 40 kW of cooling (fictional)", "parameters": {"capacity": [40, "kW"]},
             "test_requirements": ["Thermal survey after installation"]},
            {"description": "Install 60 kW of cooling (fictional)", "parameters": {"capacity": [60, "kW"]}},
        ],
    }


def software_design() -> dict:
    """Software: how many service instances keep latency within its objective at peak load."""
    latency = add(V("base_ms"), mul(div(V("load"), V("instances")), V("per_req_ms")))
    net = sub(V("budget"), mul(V("instances"), V("instance_cost")))
    return {
        "model": {"name": "service-latency", "version": "1", "outcomes": {"latency_ms": latency, "net_value": net},
                  "units": {"base_ms": "ms", "load": "rps", "instances": "", "per_req_ms": "ms/rps", "budget": "usd",
                            "instance_cost": "usd"}},
        "value_metric": "net_value",
        "assumptions": [
            {"statement": "Peak load is 1500 requests per second (fictional)", "why_assumed": "growth forecast",
             "name": "load", "value": 1500, "unit": "rps"},
            {"statement": "Base latency is 20 ms (fictional)", "why_assumed": "profiling",
             "name": "base_ms", "value": 20, "unit": "ms"},
            {"statement": "Each request per second adds 0.2 ms per instance (fictional)", "why_assumed": "load test fit",
             "name": "per_req_ms", "value": 0.2, "unit": "ms/rps"},
            {"statement": "The latency objective is 200 ms (fictional)", "why_assumed": "service level objective",
             "name": "slo_ms", "value": 200, "unit": "ms"},
            {"statement": "The monthly budget is 10000 USD (fictional)", "why_assumed": "owner input",
             "name": "budget", "value": 10000, "unit": "usd"},
            {"statement": "An instance costs 1200 USD per month (fictional)", "why_assumed": "price list",
             "name": "instance_cost", "value": 1200, "unit": "usd"},
        ],
        "constraints": [{"name": "latency within objective", "kind": "logical",
                         "relation": rel(V("latency_ms"), "<=", V("slo_ms")),
                         "variable_units": {"latency_ms": "ms", "slo_ms": "ms"}, "assumption": "slo_ms"}],
        "candidates": [
            {"description": "Run 1 instance (fictional)", "parameters": {"instances": [1, ""]}},
            {"description": "Run 2 instances (fictional)", "parameters": {"instances": [2, ""]},
             "dependencies": ["Load balancer in front of the instances", "Shared session store"],
             "test_requirements": ["Peak load test at the forecast load"]},
            {"description": "Run 4 instances (fictional)", "parameters": {"instances": [4, ""]},
             "dependencies": ["Load balancer in front of the instances", "Shared session store"]},
        ],
    }


def resource_design() -> dict:
    """Resource optimization: a production plan from a linear program with a known optimum (x=2, y=6, 36)."""
    throughput = add(mul(K(3, "units"), V("x")), mul(K(5, "units"), V("y")))
    return {
        "model": {"name": "throughput", "version": "1", "outcomes": {"throughput": throughput},
                  "units": {"x": "", "y": ""}},
        "value_metric": "throughput",
        "assumptions": [{"statement": "Line A runs at most 4 batches (fictional)", "why_assumed": "line capacity",
                         "name": "line_a", "value": 4, "unit": ""}],
        "constraints": [{"name": "line A capacity", "kind": "resource", "relation": rel(V("x"), "<=", V("line_a")),
                         "variable_units": {"x": "", "line_a": ""}, "assumption": "line_a"}],
        "optimization": {"variables": [{"name": "x", "lower": 0}, {"name": "y", "lower": 0}],
                         "objective": add(mul(K(3), V("x")), mul(K(5), V("y"))), "direction": "maximize",
                         "constraints": [rel(V("x"), "<=", K(4)), rel(mul(K(2), V("y")), "<=", K(12)),
                                         rel(add(mul(K(3), V("x")), mul(K(2), V("y"))), "<=", K(18))]},
        "candidates": [{"description": "Run line A only (fictional)", "parameters": {"x": [4, ""], "y": [0, ""]}}],
        "decision_rule": {"maximize": "expected_value", "min_robustness": "fragile"},
    }


def infeasible_design() -> dict:
    """No feasible solution: every candidate breaks the budget and the plan's constraints contradict each other."""
    d = warehouse_design(distributions={})
    d["assumptions"][2] = {"statement": "The budget allows at most 50000 square feet (fictional)",
                           "why_assumed": "owner input", "name": "max_sqft", "value": 50000, "unit": "sqft"}
    d["candidates"] = d["candidates"][:2]
    d["optimization"] = {"variables": [{"name": "sqft", "lower": 0}], "objective": V("sqft"), "direction": "maximize",
                         "constraints": [rel(V("sqft"), "<=", K(50000)), rel(V("sqft"), ">=", K(80000))]}
    return d


PRIOR_ART_COVERAGE = {"sources": ["fictional trade-journal index"], "domains": ["logistics"],
                      "time_range": [None, None], "limitations": ["a fixed fictional index of 2 records"]}
PRIOR_ART_MATCHING = {
    "subject": "rail-served cold-storage warehouses",
    "queries": ["rail served cold storage warehouse"],
    "records": [
        {"title": "Rail-served cold storage warehouse cuts trucking costs (fictional)", "source": "Fictional Logistics Review",
         "uri": "https://example.invalid/rail-cold", "published": "2025-03-01",
         "summary": "A cold storage warehouse served by rail reduced truck trips.", "domains": ["logistics"],
         "keywords": ["rail", "served", "cold", "storage", "warehouse"]},
        {"title": "Dockside freezer storage at inland ports (fictional)", "source": "Fictional Port Quarterly",
         "uri": "https://example.invalid/dockside", "published": "2024-07-01", "summary": "Freezer storage near docks.",
         "domains": ["logistics"]},
    ],
    "coverage": PRIOR_ART_COVERAGE,
}
PRIOR_ART_NONE = {"subject": "drone-loaded vertical warehouses", "queries": ["drone loaded vertical warehouse"],
                  "records": PRIOR_ART_MATCHING["records"], "coverage": PRIOR_ART_COVERAGE}
