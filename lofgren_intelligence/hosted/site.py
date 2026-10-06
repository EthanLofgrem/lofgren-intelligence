"""Public website for the hosted Lofgren Intelligence service.

Presentation only: nothing here grants access, asserts payment, runs research
or changes entitlement state. Every capability statement is derived from, or
must stay consistent with, the capability manifest in ``capabilities.py``
(``docs/CAPABILITIES.json``) and its ``NOT_PROVEN`` list.

Pages are plain strings built with :mod:`html` escaping. They load one
stylesheet and one script from ``/static/`` (package data), use no inline
event handlers or ``style`` attributes, and load nothing from a CDN, so they
run under a strict Content-Security-Policy (see :func:`csp`).
"""

from __future__ import annotations

import hashlib
import html
import secrets
from importlib import resources

from . import capabilities

e = html.escape

STATIC_TYPES = {
    "site.css": "text/css; charset=utf-8",
    "site.js": "text/javascript; charset=utf-8",
    "mark.svg": "image/svg+xml",
}


def _static_version() -> str:
    digest = hashlib.sha256()
    for name in sorted(STATIC_TYPES):
        digest.update(resources.files(__package__).joinpath("static", name).read_bytes())
    return digest.hexdigest()[:10]


# Cache-busting query for /static/ URLs; changes whenever an asset changes.
STATIC_VERSION = _static_version()

# The MCP transport owns /mcp, so the human-readable MCP page lives here.
MCP_PAGE = "/mcp-clients"

NAV = (
    ("product", "Product", "/product"),
    ("pricing", "Pricing", "/pricing"),
    ("mcp", "MCP", MCP_PAGE),
    ("docs", "Docs", "/docs"),
    ("about", "About", "/about"),
)

CLIENT_STATUS = "Designed to connect via MCP; client compatibility is being verified."
CLIENTS = ("ChatGPT", "Claude", "Codex", "Other MCP clients")

PLANNED_LABEL = "Planned — not yet available; paid checkout opens after pricing is approved."

# The governed states and their user-facing messages (shared with the console).
GOVERNED_STATES = (
    ("needs-clarification", "Needs clarification", "?", "We need more context before research can begin."),
    ("insufficient", "Insufficient evidence", "!", "Available evidence is not enough to support a conclusion."),
    ("contradicted", "Contradicted", "✕", "Reliable sources materially disagree. Review both sides."),
    ("paused", "Paused for budget", "‖", "The next stage exceeds your approved research budget."),
    ("limited", "Completed with limitations", "✓", "Research is complete for the approved scope; important limits remain."),
    ("failed", "Failed safely", "■", "The case did not complete. No conclusion was produced."),
    ("quota", "Quota reached", "○", "You've reached your current usage limit."),
)

MARK = (
    '<svg class="mark" viewBox="0 0 32 32" width="28" height="28" aria-hidden="true" focusable="false">'
    '<rect x="1" y="1" width="30" height="30" rx="8" class="mark-bg"/>'
    '<path d="M10 8v16h12" class="mark-l"/>'
    '<circle cx="22" cy="10" r="3" class="mark-dot"/></svg>'
)


def new_nonce() -> str:
    return secrets.token_urlsafe(18)


def csp(nonce: str) -> str:
    return (
        "default-src 'none'; "
        "script-src 'self'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def page_headers(nonce: str) -> dict[str, str]:
    return {"Content-Security-Policy": csp(nonce), "Cache-Control": "no-cache"}


def static_bytes(name: str) -> bytes | None:
    if name not in STATIC_TYPES:
        return None
    return resources.files(__package__).joinpath("static", name).read_bytes()


def _head(title: str, nonce: str, extra_style: str = "") -> str:
    style = f'<style nonce="{e(nonce)}">{extra_style}</style>' if extra_style else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)} · Lofgren Intelligence</title>"
        '<meta name="description" content="Evidence-first research, verification and discovery through MCP.">'
        f'<link rel="icon" href="/static/mark.svg?v={STATIC_VERSION}" type="image/svg+xml">'
        f'<link rel="stylesheet" href="/static/site.css?v={STATIC_VERSION}">'
        f'<script src="/static/site.js?v={STATIC_VERSION}"></script>'
        f"{style}</head>"
    )


def _nav_items(active: str) -> str:
    out = []
    for key, label, href in NAV:
        current = ' aria-current="page"' if key == active else ""
        out.append(f'<li><a href="{href}"{current}>{label}</a></li>')
    return "".join(out)


def site_header(active: str = "") -> str:
    return (
        '<header class="site-header"><div class="container header-inner">'
        f'<a class="brand" href="/">{MARK}<span>Lofgren Intelligence</span></a>'
        '<button class="nav-toggle" type="button" aria-expanded="false" aria-controls="site-menu">Menu</button>'
        '<div class="site-menu collapsible" id="site-menu">'
        f'<nav aria-label="Primary"><ul class="nav-links">{_nav_items(active)}</ul></nav>'
        '<div class="header-actions"><a class="link-quiet" href="/account">Sign in</a>'
        f'<a class="btn btn-primary" href="{MCP_PAGE}#connect">Get started</a></div>'
        "</div></div></header>"
    )


def site_footer() -> str:
    return (
        '<footer class="site-footer"><div class="container footer-inner">'
        '<div class="footer-brand"><p class="footer-name">Lofgren Intelligence</p>'
        "<p>Evidence-first research for better-informed decisions.</p></div>"
        '<nav aria-label="Footer"><ul class="footer-links">'
        f'<li><a href="/product">Product</a></li><li><a href="/pricing">Pricing</a></li>'
        f'<li><a href="{MCP_PAGE}">MCP</a></li><li><a href="/docs">Documentation</a></li>'
        '<li><a href="/about">About</a></li></ul></nav>'
        '<nav aria-label="Account and policies"><ul class="footer-links">'
        '<li><a href="/account">Account</a></li><li><a href="/pricing#billing">Billing</a></li>'
        '<li><a href="/account">Privacy</a></li><li><a href="/about#terms">Terms</a></li></ul></nav>'
        '<a class="footer-cta" href="/docs#tools">API &amp; MCP docs <span aria-hidden="true">→</span></a>'
        "</div>"
        '<div class="container footer-note"><p>Status: tools are registered on the hosted MCP endpoint. '
        "Deployment, real-client compatibility, backup/restore and the public release gate are not yet proven.</p></div>"
        "</footer>"
    )


def public_page(*, title: str, active: str, body: str, nonce: str, extra_style: str = "") -> str:
    return (
        _head(title, nonce, extra_style)
        + '<body class="public"><a class="skip-link" href="#main">Skip to main content</a>'
        + site_header(active)
        + f'<main id="main" tabindex="-1">{body}</main>'
        + site_footer()
        + "</body></html>"
    )


# --------------------------------------------------------------------------- shared components

def demo_banner() -> str:
    return (
        '<p class="demo-banner" role="note"><span class="demo-tag">Demo</span> '
        "Illustrative demo data — not a live research result.</p>"
    )


def state_card(key: str, name: str, glyph: str, message: str, heading: str = "h3") -> str:
    return (
        f'<article class="state state-{key}"><span class="state-icon" aria-hidden="true">{e(glyph)}</span>'
        f"<div><{heading} class=\"state-name\">{e(name)}</{heading}><p>{e(message)}</p></div></article>"
    )


def governed_states(heading: str = "h3") -> str:
    return '<div class="state-grid">' + "".join(state_card(*s, heading=heading) for s in GOVERNED_STATES) + "</div>"


def client_list(link: bool = True) -> str:
    items = []
    for name in CLIENTS:
        slug = name.lower().replace(" ", "-")
        label = e(name)
        inner = (f'<a class="client-chip" href="{MCP_PAGE}#client-{slug}">{label} <span aria-hidden="true">›</span></a>'
                 if link else f'<span class="client-chip">{label}</span>')
        items.append(f"<li>{inner}</li>")
    return f'<ul class="client-list">{"".join(items)}</ul>'


PLANS = (
    {
        "id": "founding-free", "name": "Founding Free", "who": "Accounts 1–1,000",
        "price": "$0", "unit": "", "detail": "Quota-limited access for the first 1,000 activated accounts.",
        "features": ("Research and verification tools", "Evidence and source links", "Weekly usage limit shown by usage_status"),
        "planned": False,
    },
    {
        "id": "payg", "name": "Pay as you go", "who": "Flexible, occasional use",
        "price": "$0.0312", "unit": "per prompted research", "detail": "Heavy work: $12.48 each.",
        "features": ("No subscription", "Pay only for estimated, approved work", "Same evidence and traceability"),
        "planned": True,
    },
    {
        "id": "researcher", "name": "Researcher", "who": "For regular researchers",
        "price": "$49.99", "unit": "/ month", "detail": "400 weekly entries · $0.0156 each.",
        "features": ("Everything in Pay as you go", "Lower per-entry rate", "Included heavy credits"),
        "planned": True,
    },
    {
        "id": "good-idea", "name": "Good Idea", "who": "For power users and teams",
        "price": "$79.99", "unit": "/ month", "detail": "20,000 monthly entries · $0.0050 each.",
        "features": ("Everything in Researcher", "Lowest per-entry rate", "Build, act and measure at scale"),
        "planned": True,
    },
)


def plan_cards(heading: str = "h3") -> str:
    cards = []
    for p in PLANS:
        features = "".join(f'<li><span aria-hidden="true">✓</span> {e(f)}</li>' for f in p["features"])
        if p["planned"]:
            status = f'<p class="plan-status planned"><span aria-hidden="true">◷</span> {e(PLANNED_LABEL)}</p>'
        else:
            status = '<p class="plan-status available"><strong>Founding access</strong> — limited to the first 1,000 accounts.</p>'
        unit = f' <span class="plan-unit">{e(p["unit"])}</span>' if p["unit"] else ""
        cards.append(
            f'<article class="plan" id="plan-{p["id"]}" aria-labelledby="plan-{p["id"]}-name">'
            f'<{heading} class="plan-name" id="plan-{p["id"]}-name">{e(p["name"])}</{heading}>'
            f'<p class="plan-who">{e(p["who"])}</p>'
            f'<p class="plan-price"><span class="price">{e(p["price"])}</span>{unit}</p>'
            f'<p class="plan-detail">{e(p["detail"])}</p>{status}'
            f'<ul class="checks">{features}</ul></article>'
        )
    return '<div class="plan-grid">' + "".join(cards) + "</div>"


JOURNEY = (
    ("1", "Objective", "Turn a goal into a scoped plan.", ("compile_objective", "plan_research")),
    ("2", "Research", "Find relevant, citable evidence.", ("investigate",)),
    ("3", "Verify", "Check claims and surface contradictions.", ("verify_claim", "find_contradictions")),
    ("4", "Discover", "Generate supported possibilities from verified state.", ("discover",)),
    ("5", "Build", "Produce a verified artifact.", ("build_artifact",)),
    ("6", "Act", "Propose, approve in the browser, then execute.", ("propose_action", "execute_action")),
    ("7", "Measure", "Compare expected with actual results.", ("measure_outcome",)),
    ("8", "Improve", "Evaluate improvements on held-out evidence.", ("evaluate_improvement",)),
)


def journey_steps() -> str:
    items = []
    for num, name, text, tools in JOURNEY:
        codes = " ".join(f"<code>{t}</code>" for t in tools)
        items.append(
            f'<li class="step"><span class="step-num" aria-hidden="true">{num}</span>'
            f'<div><h3 class="step-name">{name}</h3><p>{text}</p><p class="step-tools">{codes}</p></div></li>'
        )
    return f'<ol class="steps">{"".join(items)}</ol>'


def receipt_note() -> str:
    return ('<p class="muted">Every stage writes its own Receipt. Retrieve the audit trail with '
            "<code>get_receipt</code> and <code>export_knowledge_map2</code>.</p>")


RELEASE_ROWS = (
    ("Evidence Intelligence", "V1"),
    ("Discovery Intelligence", "V2"),
    ("Production Intelligence", "V3"),
    ("Execution Intelligence", "V4"),
    ("Outcome Intelligence", "V5"),
    ("Improvement Intelligence", "V6"),
)


def release_status() -> str:
    rows = "".join(
        f'<div class="status-row"><dt>{name}</dt><dd><span class="pill pill-well">'
        f'<span aria-hidden="true">✓</span> {lvl} certified</span></dd></div>'
        for name, lvl in RELEASE_ROWS
    )
    rows += ('<div class="status-row"><dt>Founding Free access</dt><dd>Accounts 1–1000</dd></div>')
    return f'<dl class="status-list">{rows}</dl>'


READINESS_NOTICE = (
    '<p class="notice"><strong>Governance boundary:</strong> V1–V6 are certified as the bounded intelligence '
    "stack on this release line. Public readiness is a separate gate: hosted OAuth, tenant isolation, persistence, "
    "deployment identity, backup/restore, billing and real-client interoperability must still pass before this "
    "release is declared public-ready.</p>"
)


# --------------------------------------------------------------------------- pages

HOME_STYLE = "@media(max-width:820px){.hero-grid{grid-template-columns:1fr}}"


def home_page(base: str, nonce: str) -> str:
    endpoint = e(base.rstrip("/") + "/mcp")
    preview = (
        '<aside class="preview card" aria-labelledby="preview-title">'
        + demo_banner()
        + '<div class="preview-head"><span class="pill pill-limited"><span aria-hidden="true">✓</span> '
        "Completed with limitations</span>"
        '<h2 class="preview-title" id="preview-title">Should I open a warehouse in Phoenix?</h2></div>'
        '<dl class="assurance-mini">'
        '<div><dt>Assurance</dt><dd><span class="pill pill-provisional">PROVISIONAL</span></dd></div>'
        '<div><dt>Evidence coverage</dt><dd>8 of 18 claims well supported (demo)</dd></div>'
        '<div><dt>Contradiction exposure</dt><dd>2 contradictions, 1 decision-relevant (demo)</dd></div>'
        '<div><dt>Decision-relevant unknowns</dt><dd>3 (demo)</dd></div></dl>'
        '<a class="btn btn-secondary" href="/app/research/demo-warehouse">Open the demo case</a>'
        "</aside>"
    )
    body = f"""
<section class="hero"><div class="container hero-grid">
<div class="hero-copy">
<h1 class="display">Lofgren <span class="accent">Intelligence</span></h1>
<p class="hero-sub">Evidence-first research, verification, discovery, building, execution and learning.</p>
<p class="lede">Move from a question to evidence, understanding and better decisions. Lofgren Intelligence helps you and
your AI client find, verify and use real-world information with traceable evidence — including what disagrees and
what is still unknown.</p>
<div class="actions"><a class="btn btn-primary" href="{MCP_PAGE}#connect">Connect your AI client <span aria-hidden="true">→</span></a>
<a class="btn btn-secondary" href="/pricing">View pricing</a>
<a class="btn btn-secondary" href="#how">See how it works</a></div>
</div>
{preview}
</div></section>

<section class="band" id="how" aria-labelledby="how-title"><div class="container">
<div class="section-head"><h2 id="how-title">How it works</h2><p>From a question to a measured, reviewed outcome.</p></div>
{journey_steps()}
{receipt_note()}
</div></section>

<section class="band band-alt" aria-labelledby="clients-title"><div class="container split">
<div class="section-head"><h2 id="clients-title">Use with MCP clients</h2>
<p>Connect Lofgren Intelligence to an MCP-capable AI client.</p></div>
<div>{client_list()}<p class="muted">{e(CLIENT_STATUS)}</p>
<p><a class="link-arrow" href="{MCP_PAGE}">Learn about MCP <span aria-hidden="true">→</span></a></p></div>
</div></section>

<section class="band" aria-labelledby="pricing-title"><div class="container">
<div class="section-head"><h2 id="pricing-title">Simple, transparent pricing</h2>
<p>Start free. Every plan keeps evidence, source links and receipts.</p></div>
{plan_cards()}
</div></section>

<section class="band band-alt" aria-labelledby="trust-title"><div class="container">
<div class="section-head"><h2 id="trust-title">Built for trust and better decisions</h2>
<p>We surface the full picture, not just what confirms a belief.</p></div>
<ul class="feature-grid">
<li><h3>Evidence, not opinions</h3><p>Findings link to the sources that support them.</p></li>
<li><h3>Contradictions included</h3><p>See what disagrees and why it matters.</p></li>
<li><h3>Unknowns highlighted</h3><p>We say what could not be verified. <strong>INSUFFICIENT_EVIDENCE</strong> is a governed answer, with missing-evidence requirements attached.</p></li>
<li><h3>Receipts you can check</h3><p>Trace every claim back to its evidence.</p></li>
<li><h3>Full traceability</h3><p>Follow the chain from question to evidence to answer.</p></li>
</ul>
</div></section>

<section class="band" id="connect-summary" aria-labelledby="status-title"><div class="container split">
<div class="section-head"><h2 id="status-title">Release status</h2>
<p>Start in your AI client with <code>account_status</code> and <code>usage_status</code> so entitlement and quota are
visible before expensive work begins. Founding Free covers activated accounts 1–1000.</p>
<p class="endpoint"><span>MCP endpoint</span> <code>{endpoint}</code></p></div>
<div class="card">{release_status()}</div>
</div>
<div class="container">{READINESS_NOTICE}</div></section>
"""
    return public_page(title="Evidence-first research", active="", body=body, nonce=nonce, extra_style=HOME_STYLE)


def product_page(nonce: str) -> str:
    levels = capabilities.LEVEL_TITLES
    stage_rows = "".join(
        f'<li class="card"><h3>{e(levels[lvl])}</h3><p class="muted">{lvl} · '
        f'{len([t for t, l in capabilities.TOOL_LEVELS.items() if l == lvl])} hosted tools</p></li>'
        for lvl in ("V1", "V2", "V3", "V4", "V5", "V6")
    )
    body = f"""
<section class="page-hero"><div class="container">
<h1>Product</h1>
<p class="lede">A governed chain from objective to evidence, discovery, verified artifacts, approved actions,
measured outcomes and reviewed improvement — each stage with its own receipt.</p>
<div class="actions"><a class="btn btn-primary" href="/app">Explore the demo console</a>
<a class="btn btn-secondary" href="/docs">Read the docs</a></div>
</div></section>

<section class="band" aria-labelledby="stages-title"><div class="container">
<div class="section-head"><h2 id="stages-title">Stages</h2><p>Tool counts come from the hosted capability manifest.</p></div>
<ul class="card-grid">{stage_rows}</ul>
</div></section>

<section class="band band-alt" aria-labelledby="assurance-title"><div class="container split">
<div class="section-head"><h2 id="assurance-title">Layered assurance, not a single score</h2>
<p>A single percentage hides what matters. Each result reports:</p></div>
<ul class="checks card">
<li><span aria-hidden="true">✓</span> <strong>Assurance level</strong> — for example PROVISIONAL until decision-relevant unknowns are resolved.</li>
<li><span aria-hidden="true">✓</span> <strong>Evidence coverage</strong> — the share of claims well supported, partially supported, uncertain and contradicted.</li>
<li><span aria-hidden="true">✓</span> <strong>Contradiction exposure</strong> — what disagrees and whether it affects the decision.</li>
<li><span aria-hidden="true">✓</span> <strong>Decision-relevant unknowns</strong> — counted and listed with how to resolve them.</li>
<li><span aria-hidden="true">✓</span> <strong>Scope</strong> — what the answer covers and what it does not.</li>
</ul></div></section>

<section class="band" aria-labelledby="states-title"><div class="container">
<div class="section-head"><h2 id="states-title">Governed states</h2>
<p>When evidence, budget or quota run out, the result says so plainly instead of guessing.</p></div>
{governed_states()}
</div></section>
"""
    return public_page(title="Product", active="product", body=body, nonce=nonce)


def pricing_page(nonce: str) -> str:
    body = f"""
<section class="page-hero"><div class="container">
<h1>Pricing</h1>
<p class="lede">Start free. Paid plans are planned and are not yet available: paid checkout opens after pricing is approved.</p>
</div></section>
<section class="band" aria-labelledby="plans-title"><div class="container">
<h2 id="plans-title" class="visually-hidden">Plans</h2>
{plan_cards()}
</div></section>
<section class="band band-alt" id="billing" aria-labelledby="billing-title"><div class="container split">
<div class="section-head"><h2 id="billing-title">How billing will work</h2>
<p>The rules below describe the pricing model. They are not an offer.</p></div>
<ul class="checks card">
<li><span aria-hidden="true">✓</span> Every job is estimated before it runs.</li>
<li><span aria-hidden="true">✓</span> A spend cap blocks any job whose estimate exceeds it.</li>
<li><span aria-hidden="true">✓</span> The charge is never more than the estimate.</li>
<li><span aria-hidden="true">✓</span> Returning from checkout does not prove payment; access changes only after the signed webhook updates your entitlement.</li>
</ul></div></section>
"""
    return public_page(title="Pricing", active="pricing", body=body, nonce=nonce)


def mcp_page(base: str, nonce: str) -> str:
    endpoint = e(base.rstrip("/") + "/mcp")
    clients = "".join(
        f'<li class="card" id="client-{name.lower().replace(" ", "-")}"><h3>{e(name)}</h3>'
        f'<p><span class="pill pill-partial"><span aria-hidden="true">◐</span> Verification in progress</span></p>'
        f"<p class=\"muted\">{e(CLIENT_STATUS)}</p></li>"
        for name in CLIENTS
    )
    body = f"""
<section class="page-hero"><div class="container">
<h1>Connect through MCP</h1>
<p class="lede">The Model Context Protocol lets an AI client call Lofgren Intelligence tools. Your client discovers
OAuth from the endpoint, opens a consent screen, and receives scoped access.</p>
<p class="endpoint"><span>MCP endpoint</span> <code>{endpoint}</code></p>
</div></section>
<section class="band" aria-labelledby="clients-title"><div class="container">
<div class="section-head"><h2 id="clients-title">MCP clients</h2><p>{e(CLIENT_STATUS)}</p></div>
<ul class="card-grid">{clients}</ul>
</div></section>
<section class="band band-alt" id="connect" aria-labelledby="connect-title"><div class="container">
<div class="section-head"><h2 id="connect-title">Connect in three steps</h2></div>
<ol class="card-grid numbered">
<li class="card"><h3>Add the server</h3><p>Add the remote MCP endpoint to your MCP-capable AI client.</p></li>
<li class="card"><h3>Review consent</h3><p>Sign in and authorize the named client for the <code>mcp</code> scope. Check the redirect host shown on the consent page.</p></li>
<li class="card"><h3>Check access</h3><p>Call <code>account_status</code> and <code>usage_status</code> first.</p></li>
</ol></div></section>
"""
    return public_page(title="MCP", active="mcp", body=body, nonce=nonce)


def docs_page(base: str, nonce: str) -> str:
    endpoint = e(base.rstrip("/") + "/mcp")
    level_blocks = []
    for level in capabilities.LEVELS:
        tools = sorted(t for t, lvl in capabilities.TOOL_LEVELS.items() if lvl == level)
        items = "".join(
            f"<li><code>{t}</code>"
            + (f' <span class="muted">(gated: {e(capabilities.GATED[t])})</span>' if t in capabilities.GATED else "")
            + "</li>"
            for t in tools
        )
        level_blocks.append(
            f'<section class="card" aria-labelledby="lvl-{level}"><h3 id="lvl-{level}">{e(level)} · '
            f"{e(capabilities.LEVEL_TITLES[level])}</h3><ul class=\"tool-list\">{items}</ul></section>"
        )
    not_proven = "".join(f"<li>{e(x)}</li>" for x in capabilities.NOT_PROVEN)
    body = f"""
<section class="page-hero"><div class="container">
<h1>Documentation</h1>
<p class="lede">The hosted service exposes its tools on one authenticated MCP endpoint: <code>{endpoint}</code>.</p>
</div></section>
<section class="band" aria-labelledby="journey-title"><div class="container">
<div class="section-head"><h2 id="journey-title">The governed journey</h2></div>
{journey_steps()}
{receipt_note()}
</div></section>
<section class="band band-alt" id="tools" aria-labelledby="tools-title"><div class="container">
<div class="section-head"><h2 id="tools-title">Hosted tools by level</h2>
<p>Generated from the capability manifest (<code>docs/CAPABILITIES.json</code>). “Hosted” means registered on the
authenticated endpoint; it does not mean deployed or publicly launched.</p></div>
<div class="card-grid">{"".join(level_blocks)}</div>
</div></section>
<section class="band" aria-labelledby="states-title"><div class="container">
<div class="section-head"><h2 id="states-title">Recovery and governed states</h2>
<p><strong>INSUFFICIENT_EVIDENCE</strong>, contradiction, quota and budget limits are valid outcomes, not UI failures.</p></div>
{governed_states()}
</div></section>
<section class="band band-alt" aria-labelledby="proof-title"><div class="container">
<div class="section-head"><h2 id="proof-title">Not proven</h2>
<p>The repository's tests and certifications do not prove the following:</p></div>
<ul class="card plain-list">{not_proven}</ul>
<p><a href="/.well-known/oauth-authorization-server">OAuth metadata</a> · <a href="/healthz">Health</a></p>
</div></section>
"""
    return public_page(title="Docs", active="docs", body=body, nonce=nonce)


def about_page(nonce: str) -> str:
    body = """
<section class="page-hero"><div class="container">
<h1>About Lofgren Intelligence</h1>
<p class="lede">We build research tools that show their work: the evidence behind a claim, the evidence against it,
and what is still unknown.</p>
</div></section>
<section class="band" aria-labelledby="principles-title"><div class="container">
<div class="section-head"><h2 id="principles-title">Principles</h2></div>
<ul class="feature-grid">
<li><h3>Show the evidence</h3><p>Conclusions carry their sources and their limits.</p></li>
<li><h3>Expose disagreement</h3><p>Contradictions are reported with both sides.</p></li>
<li><h3>Name the unknowns</h3><p>Missing evidence is a result, not a failure.</p></li>
<li><h3>Ask before acting</h3><p>External actions wait for explicit approval of the exact action.</p></li>
<li><h3>Keep receipts</h3><p>Every stage leaves an inspectable record.</p></li>
</ul></div></section>
<section class="band band-alt" id="terms" aria-labelledby="terms-title"><div class="container">
<div class="section-head"><h2 id="terms-title">Terms and status</h2>
<p>Terms of service have not been published yet. The service is in development: deployment, real-client
compatibility and backup/restore are not yet proven, and paid plans are not yet available.</p>
<p>Manage or delete your account and data from the <a href="/account">account page</a>.</p></div>
</div></section>
"""
    return public_page(title="About", active="about", body=body, nonce=nonce)
