"""Command line: `lofgren <command>`.

    lofgren investigate "objective" --files notes/ --lat 33.45 --lon -112.07 --tle elements.tle
    lofgren estimate "objective" --files notes/
    lofgren passes --lat 33.45 --lon -112.07 --fetch
    lofgren pricing --standard-units 500 --heavy 5
    lofgren satellites
    lofgren mcp
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, build_registry
from .billing.pricing import PLANS, cheapest_plan, monthly_bill
from .intent.compiler import compile_intent
from .kernel.pipeline import estimate_run, run_investigation
from .kernel.knowledge_map import export_knowledge_map
from .kernel.state import export_state
from .verification.calibration import PredictionLog
from .models.provider import default_provider
from .orbital.catalog import IMAGING_SATELLITES, fetch_tles, load_tles
from .orbital.propagate import PROPAGATOR, find_passes
from .report.markdown import render_json, render_markdown
from .research.planner import plan_research


def _add_sources(p: argparse.ArgumentParser) -> None:
    p.add_argument("objective", help="what you want to know, verify or accomplish")
    p.add_argument("--files", nargs="*", default=[], help="documents or folders to use as evidence")
    p.add_argument("--url", nargs="*", default=[], dest="urls", help="public web pages to read")
    p.add_argument("--search", choices=["brave"], help="discover web pages with a search provider (needs BRAVE_API_KEY)")
    p.add_argument("--tle", help="file of orbital elements (TLE) for pass prediction")
    p.add_argument("--fetch-orbits", action="store_true", help="fetch current elements from CelesTrak")
    p.add_argument("--imagery", action="store_true", help="search open Sentinel-2 / Landsat catalogs")
    p.add_argument("--sensors", nargs="*", default=[], help="CSV readings from your own sensors")
    p.add_argument("--sensors-authorized", action="store_true",
                   help="confirm you own or are authorized to read these sensors")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--plan", default="payg", choices=sorted(PLANS))
    p.add_argument("--max-spend", type=float, default=5.0, help="research spend cap in USD")


def _setup(args: argparse.Namespace):
    location = {"lat": args.lat, "lon": args.lon, "name": None} if args.lat is not None and args.lon is not None else None
    contract = compile_intent(args.objective, max_spend_usd=args.max_spend, location=location)
    registry = build_registry(files=args.files, urls=args.urls, tle_path=args.tle, fetch_orbits=args.fetch_orbits,
                              imagery=args.imagery, sensor_csvs=args.sensors,
                              sensors_authorized=args.sensors_authorized, search=args.search)
    return contract, registry


def cmd_estimate(args: argparse.Namespace) -> int:
    contract, registry = _setup(args)
    plan = plan_research(contract, registry)
    est = estimate_run(plan, args.plan)
    print(f"Objective: {contract.objective}\nMode: {contract.mode}\n")
    print("Questions:")
    for q in contract.questions:
        print(f"  - {q.text}")
    print(f"\nPlan: {len(plan.tasks)} gather tasks, {plan.estimated_work_units:g} WU")
    for g in plan.gaps:
        print(f"  gap: {g.question} -> {g.reason}")
    status = "allowed" if est.allowed else f"not allowed: {est.reason}"
    print(f"\nEstimate ({PLANS[args.plan].name}): {est.job_class} job, ${est.total_usd:.4f} ({status})")
    print(f"Spend cap: ${contract.max_spend_usd:.2f}")
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    contract, registry = _setup(args)
    log = PredictionLog(args.log) if args.log else None
    result = run_investigation(contract, registry, default_provider(), args.plan, approved=args.approve,
                               prediction_log=log)
    md = render_markdown(result)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(md)
    if args.json:
        Path(args.json).write_text(json.dumps(render_json(result), indent=2, default=str), encoding="utf-8")
        print(f"full run written to {args.json}", file=sys.stderr)
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(result.receipt, indent=2, default=str), encoding="utf-8")
        print(f"research receipt {result.receipt.get('research_id')} written to {args.receipt}", file=sys.stderr)
    if args.state:
        Path(args.state).write_text(json.dumps(export_state(result), indent=2, default=str), encoding="utf-8")
        print(f"knowledge map written to {args.state}", file=sys.stderr)
    if args.state2:
        Path(args.state2).write_text(json.dumps(export_knowledge_map(result), indent=2), encoding="utf-8")
        print(f"knowledge-map/2 written to {args.state2}", file=sys.stderr)
    return 0 if result.completed else 2


def cmd_calibration(args: argparse.Namespace) -> int:
    log = PredictionLog(args.log)
    if args.claim:
        n = log.resolve(args.claim, args.correct == "yes")
        print(f"recorded outcome for {n} prediction(s) of {args.claim}")
    summary = log.summary()
    print(json.dumps(summary, indent=2))
    return 0


def cmd_schemas(args: argparse.Namespace) -> int:
    from .schemas import write_schemas

    for p in write_schemas(args.out):
        print(p)
    return 0


def _json_file(path: str | None, what: str) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read the {what} file {path}: {exc}") from None


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery.pipeline import discover_from_run
    from .discovery.report import render_discovery_markdown

    contract, registry = _setup(args)
    run = run_investigation(contract, registry, default_provider(), args.plan, approved=args.approve)
    result = discover_from_run(run, args.goal or args.objective, design=_json_file(args.design, "design"),
                               prior_art=_json_file(args.prior_art, "prior-art"))
    md = render_discovery_markdown(result)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"discovery report written to {args.out}", file=sys.stderr)
    else:
        print(md)
    for path, data, what in ((args.json, None, "discovery"), (args.receipt, result.receipt, "discovery receipt"),
                             (args.handoff, result.handoff, "V3 handoff")):
        if not path:
            continue
        if what == "discovery":
            from .discovery.report import discovery_summary

            data = discovery_summary(result)
        if data is None:
            print(f"no {what}: the outcome is {result.outcome.value}", file=sys.stderr)
            continue
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"{what} written to {path}", file=sys.stderr)
    return 0


def cmd_produce(args: argparse.Namespace) -> int:
    from .discovery.pipeline import discover_from_run
    from .production import ProductionError, build_artifact, write_artifact

    contract, registry = _setup(args)
    run = run_investigation(contract, registry, default_provider(), args.plan, approved=args.approve)
    result = discover_from_run(run, args.goal or args.objective, design=_json_file(args.design, "design"),
                               prior_art=_json_file(args.prior_art, "prior-art"))
    if result.handoff is None:
        print(f"nothing to produce: discovery outcome is {result.outcome.value}", file=sys.stderr)
        for q in result.requirements:
            print(f"  needs: {q.description}", file=sys.stderr)
        return 2
    try:
        produced = build_artifact(result.handoff, discovery_receipt=result.receipt, context=result.context,
                                  kind=args.kind)
        write_artifact(produced, args.out_dir)
    except ProductionError as exc:
        print(f"production refused: {exc}", file=sys.stderr)
        return 1
    tests = produced.verification.tests
    print(json.dumps({
        "artifact_id": produced.artifact_id,
        "kind": args.kind,
        "directory": str(args.out_dir),
        "files": [f["path"] for f in produced.artifact["files"]],
        "checks": {x.requirement_id: x.passed for x in produced.acceptance},
        "tests": tests,
        "receipt": produced.receipt["receipt_hash"],
        "authority_granted": False,
    }, indent=2))
    return 0


def cmd_verify_artifact(args: argparse.Namespace) -> int:
    from .production import verify_directory

    out = verify_directory(args.directory)
    print(json.dumps(out, indent=2))
    return 0 if out["passed"] else 1


def cmd_certify(args: argparse.Namespace) -> int:
    from .certification import render_certification, run_certification

    if getattr(args, "v3", False):
        from .production.certification import render_v3_certification, run_v3_certification

        cert = run_v3_certification()
        print(render_v3_certification(cert))
        if args.out:
            Path(args.out).write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
        return 0 if cert["code_terms_certified"] else 1
    if args.v2:
        from .discovery.certification import render_v2_certification, run_v2_certification

        cert = run_v2_certification()
        print(render_v2_certification(cert))
        if args.out:
            Path(args.out).write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
        return 0 if cert["code_terms_certified"] else 1
    cert = run_certification()
    print(render_certification(cert))
    if args.out:
        Path(args.out).write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
    return 0 if cert["v1_ready"] else 1


def cmd_certify_boundary(args: argparse.Namespace) -> int:
    from .boundary_certification import render_boundary, run_boundary_certification

    cert = run_boundary_certification()
    print(render_boundary(cert))
    if args.out:
        Path(args.out).write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
    return 0 if cert["code_terms_certified"] else 1


def cmd_passes(args: argparse.Namespace) -> int:
    if args.tle:
        tles = load_tles(args.tle)
    elif args.fetch:
        tles = fetch_tles([s.norad_id for s in IMAGING_SATELLITES])
    else:
        print("give --tle FILE or --fetch", file=sys.stderr)
        return 2
    start = datetime.now(timezone.utc)
    rows = []
    for t in tles:
        for p in find_passes(t, args.lat, args.lon, start, args.hours, args.min_elevation):
            rows.append(p)
    rows.sort(key=lambda p: p.rise)
    print(f"Passes over ({args.lat}, {args.lon}) above {args.min_elevation:g} deg, next {args.hours:g} h "
          f"[{PROPAGATOR}]")
    for p in rows:
        print(f"  {p.rise:%Y-%m-%d %H:%M} UTC  {p.name:<14} max {p.max_elevation_deg:5.1f} deg  "
              f"{p.duration_s / 60:4.1f} min")
    if not rows:
        print("  none")
    return 0


def cmd_pricing(args: argparse.Namespace) -> int:
    print(f"{'Plan':<14}{'Fee':>9}{'Rate/unit':>11}{'Heavy':>9}{'Incl.':>7}  Limit")
    for p in PLANS.values():
        print(f"{p.name:<14}{p.monthly_fee:>9.2f}{p.rate:>11.4f}{p.heavy_price:>9.2f}{p.included_heavy:>7}  "
              f"{p.entry_limit:,}/{p.limit_period}")
    if args.standard_units is not None:
        print(f"\nMonthly bill for {args.standard_units:g} standard units and {args.heavy} heavy jobs:")
        for pid in PLANS:
            if pid != "free":
                print(f"  {PLANS[pid].name:<14} ${monthly_bill(pid, args.standard_units, args.heavy):,.2f}")
        print(f"  cheapest: {PLANS[cheapest_plan(args.standard_units, args.heavy)].name}")
    return 0


def cmd_satellites(_: argparse.Namespace) -> int:
    for s in IMAGING_SATELLITES:
        print(f"{s.name:<12} NORAD {s.norad_id:<6} {s.sensor:<42} {s.data_access}")
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from .mcp.server import serve

    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lofgren", description="Lofgren Intelligence — evidence to outcome.")
    parser.add_argument("--version", action="version", version=f"lofgren-intelligence {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("investigate", help="run the V1 loop and write a verified report")
    _add_sources(p)
    p.add_argument("--approve", action="store_true", help="approve actions that need approval")
    p.add_argument("--out", help="write the Markdown report here")
    p.add_argument("--json", help="write the full run (findings, graph, ledger, receipt) as JSON here")
    p.add_argument("--receipt", help="write the research receipt here")
    p.add_argument("--state", help="write the V2 knowledge map (knowledge-map/1) here")
    p.add_argument("--state2", help="write knowledge-map/2 (provenance, question associations, derivation) here")
    p.add_argument("--log", help="append stated confidences to this prediction log (JSONL)")
    p.set_defaults(fn=cmd_investigate)

    p = sub.add_parser("estimate", help="show the contract, plan and price without running")
    _add_sources(p)
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("passes", help="predict imaging-satellite passes over a location")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--tle")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--min-elevation", type=float, default=30.0)
    p.set_defaults(fn=cmd_passes)

    p = sub.add_parser("pricing", help="show plans and compare monthly bills")
    p.add_argument("--standard-units", type=float)
    p.add_argument("--heavy", type=int, default=0)
    p.set_defaults(fn=cmd_pricing)

    p = sub.add_parser("calibration", help="record real outcomes and show calibration (Brier, reliability)")
    p.add_argument("--log", required=True, help="prediction log written by investigate --log")
    p.add_argument("--claim", help="claim id whose outcome is now known")
    p.add_argument("--correct", choices=["yes", "no"], help="did the claim turn out true?")
    p.set_defaults(fn=cmd_calibration)

    p = sub.add_parser("schemas", help="write the evidence-protocol JSON schemas")
    p.add_argument("--out", default="schemas")
    p.set_defaults(fn=cmd_schemas)

    p = sub.add_parser("discover", help="research an objective (V1), then run Discovery Intelligence (V2) over it")
    _add_sources(p)
    p.add_argument("--goal", help="the discovery objective, if different from the research objective")
    p.add_argument("--design", help="design space JSON: model, assumptions, constraints, candidates, optimization")
    p.add_argument("--prior-art", dest="prior_art", help="prior-art fixture JSON: subject, queries, records, coverage")
    p.add_argument("--approve", action="store_true", help="approve actions that need approval")
    p.add_argument("--out", help="write the Markdown discovery report here")
    p.add_argument("--json", help="write the typed discovery summary here")
    p.add_argument("--receipt", help="write the discovery receipt here")
    p.add_argument("--handoff", help="write the V3 handoff here (only when a candidate is selected)")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("produce", help="research (V1), discover (V2), then build and verify an artifact (V3)")
    _add_sources(p)
    p.add_argument("--goal", help="the discovery objective, if different from the research objective")
    p.add_argument("--design", required=True, help="design space JSON (see docs/DISCOVERY.md)")
    p.add_argument("--prior-art", dest="prior_art", help="prior-art fixture JSON")
    p.add_argument("--kind", default="structured_bundle", choices=["structured_bundle", "markdown", "python_module"])
    p.add_argument("--out-dir", dest="out_dir", required=True, help="new or empty directory for the artifact")
    p.add_argument("--approve", action="store_true", help="approve actions that need approval")
    p.set_defaults(fn=cmd_produce)

    p = sub.add_parser("verify-artifact", help="independently verify an artifact directory written by produce")
    p.add_argument("directory")
    p.set_defaults(fn=cmd_verify_artifact)

    p = sub.add_parser("certify", help="run the V1 certification suite, or a later version certification")
    versions = p.add_mutually_exclusive_group()
    versions.add_argument("--v2", action="store_true", help="run V2 Discovery certification")
    versions.add_argument("--v3", action="store_true", help="run V3 Production certification")
    p.add_argument("--out", help="write the certification result as JSON")
    p.set_defaults(fn=cmd_certify)

    p = sub.add_parser("certify-boundary", help="certify the V1 -> V2 boundary (code terms of the step-4 gate)")
    p.add_argument("--out", help="write the boundary certification result as JSON")
    p.set_defaults(fn=cmd_certify_boundary)

    sub.add_parser("satellites", help="list open-data imaging satellites").set_defaults(fn=cmd_satellites)
    sub.add_parser("mcp", help="run as an MCP server over stdio").set_defaults(fn=cmd_mcp)

    args = parser.parse_args(argv)
    _utf8_output()
    return args.fn(args)


def _utf8_output() -> None:
    """Reports and certification use characters such as ≥ and ✓. Where stdout or stderr is not UTF-8
    (a pipe on a Windows cp1252 locale, for example), switch it to UTF-8 so they print instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
