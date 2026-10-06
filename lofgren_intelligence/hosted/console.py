"""Research-console SHELL: static, illustrative demo pages.

Nothing here reads tenant state, calls a backend, runs research or records a
decision. Every page carries the non-dismissable demo banner and every
identifier, count and status is labelled as demo data. The clarification
interview and Case Charter on the new-research page are a UI prototype only.
"""

from __future__ import annotations

import html

from .site import GOVERNED_STATES, MARK, _head, demo_banner, governed_states

e = html.escape

CONSOLE_NAV = (
    ("new", "New research", "/app/research/new", "+"),
    ("case", "Demo case", "/app/research/demo-warehouse", "▤"),
    ("library", "Knowledge library", "/app/library", "▦"),
    ("receipts", "Research receipts", "/app/receipts", "≡"),
    ("usage", "Usage & quota", "/app/usage", "◔"),
    ("settings", "Settings", "/app/settings", "⚙"),
)

CASE_QUESTION = "Should I open a warehouse in Phoenix?"
CASE_URL = "/app/research/demo-warehouse"

STATUS = {
    # key: (label, glyph)
    "well": ("Well supported", "✓"),
    "partial": ("Partially supported", "◐"),
    "uncertain": ("Uncertain", "?"),
    "contradicted": ("Contradicted", "✕"),
    "limited": ("Completed with limitations", "✓"),
    "insufficient": ("Insufficient evidence", "!"),
    "failed": ("Failed safely", "■"),
}


def pill(kind: str, label: str | None = None) -> str:
    text, glyph = STATUS.get(kind, (label or kind, "•"))
    return f'<span class="pill pill-{kind}"><span aria-hidden="true">{glyph}</span> {e(label or text)}</span>'


def console_page(*, title: str, active: str, body: str, nonce: str) -> str:
    links = []
    for key, label, href, glyph in CONSOLE_NAV:
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<li><a href="{href}"{current}><span class="side-glyph" aria-hidden="true">{glyph}</span>'
                     f"{e(label)}</a></li>")
    sidebar = (
        '<aside class="sidebar" aria-label="Console sidebar">'
        f'<div class="sidebar-top"><a class="brand brand-dark" href="/app">{MARK}<span>Lofgren Intelligence'
        '<small>Research console · demo</small></span></a>'
        '<button class="nav-toggle nav-toggle-dark" type="button" aria-expanded="false" aria-controls="console-menu">Menu</button></div>'
        '<div class="collapsible" id="console-menu">'
        f'<nav aria-label="Console"><ul class="side-links">{"".join(links)}</ul></nav>'
        '<div class="side-card"><p class="side-card-title">MCP connection</p>'
        "<p>Demo shell: no AI client is connected and no research runs here.</p>"
        '<a href="/mcp-clients#connect">Connection guide</a></div>'
        "</div></aside>"
    )
    topbar = (
        '<header class="topbar"><ul class="chips" aria-label="Demo account status">'
        '<li class="chip"><span class="chip-k">account_status</span> <span>Demo</span></li>'
        '<li class="chip"><span class="chip-k">usage_status</span> <span>128 / 500 units (demo)</span></li>'
        '<li class="chip"><span class="chip-k">receipts</span> <span>Enabled (demo)</span></li>'
        '</ul><a class="link-quiet" href="/">Back to site</a></header>'
    )
    footer = (
        '<footer class="console-footer"><p>Research console shell — illustrative demo data only. '
        '<a href="/docs">Docs</a> · <a href="/about#terms">Terms and status</a></p></footer>'
    )
    return (
        _head(title, nonce)
        + '<body class="console"><a class="skip-link" href="#main">Skip to main content</a>'
        + f'<div class="shell">{sidebar}<div class="workspace">{topbar}'
        + f'<main id="main" tabindex="-1">{demo_banner()}{body}</main>{footer}</div></div>'
        + "</body></html>"
    )


# --------------------------------------------------------------------------- /app

def overview_page(nonce: str) -> str:
    body = f"""
<div class="page-head"><h1>Research console</h1>
<p class="muted">A static preview of how cases, receipts and governed states will appear.</p>
<a class="btn btn-primary" href="/app/research/new">Start new research</a></div>
<section class="panel" aria-labelledby="recent-title"><h2 id="recent-title">Recent cases (demo)</h2>
<div class="table-wrap"><table><caption class="visually-hidden">Demo cases</caption>
<thead><tr><th scope="col">Case</th><th scope="col">State</th><th scope="col">Assurance</th></tr></thead><tbody>
<tr><th scope="row"><a href="{CASE_URL}">{e(CASE_QUESTION)}</a></th><td>{pill("limited", "Completed with limitations")}</td><td><span class="pill pill-provisional">PROVISIONAL</span></td></tr>
<tr><th scope="row">Which supplier region has the lowest lead-time risk? (demo)</th><td>{pill("contradicted")}</td><td><span class="pill pill-provisional">PROVISIONAL</span></td></tr>
<tr><th scope="row">Is a rooftop solar lease worth it for a small office? (demo)</th><td>{pill("insufficient", "Insufficient evidence")}</td><td>No conclusion</td></tr>
</tbody></table></div></section>
<section class="panel" aria-labelledby="states-title"><h2 id="states-title">Governed states</h2>
<p class="muted">Each state is shown with an icon, a name and a message, never by color alone.</p>
{governed_states()}</section>
"""
    return console_page(title="Research console", active="", body=body, nonce=nonce)


# --------------------------------------------------------------------------- /app/research/new

QUESTIONS = (
    ("q-purpose", "What decision will this research inform?",
     "The decision sets which evidence is relevant and what counts as enough."),
    ("q-location", "Which locations or submarkets are you considering?",
     "Labor, zoning and logistics evidence differ sharply across the metro."),
    ("q-timing", "When would the warehouse need to open?",
     "Zoning and utility lead times only matter relative to your timeline."),
    ("q-size", "Roughly how much space and how many shifts do you need?",
     "Size and shifts change labor, power and site requirements."),
    ("q-limits", "What would rule Phoenix out for you?",
     "Knowing your deal-breakers lets research test them first."),
)

ANSWER_CHOICES = (
    ("answer", "Answer"),
    ("unknown", "Unknown"),
    ("skip", "Skip"),
    ("default", "Use a reasonable default"),
    ("later", "Ask me later"),
)


def _question(idx: int, qid: str, text: str, reason: str) -> str:
    radios = "".join(
        f'<label class="choice"><input type="radio" name="{qid}-mode" id="{qid}-{val}" value="{val}"'
        f'{" checked" if val == "answer" else ""}> {e(label)}</label>'
        for val, label in ANSWER_CHOICES
    )
    return (
        f'<li class="question"><fieldset><legend><span class="q-num">Question {idx}</span> {e(text)}</legend>'
        f'<p class="q-reason"><strong>Why we ask:</strong> {e(reason)}</p>'
        f'<label class="field-label" for="{qid}">Your answer</label>'
        f'<input class="input" type="text" id="{qid}" name="{qid}" autocomplete="off">'
        f'<div class="choices" role="group" aria-label="How to handle question {idx}">{radios}</div>'
        "</fieldset></li>"
    )


def _check(name: str, value: str, label: str, checked: bool = False) -> str:
    cid = f"{name}-{value}"
    return (f'<label class="choice"><input type="checkbox" id="{cid}" name="{name}" value="{value}"'
            f'{" checked" if checked else ""}> {e(label)}</label>')


def new_research_page(nonce: str) -> str:
    questions = "".join(_question(i + 1, *q) for i, q in enumerate(QUESTIONS))
    sources = "".join(_check("sources", v, l, c) for v, l, c in (
        ("public-stats", "Public statistics", True), ("gov-records", "Government records", True),
        ("industry", "Industry reports", True), ("news", "News coverage", False),
        ("licensed", "Licensed data (extra cost)", False)))
    excluded = "".join(_check("exclude", v, l, True) for v, l in (
        ("contact", "Contacting brokers, landlords or officials"), ("purchase", "Buying data or services"),
        ("external", "Any external action without browser approval")))
    body = f"""
<div class="page-head"><h1>New research</h1>
<p class="note-prototype" role="note"><strong>Prototype</strong> — the live clarification interview is in development.
Nothing on this page is sent anywhere, and no research runs.</p></div>

<section class="panel" aria-labelledby="objective-title">
<h2 id="objective-title">Objective</h2>
<label class="field-label" for="objective">What do you want to find out?</label>
<textarea class="input input-xl" id="objective" name="objective" rows="3">{e(CASE_QUESTION)}</textarea>
<div class="mode-row" role="group" aria-label="Research modes (prototype)">
{_check("mode", "deep", "Deep research", True)}{_check("mode", "verify", "Verify claims", True)}
{_check("mode", "contradictions", "Find contradictions", True)}{_check("mode", "unknowns", "Explore unknowns", True)}
</div>
<button class="btn btn-primary" type="button" disabled aria-describedby="run-disabled">Run research</button>
<p class="muted" id="run-disabled">Disabled in the prototype: research starts only after a scope is approved.</p>
</section>

<section class="panel" aria-labelledby="interview-title">
<h2 id="interview-title">Clarification interview (sample)</h2>
<p class="muted">Sample questions with the reason for each. Every question can be answered, marked unknown, skipped,
filled with a reasonable default, or deferred.</p>
<ol class="question-list">{questions}</ol>
</section>

<section class="panel" aria-labelledby="charter-title">
<h2 id="charter-title">Case Charter (sample)</h2>
<dl class="kv">
<div><dt>Status</dt><dd><span class="pill pill-provisional">READY_FOR_SCOPE_APPROVAL</span>
<span class="pill pill-uncertain"><span aria-hidden="true">!</span> Not approved</span></dd></div>
<div><dt>Decision</dt><dd>Whether to open a warehouse in the Phoenix metro (sample)</dd></div>
<div><dt>Known context</dt><dd>Regional distribution; single shift; opening within 18 months (sample answers)</dd></div>
<div><dt>Deferred questions</dt><dd>Deal-breakers (asked later)</dd></div>
<div><dt>Defaults used</dt><dd>Mid-size facility (sample default)</dd></div>
</dl>
<p class="muted">A charter in this status is waiting for you to approve the scope. It has not been approved and no budget has been committed.</p>
</section>

<section class="panel" aria-labelledby="scope-title">
<h2 id="scope-title">Scope Builder (prototype)</h2>
<div class="form-grid">
<div><label class="field-label" for="scope-outcome">Outcome type</label>
<select class="input" id="scope-outcome" name="scope-outcome"><option>Decision brief</option><option>Evidence map</option><option>Risk register</option></select></div>
<div><label class="field-label" for="scope-location">Location</label>
<input class="input" type="text" id="scope-location" name="scope-location" value="Phoenix metro, Arizona"></div>
<div><label class="field-label" for="scope-from">Time range — from</label>
<input class="input" type="month" id="scope-from" name="scope-from" value="2024-01"></div>
<div><label class="field-label" for="scope-to">Time range — to</label>
<input class="input" type="month" id="scope-to" name="scope-to" value="2026-09"></div>
<div><label class="field-label" for="scope-depth">Depth</label>
<select class="input" id="scope-depth" name="scope-depth"><option>Standard</option><option>Verified</option><option selected>Deep</option><option>Heavy</option></select></div>
<div><label class="field-label" for="scope-budget">Budget (USD, sample)</label>
<input class="input" type="number" id="scope-budget" name="scope-budget" min="0" step="0.01" value="1.25"></div>
<div><label class="field-label" for="scope-time">Time limit (minutes)</label>
<input class="input" type="number" id="scope-time" name="scope-time" min="1" value="20"></div>
</div>
<fieldset class="fieldset"><legend>Allowed source classes</legend><div class="choices">{sources}</div></fieldset>
<fieldset class="fieldset"><legend>Out-of-scope actions</legend><div class="choices">{excluded}</div></fieldset>
<button class="btn btn-primary" type="button" disabled aria-describedby="approve-disabled">Approve scope</button>
<p class="muted" id="approve-disabled">Disabled in the prototype: scope approval is not available yet.</p>
</section>
"""
    return console_page(title="New research", active="new", body=body, nonce=nonce)


# --------------------------------------------------------------------------- /app/research/demo-warehouse

CLAIMS = (
    ("Industrial demand in the Phoenix metro stayed strong through the study period.", "well",
     "Regional industrial market report (sample)"),
    ("Industrial vacancy is close to the metro's recent average.", "well",
     "Quarterly vacancy survey (sample)"),
    ("Warehouse labor costs are above the national average.", "partial",
     "Wage statistics and staffing quotes (sample)"),
    ("Zoning approval can take 6–12+ months in some municipalities.", "uncertain",
     "Municipal planning pages (sample)"),
    ("I-10 congestion materially delays peak-period freight.", "contradicted",
     "Corridor travel-time data vs operator survey (sample)"),
    ("Grid connection for large sites can take over a year in some submarkets.", "uncertain",
     "Utility planning notice (sample)"),
)

COVERAGE = (("well", 8), ("partial", 5), ("uncertain", 3), ("contradicted", 2))

UNKNOWNS = (
    ("U1", "Zoning timeline for the shortlisted parcels", "High", "Opening date; carrying cost",
     "Pre-application meeting with the municipality", "Open"),
    ("U2", "Labor availability for the required shifts", "High", "Operating cost; hiring plan",
     "Staffing-agency quotes for the candidate submarket", "Open"),
    ("U3", "Grid connection lead time", "High", "Opening date", "Utility service inquiry", "In progress"),
    ("U4", "Last-mile carrier capacity", "Medium", "Service levels", "Carrier quotes for planned volumes", "Open"),
)

ASSUMPTIONS = (
    ("A1", "Demand continues near its recent trend through the planning horizon", "High", "Utilization; revenue",
     "Recheck against the next quarter's absorption data", "Accepted for this scope"),
    ("A2", "Lease rates stay within the sampled range", "Medium", "Operating cost",
     "Broker quotes for shortlisted sites", "Unverified"),
    ("A3", "Single-shift operation at opening", "Low", "Labor need", "Confirm with the operations plan", "From your answers"),
)


def _register(rows: tuple, caption: str, first: str) -> str:
    body = "".join(
        f'<tr><th scope="row">{e(rid)} (demo)</th><td>{e(text)}</td><td>{e(mat)}</td><td>{e(aff)}</td>'
        f"<td>{e(res)}</td><td>{e(status)}</td></tr>"
        for rid, text, mat, aff, res, status in rows
    )
    return (
        f'<div class="table-wrap"><table><caption>{e(caption)}</caption><thead><tr><th scope="col">ID</th>'
        f'<th scope="col">{e(first)}</th><th scope="col">Materiality</th><th scope="col">Affects</th>'
        f'<th scope="col">How to resolve</th><th scope="col">Status</th></tr></thead><tbody>{body}</tbody></table></div>'
    )


def _coverage() -> str:
    total = sum(n for _, n in COVERAGE)
    rows = []
    for kind, n in COVERAGE:
        share = round(100 * n / total)
        rows.append(
            f'<li class="cov-row"><span class="cov-label">{pill(kind)}</span>'
            f'<svg class="bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">'
            f'<rect class="bar-track" width="100" height="8" rx="4"/><rect class="bar-fill bar-{kind}" width="{share}" height="8" rx="4"/></svg>'
            f'<span class="cov-value">{n} of {total} claims · {share}% share (demo)</span></li>'
        )
    return f'<ul class="coverage" aria-label="Evidence coverage">{"".join(rows)}</ul>'


TABS = ("evidence", "contradictions", "unknowns", "gaps", "recommendations")


def _tabs() -> str:
    labels = {"evidence": "Evidence", "contradictions": "Contradictions", "unknowns": "Unknowns",
              "gaps": "Gaps", "recommendations": "Recommendations"}
    counts = {"evidence": 6, "contradictions": 2, "unknowns": 4, "gaps": 3, "recommendations": 3}
    tablist = "".join(
        f'<a role="tab" id="tab-{k}" href="#panel-{k}" aria-controls="panel-{k}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{labels[k]} <span class="count">{counts[k]}</span></a>'
        for i, k in enumerate(TABS)
    )
    evidence_rows = "".join(
        f"<tr><th scope=\"row\">{e(src)}</th><td>{e(kind)}</td><td>{e(supports)}</td><td>{pill(st)}</td></tr>"
        for src, kind, supports, st in (
            ("Regional industrial market report (sample)", "Industry report", "Demand trend", "well"),
            ("Quarterly vacancy survey (sample)", "Industry report", "Vacancy level", "well"),
            ("Wage statistics extract (sample)", "Public statistics", "Labor cost", "partial"),
            ("Municipal planning pages (sample)", "Government records", "Zoning timelines", "uncertain"),
            ("Corridor travel-time data (sample)", "Government records", "Congestion", "contradicted"),
            ("Logistics operator survey (sample)", "Industry survey", "Congestion", "contradicted"),
        )
    )
    contradiction = """
<article class="contradiction" aria-labelledby="c1-title">
<h3 id="c1-title">Contradiction C1 (demo) · <span class="ctype">SCOPE_DEPENDENT</span></h3>
<div class="versus">
<div class="side"><p class="side-label">Claim A</p><p>I-10 congestion materially delays peak-period freight.</p>
<p class="muted">Corridor travel-time data (sample)</p></div>
<div class="vs" aria-hidden="true">vs</div>
<div class="side"><p class="side-label">Claim B</p><p>Operators report peak delays as minor compared with other metros.</p>
<p class="muted">Logistics operator survey (sample)</p></div>
</div>
<dl class="kv">
<div><dt>Contradiction type</dt><dd>SCOPE_DEPENDENT — both can be true for different scopes.</dd></div>
<div><dt>Why the evidence differs</dt><dd>Claim A measures all traffic on the corridor at peak hours. Claim B surveys operators who already schedule dispatch around those peaks.</dd></div>
<div><dt>Impact on the decision</dt><dd>Affects which side of the metro to shortlist and the delivery windows you can promise; on its own it does not decide go or no-go.</dd></div>
<div><dt>Evidence that would resolve it</dt><dd>Route-level travel times from the candidate sites at your planned dispatch windows, plus carrier quotes.</dd></div>
</dl></article>
<p class="muted">C2 (demo): labor cost estimates differ between wage statistics and staffing quotes — <span class="ctype">SOURCE_DEPENDENT</span>, lower impact.</p>
"""
    panels = {
        "evidence": f'<div class="table-wrap"><table><caption>Evidence (demo, 6 of 12 sample sources shown)</caption><thead><tr><th scope="col">Source</th><th scope="col">Class</th><th scope="col">Supports</th><th scope="col">Status</th></tr></thead><tbody>{evidence_rows}</tbody></table></div>',
        "contradictions": contradiction,
        "unknowns": _register(UNKNOWNS, "Unknowns register (demo)", "Unknown"),
        "gaps": '<ul class="plain-list"><li><strong>Gap (demo):</strong> parcel-level zoning records for the shortlisted sites.</li><li><strong>Gap (demo):</strong> sub-metro wage data for warehouse roles.</li><li><strong>Gap (demo):</strong> utility capacity for large connections by submarket.</li></ul>',
        "recommendations": '<ol class="plain-list"><li><strong>Recommendation (demo):</strong> confirm zoning timelines for two shortlisted parcels before committing.</li><li><strong>Recommendation (demo):</strong> collect staffing quotes for the required shifts.</li><li><strong>Recommendation (demo):</strong> run a route-level travel-time check at planned dispatch windows.</li></ol>',
    }
    panel_html = "".join(
        f'<section role="tabpanel" id="panel-{k}" aria-labelledby="tab-{k}" class="tabpanel" tabindex="0">'
        f'<h3 class="panel-title">{labels[k]}</h3>{panels[k]}</section>'
        for k in TABS
    )
    return (
        '<section class="panel" aria-labelledby="details-title"><h2 id="details-title">Case details</h2>'
        f'<div class="tablist" role="tablist" aria-label="Case details">{tablist}</div>{panel_html}</section>'
    )


def _receipt() -> str:
    rows = (
        ("Receipt ID", "DEMO-RECEIPT-0001"),
        ("Created", "Sample timestamp"),
        ("Knowledge fingerprint", "demo-fingerprint-not-a-hash"),
        ("Query", CASE_QUESTION),
        ("Tools used", "investigate, verify_claim, find_contradictions, discover"),
        ("Sources analyzed", "12"),
        ("Claims evaluated", "18"),
        ("Contradictions found", "2"),
        ("Unknowns identified", "4"),
        ("State", "Completed with limitations"),
    )
    items = "".join(
        f'<div><dt>{e(k)}</dt><dd>{e(v)} <span class="demo-tag">demo</span></dd></div>' for k, v in rows
    )
    return (
        '<aside class="panel receipt" aria-labelledby="receipt-title"><h2 id="receipt-title">Research Receipt</h2>'
        '<p class="muted">Every value below is demo data, not a real receipt.</p>'
        f'<dl class="kv kv-tight">{items}</dl>'
        '<button class="btn btn-secondary" type="button" disabled>Export knowledge map (unavailable in demo)</button>'
        "</aside>"
    )


def _progress() -> str:
    stages = (("Research", "Searched sample sources"), ("Verify", "Checked claims"),
              ("Discover", "Looked for related options"), ("Receipt", "Recorded the case"))
    items = "".join(
        f'<li class="stage stage-done"><span class="stage-num" aria-hidden="true">✓</span>'
        f'<span><strong>{i + 1}. {name}</strong> <span class="stage-state">Done (demo)</span>'
        f'<span class="muted stage-note">{note}</span></span></li>'
        for i, (name, note) in enumerate(stages)
    )
    return f'<ol class="progress" aria-label="Research progress (demo)">{items}</ol>'


def case_page(nonce: str) -> str:
    claims = "".join(
        f'<tr><th scope="row">{e(text)}</th><td>{pill(kind)}</td><td>{e(src)}</td></tr>'
        for text, kind, src in CLAIMS
    )
    limited = next(s for s in GOVERNED_STATES if s[0] == "limited")
    body = f"""
<div class="page-head"><p class="eyebrow">Demo case</p><h1>{e(CASE_QUESTION)}</h1></div>
{_progress()}
<div class="case-grid">
<div class="case-main">
<section class="panel" aria-labelledby="answer-title">
<h2 id="answer-title">Answer</h2>
<div class="state state-limited state-inline"><span class="state-icon" aria-hidden="true">{limited[2]}</span>
<div><p class="state-name">{limited[1]}</p><p>{limited[3]}</p></div></div>
<p class="answer-lead">Promising, but labor cost, zoning timelines and logistics constraints still need confirmation.</p>
<p>The sample evidence shows strong demand and a central location. Labor costs appear above the national average,
zoning approval can be slow in some municipalities, and congestion evidence conflicts. Confirm site-specific zoning,
labor availability and carrier capacity before committing.</p>
</section>

<section class="panel assurance" aria-labelledby="assurance-title">
<h2 id="assurance-title">Assurance</h2>
<dl class="kv">
<div><dt>Assurance</dt><dd><span class="pill pill-provisional">PROVISIONAL</span> until decision-relevant unknowns are resolved.</dd></div>
<div><dt>Evidence coverage</dt><dd>{_coverage()}</dd></div>
<div><dt>Contradiction exposure</dt><dd>2 contradictions (demo); 1 is decision-relevant (C1, SCOPE_DEPENDENT).</dd></div>
<div><dt>Decision-relevant unknowns</dt><dd>3 of 4 recorded unknowns (demo).</dd></div>
<div><dt>Scope</dt><dd>Industrial warehouse siting in the Phoenix metro, sample public and industry sources 2024–2026; excludes parcel-level due diligence and any contact with third parties.</dd></div>
</dl></section>

<section class="panel" aria-labelledby="claims-title">
<h2 id="claims-title">Claims</h2>
<div class="table-wrap"><table><caption>Claims (demo: 6 of 18 shown)</caption>
<thead><tr><th scope="col">Claim</th><th scope="col">Status</th><th scope="col">Key evidence</th></tr></thead>
<tbody>{claims}</tbody></table></div></section>

{_tabs()}

<section class="panel" aria-labelledby="change-title">
<h2 id="change-title">What could change this answer?</h2>
<ul class="plain-list">
<li><strong>Zoning:</strong> if the shortlisted parcels need more than 12 months, the answer moves toward “delay”.</li>
<li><strong>Labor:</strong> if staffing quotes cannot cover the required shifts, operating cost assumptions fail.</li>
<li><strong>Power:</strong> a grid connection lead time beyond the opening date would block the plan for that site.</li>
<li><strong>Congestion:</strong> resolving C1 against the candidate sites could change which side of the metro to choose.</li>
</ul></section>

<section class="panel" aria-labelledby="assumptions-title">
<h2 id="assumptions-title">Assumptions register</h2>
{_register(ASSUMPTIONS, "Assumptions register (demo)", "Assumption")}
</section>
</div>
{_receipt()}
</div>
"""
    return console_page(title="Demo case", active="case", body=body, nonce=nonce)


# --------------------------------------------------------------------------- other console pages

def receipts_page(nonce: str) -> str:
    rows = "".join(
        f'<tr><th scope="row">{e(rid)} <span class="demo-tag">demo</span></th><td>{e(q)}</td><td>{st}</td></tr>'
        for rid, q, st in (
            ("DEMO-RECEIPT-0001", CASE_QUESTION, pill("limited", "Completed with limitations")),
            ("DEMO-RECEIPT-0002", "Which supplier region has the lowest lead-time risk?", pill("contradicted")),
            ("DEMO-RECEIPT-0003", "Is a rooftop solar lease worth it for a small office?", pill("insufficient", "Insufficient evidence")),
            ("DEMO-RECEIPT-0004", "Compare three CRM tools for a five-person team", pill("failed", "Failed safely")),
        )
    )
    body = f"""
<div class="page-head"><h1>Research receipts</h1>
<p class="muted">Receipts record what was searched, verified and concluded. These rows are demo data.</p></div>
<section class="panel" aria-labelledby="list-title"><h2 id="list-title">Receipts (demo)</h2>
<div class="table-wrap"><table><caption class="visually-hidden">Demo receipts</caption>
<thead><tr><th scope="col">Receipt</th><th scope="col">Question</th><th scope="col">State</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p><a href="{CASE_URL}">Open the demo case receipt</a></p></section>
"""
    return console_page(title="Research receipts", active="receipts", body=body, nonce=nonce)


def library_page(nonce: str) -> str:
    body = f"""
<div class="page-head"><h1>Knowledge library</h1>
<p class="muted">Saved analyses and evidence maps will appear here. These entries are demo data.</p></div>
<section class="panel" aria-labelledby="saved-title"><h2 id="saved-title">Saved analyses (demo)</h2>
<ul class="card-grid">
<li class="card"><h3><a href="{CASE_URL}">{e(CASE_QUESTION)}</a></h3><p>{pill("limited", "Completed with limitations")}</p><p class="muted">18 claims · 2 contradictions (demo)</p></li>
<li class="card"><h3>Supplier lead-time risk (demo)</h3><p>{pill("contradicted")}</p><p class="muted">Review both sides before deciding.</p></li>
<li class="card"><h3>Rooftop solar lease (demo)</h3><p>{pill("insufficient", "Insufficient evidence")}</p><p class="muted">No conclusion was supported.</p></li>
</ul></section>
"""
    return console_page(title="Knowledge library", active="library", body=body, nonce=nonce)


def usage_page(nonce: str) -> str:
    quota = next(s for s in GOVERNED_STATES if s[0] == "quota")
    paused = next(s for s in GOVERNED_STATES if s[0] == "paused")
    body = f"""
<div class="page-head"><h1>Usage &amp; quota</h1>
<p class="muted">In your AI client, <code>usage_status</code> reports the real figures. These are demo values.</p></div>
<section class="panel" aria-labelledby="week-title"><h2 id="week-title">This week (demo)</h2>
<p><strong>128 of 500</strong> units used (demo)</p>
<svg class="bar bar-lg" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false"><rect class="bar-track" width="100" height="8" rx="4"/><rect class="bar-fill bar-partial" width="26" height="8" rx="4"/></svg>
<p class="muted">Plan: Founding Free (demo). The quota resets weekly.</p></section>
<section class="panel" aria-labelledby="limits-title"><h2 id="limits-title">When a limit is reached</h2>
<div class="state-grid">
<article class="state state-quota"><span class="state-icon" aria-hidden="true">{quota[2]}</span><div><h3 class="state-name">{quota[1]}</h3><p>{e(quota[3])}</p></div></article>
<article class="state state-paused"><span class="state-icon" aria-hidden="true">{paused[2]}</span><div><h3 class="state-name">{paused[1]}</h3><p>{e(paused[3])}</p></div></article>
</div></section>
"""
    return console_page(title="Usage and quota", active="usage", body=body, nonce=nonce)


def settings_page(nonce: str) -> str:
    body = """
<div class="page-head"><h1>Settings</h1>
<p class="muted">A preview of research defaults. Nothing on this page is saved.</p></div>
<section class="panel" aria-labelledby="defaults-title"><h2 id="defaults-title">Research defaults (prototype)</h2>
<div class="form-grid">
<div><label class="field-label" for="set-depth">Default depth</label>
<select class="input" id="set-depth" name="set-depth"><option>Standard</option><option selected>Verified</option><option>Deep</option></select></div>
<div><label class="field-label" for="set-budget">Default budget cap (USD)</label>
<input class="input" type="number" id="set-budget" name="set-budget" min="0" step="0.01" value="0.50"></div>
</div>
<fieldset class="fieldset"><legend>Before research starts</legend><div class="choices">
<label class="choice"><input type="checkbox" id="set-clarify" name="set-clarify" checked> Always ask clarifying questions</label>
<label class="choice"><input type="checkbox" id="set-approve" name="set-approve" checked> Require scope approval</label>
</div></fieldset>
<button class="btn btn-primary" type="button" disabled>Save (unavailable in demo)</button>
</section>
<section class="panel" aria-labelledby="account-title"><h2 id="account-title">Account and data</h2>
<p>Export or delete your real account data on the <a href="/account">account page</a>.</p></section>
"""
    return console_page(title="Settings", active="settings", body=body, nonce=nonce)


PAGES = {
    "/app": overview_page,
    "/app/research/new": new_research_page,
    CASE_URL: case_page,
    "/app/receipts": receipts_page,
    "/app/library": library_page,
    "/app/usage": usage_page,
    "/app/settings": settings_page,
}
