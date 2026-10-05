"""User-facing onboarding and journey pages for the hosted MCP service.

These helpers are intentionally presentation-only. They do not grant access,
assert payment, or change entitlement state.
"""

from __future__ import annotations

import html


def landing_html(base: str) -> str:
    endpoint = html.escape(base.rstrip("/") + "/mcp")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lofgren Intelligence · Connect your AI client</title>
<style>
:root{{--bg:#090c11;--panel:#111722;--line:#263244;--text:#f4f7fb;--muted:#9eabba;--blue:#4c8dff;--green:#39c983;--amber:#e6b450}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a{{color:#95c0ff;text-decoration:none}}a:hover{{text-decoration:underline}}code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0c121a;border:1px solid var(--line);padding:3px 7px;border-radius:7px}}
.wrap{{max-width:1120px;margin:auto;padding:0 24px}}header{{position:sticky;top:0;border-bottom:1px solid #182130;background:rgba(9,12,17,.94);backdrop-filter:blur(12px);z-index:2}}
nav{{height:66px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-weight:800;letter-spacing:-.02em}}.nav{{display:flex;gap:18px;font-size:14px}}
.hero{{padding:82px 0 50px;display:grid;grid-template-columns:1.2fr .8fr;gap:44px;align-items:center}}.eyebrow{{display:inline-flex;align-items:center;gap:8px;border:1px solid #2e425d;background:#101824;border-radius:999px;padding:7px 11px;color:#c1ccda;font-size:13px}}.dot{{width:8px;height:8px;border-radius:50%;background:var(--green)}}
h1{{font-size:clamp(42px,7vw,72px);line-height:.98;letter-spacing:-.055em;margin:20px 0}}.lede{{font-size:20px;line-height:1.55;color:#b3becc;max-width:720px}}
.actions{{display:flex;gap:12px;flex-wrap:wrap;margin-top:28px}}.btn{{display:inline-flex;align-items:center;justify-content:center;min-height:46px;padding:0 17px;border:1px solid var(--line);border-radius:10px;font-weight:750}}.primary{{background:var(--blue);border-color:var(--blue);color:white}}.secondary{{background:var(--panel);color:white}}
.card,.step{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px}}.status{{display:flex;justify-content:space-between;gap:14px;padding:10px 0;border-bottom:1px solid #202a38;font-size:14px}}.status:last-child{{border-bottom:0}}.ok{{color:#70dda6}}
.section{{padding:44px 0}}.section h2{{font-size:32px;letter-spacing:-.03em;margin:0 0 10px}}.sectionlead{{color:var(--muted);max-width:800px;line-height:1.6;margin-bottom:22px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.step h3{{margin:8px 0}}.step p{{color:var(--muted);line-height:1.55}}.num{{font-size:12px;color:#8fb5ee;letter-spacing:.1em;text-transform:uppercase}}
.endpoint{{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#0d131c;border:1px solid var(--line);padding:15px;border-radius:12px;overflow:auto}}
.flow{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}}.flow div{{text-align:center;padding:14px 10px;border:1px solid var(--line);background:var(--panel);border-radius:12px;font-size:14px}}
.notice{{margin-top:24px;border-left:3px solid var(--amber);background:#17140d;color:#dccb9f;padding:16px 18px;border-radius:10px}}footer{{padding:40px 0 60px;color:#798798;font-size:14px}}
@media(max-width:820px){{.hero{{grid-template-columns:1fr;padding-top:52px}}.grid{{grid-template-columns:1fr}}.flow{{grid-template-columns:1fr 1fr}}.nav{{display:none}}}}
</style>
</head>
<body>
<header><div class="wrap"><nav><div class="brand">Lofgren Intelligence</div><div class="nav"><a href="#connect">Connect</a><a href="#journey">Journey</a><a href="/account">Account</a></div></nav></div></header>
<main class="wrap">
<section class="hero">
<div>
<span class="eyebrow"><span class="dot"></span> Certified V1–V6 governed intelligence stack</span>
<h1>Turn evidence into governed outcomes you can inspect.</h1>
<p class="lede">Connect Lofgren Intelligence to an MCP-capable AI client. Research and verify evidence, discover supported possibilities, build verified artifacts, authorize bounded actions, measure outcomes and evaluate improvements—with provenance and durable receipts connecting every stage.</p>
<div class="actions"><a class="btn primary" href="#connect">Connect an AI client</a><a class="btn secondary" href="/account">Account &amp; privacy</a></div>
</div>
<div class="card" aria-label="Current capability">
<div class="status"><span>Evidence Intelligence</span><strong class="ok">V1 certified</strong></div>
<div class="status"><span>Discovery Intelligence</span><strong class="ok">V2 certified</strong></div>
<div class="status"><span>Production Intelligence</span><strong class="ok">V3 certified</strong></div>
<div class="status"><span>Execution Intelligence</span><strong class="ok">V4 certified</strong></div>
<div class="status"><span>Outcome Intelligence</span><strong class="ok">V5 certified</strong></div>
<div class="status"><span>Improvement Intelligence</span><strong class="ok">V6 certified</strong></div>
<div class="status"><span>Founding access</span><strong>Accounts 1–1000</strong></div>
</div>
</section>

<section class="section" id="connect">
<h2>Connect in three steps</h2>
<p class="sectionlead">Your AI client discovers OAuth from the MCP endpoint, opens a Lofgren Intelligence consent screen, and receives scoped access. You never need to paste a service-role key into the client.</p>
<div class="endpoint"><span>MCP endpoint</span><code>{endpoint}</code></div>
<div class="grid" style="margin-top:16px">
<article class="step"><div class="num">Step 01</div><h3>Add the server</h3><p>Add the remote MCP endpoint to your MCP-capable AI client. The client should discover LI authorization metadata automatically.</p></article>
<article class="step"><div class="num">Step 02</div><h3>Review consent</h3><p>Sign in and authorize the named client for the <code>mcp</code> scope. The consent page identifies who is requesting access.</p></article>
<article class="step"><div class="num">Step 03</div><h3>Check access</h3><p>Start with <code>account_status</code> and <code>usage_status</code> so entitlement and quota are visible before expensive work begins.</p></article>
</div>
</section>

<section class="section" id="journey">
<h2>From objective to governed outcome</h2>
<p class="sectionlead">Users should not need to understand internal version numbers. The visible journey follows one governed chain. Each stage consumes typed state from the previous stage and produces its own receipt rather than silently changing history.</p>
<div class="flow"><div>Research</div><div>Discover</div><div>Build</div><div>Act</div><div>Measure / Improve</div></div>
<div class="grid" style="margin-top:16px">
<article class="step"><h3>Frame the work</h3><p>Use <code>compile_objective</code> or <code>plan_research</code> when the request needs a clearer objective, evidence requirements or cost boundary.</p></article>
<article class="step"><h3>Research and verify</h3><p><code>investigate</code> creates a durable run. Inspect findings, contradictions, gaps and provenance before treating a conclusion as supported.</p></article>
<article class="step"><h3>Discover possibilities</h3><p>Use <code>discover</code> from verified research state. Hypotheses stay hypotheses, simulations stay predictions and the discovery receipt preserves those distinctions.</p></article>
<article class="step"><h3>Build a verified artifact</h3><p><code>build_artifact</code> turns a selected V2 candidate into a deterministic V3 artifact with acceptance results, hashes, provenance and a production receipt.</p></article>
<article class="step"><h3>Authorize before acting</h3><p><code>propose_action</code> creates a V4 proposal. External execution stays blocked until you review the exact target and payload in the browser approval screen, then call <code>execute_action</code>.</p></article>
<article class="step"><h3>Measure and improve</h3><p><code>measure_outcome</code> compares expected with actual results. <code>evaluate_improvement</code> uses held-out evidence and can recommend review, but it never silently changes the running system.</p></article>
<article class="step"><h3>Recover from uncertainty</h3><p><strong>INSUFFICIENT_EVIDENCE</strong>, contradiction, failed acceptance, unauthorized action and insufficient measurement are valid governed outcomes—not UI failures.</p></article>
<article class="step"><h3>Retrieve the complete trail</h3><p>Research, discovery, production, action, outcome and improvement receipts can be retrieved again from durable tenant-scoped state.</p></article>
</div>
</section>

<section class="section">
<h2>Clear recovery states</h2>
<div class="grid">
<article class="step"><h3>Payment required</h3><p>Use <code>create_checkout</code>. Returning from checkout does not prove payment. Re-run <code>account_status</code> after the signed Stripe webhook updates entitlement.</p></article>
<article class="step"><h3>Quota reached</h3><p>Check <code>usage_status</code> instead of repeatedly retrying an expensive operation.</p></article>
<article class="step"><h3>Evidence missing</h3><p>Follow the returned gaps, evidence requirements and settling evidence. Unknown is a governed answer, not a UI failure.</p></article>
</div>
<div class="notice"><strong>Governance boundary:</strong> V1–V6 are certified as the bounded intelligence stack on this release line. Public readiness is a separate gate: hosted OAuth, tenant isolation, persistence, deployment identity, backup/restore, billing and real-client interoperability must still pass before this release is declared public-ready.</div>
</section>

<section class="section"><h2>Service links</h2><p><a href="/healthz">Health</a> · <a href="/readyz">Readiness</a> · <a href="/.well-known/oauth-authorization-server">OAuth metadata</a> · <a href="/account">Account &amp; privacy</a></p></section>
</main>
<footer><div class="wrap">Lofgren Intelligence · research → discovery → production → authorized action → measured outcome → reviewed improvement.</div></footer>
</body></html>"""


def consent_intro_html(client_name: str, scope: str = "mcp") -> str:
    client = html.escape(client_name or "Unknown MCP client")
    safe_scope = html.escape(scope or "mcp")
    return (
        '<div style="font-size:13px;color:#8fb5ee;margin-bottom:10px">OAuth authorization</div>'
        '<h1>Connect Lofgren Intelligence</h1>'
        f'<p><strong>{client}</strong> is requesting permission to use Lofgren Intelligence '
        f'through the <code>{safe_scope}</code> scope.</p>'
        '<p style="font-size:14px;color:#aeb8c7">This grants the client access to your authorized '
        'LI MCP tools and LI run state. It does not reveal your password or a server service-role key.</p>'
    )


def checkout_return_html() -> str:
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout return · Lofgren Intelligence</title>
<style>body{font-family:system-ui;background:#0b0d10;color:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh;padding:24px}main{max-width:620px;background:#151922;border:1px solid #2a3240;border-radius:18px;padding:32px}.muted{color:#aeb8c7}.note{background:#111722;border:1px solid #344054;border-radius:12px;padding:16px}a{color:#8bbcff}</style>
</head><body><main><h1>Checkout returned</h1><p class="muted">Returning here does not prove payment succeeded or that access has been granted.</p>
<div class="note"><strong>What happens next</strong><p>Lofgren Intelligence waits for Stripe's signed webhook to update your entitlement. Return to your AI client and call <code>account_status</code>. Continue only after the account reports active access.</p></div>
<p><a href="/">Back to Lofgren Intelligence</a></p></main></body></html>"""
