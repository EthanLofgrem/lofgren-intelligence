"""Deterministic clarification layer for large Intelligence Case objectives.

A vague, consequential objective is not treated as a research request yet.
This module classifies the objective (domain, consequence level, breadth) with
explicit keyword and structure rules -- no model calls -- then asks a bounded
set of high-information, domain-appropriate questions, records answers
explicitly (including unknown/skip/default states), and produces a Case Charter
only when the objective is scoped enough to plan research responsibly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

UNKNOWN_VALUES = {"unknown", "i don't know", "i dont know", "skip", "later", "ask me later"}
DEFAULT_VALUES = {"use a reasonable default", "reasonable default", "default"}

# ---- Domains and consequence levels ------------------------------------------------------

DOMAIN_MEDICAL = "medical"
DOMAIN_FINANCIAL = "financial"
DOMAIN_LEGAL = "legal"
DOMAIN_SAFETY = "safety"
DOMAIN_DESIGN = "engineering_design"
DOMAIN_GENERAL = "general"
DOMAINS = (DOMAIN_MEDICAL, DOMAIN_FINANCIAL, DOMAIN_LEGAL, DOMAIN_SAFETY, DOMAIN_DESIGN, DOMAIN_GENERAL)

CONSEQUENCE_HIGH = "high"
CONSEQUENCE_STANDARD = "standard"

# The original engineering-design trigger, unchanged: a design verb/noun in a short objective.
_DESIGN_WORDS = {
    "design", "build", "create", "develop", "invent", "prototype", "architecture",
    "system", "device", "solution", "product",
}

_MEDICAL_WORDS = frozenset({
    "medical", "medicine", "medicines", "health", "healthcare", "clinical", "clinician", "clinicians",
    "patient", "patients", "disease", "diseases", "disorder", "disorders", "epilepsy", "epileptic",
    "seizure", "seizures", "cancer", "cancers", "tumor", "tumour", "diabetes", "dementia", "alzheimer",
    "alzheimer's", "parkinson", "parkinson's", "depression", "drug", "drugs", "therapy", "therapies",
    "therapeutic", "diagnosis", "diagnose", "diagnostic", "symptom", "symptoms", "dosing", "dose",
    "doses", "dosage", "vaccine", "vaccines", "pharmaceutical", "pharmacology", "surgery", "surgical",
    "hospital", "hospitals", "illness", "infection", "infections", "syndrome", "mortality", "morbidity",
    "neurology", "neurological", "psychiatric", "pediatric", "paediatric", "medication", "medications",
    "antiepileptic", "anticonvulsant", "chronic", "prognosis", "comorbidity", "comorbidities",
})
_MEDICAL_PHRASES = ("quality of life", "public health", "mental health", "health outcome", "side effect")

_FINANCIAL_WORDS = frozenset({
    "business", "businesses", "revenue", "revenues", "mrr", "arr", "profit", "profits", "profitable",
    "profitability", "income", "sales", "startup", "startups", "ecommerce", "shopify", "dropshipping",
    "dropship", "inventory", "pricing", "customers", "investment", "investments", "invest", "investing",
    "investor", "investors", "stock", "stocks", "portfolio", "loan", "loans", "debt", "monetize",
    "monetise", "saas", "subscription", "subscriptions", "valuation", "funding", "fundraise",
    "fundraising", "financial", "finance", "finances", "commercial", "margin", "margins", "storefront",
    "merchant", "retail", "wholesale", "buyer", "buyers", "paying", "customer", "acquisition",
    "churn", "retention", "monetization", "monetisation",
})
_FINANCIAL_PHRASES = ("recurring revenue", "cash flow", "e commerce", "business model", "unit economics")

_LEGAL_WORDS = frozenset({
    "legal", "law", "laws", "lawsuit", "lawsuits", "litigation", "regulation", "regulations",
    "regulatory", "regulator", "regulators", "compliance", "compliant", "statute", "statutes",
    "statutory", "contract", "contracts", "liability", "court", "courts", "attorney", "lawyer",
    "gdpr", "hipaa", "ccpa", "licensing", "patent", "patents", "trademark", "trademarks",
    "copyright", "jurisdiction", "legislation", "lawful", "unlawful", "enforcement", "sue", "sued",
    "suing", "landlord", "tenant", "tenants", "lease", "eviction", "evict", "divorce", "custody",
    "immigration", "visa", "probate", "defamation", "negligence", "indemnity",
})
_LEGAL_PHRASES = ("terms of service", "privacy policy", "case law")

_SAFETY_WORDS = frozenset({
    "safety", "hazard", "hazards", "hazardous", "infrastructure", "bridge", "bridges", "dam", "dams",
    "nuclear", "chemical", "chemicals", "toxic", "toxicity", "contamination", "contaminated",
    "contaminant", "contaminants", "drinking", "potable", "structural", "explosion", "explosive",
    "emergency", "evacuation", "flood", "flooding", "wildfire", "earthquake", "radiation", "pathogen",
    "pathogens", "outbreak",
})
_SAFETY_PHRASES = ("building code", "fire safety", "food safety", "power grid", "electrical grid")

# Public-impact words make any objective high-consequence.
_PUBLIC_IMPACT_WORDS = frozenset({
    "public", "community", "communities", "citizens", "residents", "municipal", "nationwide",
    "statewide", "populations", "villages",
})

_MONEY_RE = re.compile(
    r"[$€£]\s?\d"
    r"|\b\d[\d,.]*\s?(?:k|m|bn|thousand|million|billion)?\s?(?:usd|dollars?|eur|euros?|gbp|pounds?)\b",
    re.IGNORECASE,
)
_RANKING_RE = re.compile(r"\b(?:strongest|best|most effective|top|optimal|leading|highest[- ]impact)\b",
                         re.IGNORECASE)
_PRIORITY_RE = re.compile(
    r"\bpriorit(?:y|ies|ize|ise|ized|ised|izing|ising)\b|\bdeserves? (?:priority|attention|focus)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
# Operational verbs: the objective asks for something to be done, not only researched.
_OPERATIONAL_WORDS = frozenset({
    "launch", "buy", "purchase", "publish", "contact", "hire", "deploy", "spend", "execute", "operate",
    "sell", "advertise", "sign", "enroll", "administer", "prescribe",
})
# First-person context in a medical or legal objective (a request about one person's situation).
_PERSONAL_RE = re.compile(r"\b(?:i|i'm|i've|me|my|mine|we|our)\b", re.IGNORECASE)
# Named commerce platforms, used to make the channel question specific without leaking examples.
_PLATFORMS = ("shopify", "amazon", "etsy", "ebay", "woocommerce", "tiktok shop", "walmart marketplace")


@dataclass(frozen=True)
class ObjectiveProfile:
    """How the objective was classified, and by which explicit rules."""

    domain: str
    consequence: str
    broad: bool
    design_intent: bool
    domains_matched: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "consequence": self.consequence,
            "broad_scope": self.broad,
            "design_intent": self.design_intent,
            "domains_matched": list(self.domains_matched),
            "signals": list(self.signals),
        }


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _words(text: str) -> set[str]:
    return {w.strip(".,:;!?()[]{}").lower() for w in text.split() if w.strip()}


def _hits(tokens: set[str], joined: str, words: frozenset[str], phrases: tuple[str, ...]) -> bool:
    return bool(tokens & words) or any(f" {p} " in joined for p in phrases)


def classify_objective(objective: str) -> ObjectiveProfile:
    """Classify an objective by domain, consequence and breadth (deterministic rules only).

    Domain priority is medical > legal > financial > safety > engineering design > general.
    A design objective whose only specialised match is safety keeps the engineering-design
    bank (it already asks location, users, constraints and source conditions) at high
    consequence.
    """
    text = objective.strip()
    token_list = _tokens(text)
    tokens = set(token_list)
    joined = " " + " ".join(token_list) + " "
    signals: list[str] = []

    matched: list[str] = []
    if _hits(tokens, joined, _MEDICAL_WORDS, _MEDICAL_PHRASES):
        matched.append(DOMAIN_MEDICAL)
    if _hits(tokens, joined, _LEGAL_WORDS, _LEGAL_PHRASES):
        matched.append(DOMAIN_LEGAL)
    if _hits(tokens, joined, _FINANCIAL_WORDS, _FINANCIAL_PHRASES):
        matched.append(DOMAIN_FINANCIAL)
    if _hits(tokens, joined, _SAFETY_WORDS, _SAFETY_PHRASES):
        matched.append(DOMAIN_SAFETY)
    money = bool(_MONEY_RE.search(text))
    design_intent = bool(_words(text) & _DESIGN_WORDS)

    if DOMAIN_MEDICAL in matched:
        domain = DOMAIN_MEDICAL
    elif DOMAIN_LEGAL in matched:
        domain = DOMAIN_LEGAL
    elif DOMAIN_FINANCIAL in matched:
        domain = DOMAIN_FINANCIAL
    elif DOMAIN_SAFETY in matched and not design_intent:
        domain = DOMAIN_SAFETY
    elif design_intent:
        domain = DOMAIN_DESIGN
    else:
        domain = DOMAIN_GENERAL

    high = False
    if domain in (DOMAIN_MEDICAL, DOMAIN_LEGAL, DOMAIN_SAFETY):
        high = True
        signals.append(f"high_consequence_domain:{domain}")
    if DOMAIN_SAFETY in matched and domain != DOMAIN_SAFETY:
        high = True
        signals.append("safety_terms")
    if money:
        high = True
        signals.append("financial_money_target" if domain == DOMAIN_FINANCIAL else "money_amount")
    if tokens & _PUBLIC_IMPACT_WORDS:
        high = True
        signals.append("public_impact")

    broad = False
    if text.count("?") >= 2:
        broad = True
        signals.append("multiple_sub_questions")
    conjunctions = token_list.count("and") + token_list.count("or")
    if conjunctions >= 3 or "and/or" in text.lower():
        broad = True
        signals.append("conjunction_chain")
    if _RANKING_RE.search(text):
        broad = True
        signals.append("ranking")
    if _PRIORITY_RE.search(text):
        broad = True
        signals.append("prioritization")
    # An enumeration of several open dimensions ("which buyer, problem, offer and channel").
    if any(clause.count(",") >= 2 and re.search(r"\b(?:and|or)\b", clause)
           for clause in re.split(r"[.?!;]", text)):
        broad = True
        signals.append("enumerated_dimensions")

    if tokens & _OPERATIONAL_WORDS:
        signals.append("operational_execution_requested")
    if domain in (DOMAIN_MEDICAL, DOMAIN_LEGAL) and _PERSONAL_RE.search(text):
        signals.append("individual_advice_request")

    return ObjectiveProfile(
        domain=domain,
        consequence=CONSEQUENCE_HIGH if high else CONSEQUENCE_STANDARD,
        broad=broad,
        design_intent=design_intent,
        domains_matched=tuple(matched),
        signals=tuple(signals),
    )


# ---- Question banks ------------------------------------------------------------------------


@dataclass(frozen=True)
class ClarificationQuestion:
    key: str
    prompt: str
    why: str
    required: bool = True
    examples: tuple[str, ...] = ()
    default: str = ""


@dataclass
class ClarificationResult:
    status: str
    objective: str
    round: int
    questions: list[ClarificationQuestion] = field(default_factory=list)
    accepted_answers: dict[str, Any] = field(default_factory=dict)
    critical_unknowns: list[str] = field(default_factory=list)
    case_charter: dict[str, Any] | None = None
    domain: str = DOMAIN_GENERAL
    consequence: str = CONSEQUENCE_STANDARD
    classification: dict[str, Any] = field(default_factory=dict)
    safety_notice: str | None = None
    exclusions: list[str] = field(default_factory=list)


# The engineering-design bank (formerly the only bank).
BASE_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "location",
        "Where is this intended to be used or built?",
        "Location can change climate, regulations, available materials, logistics, source conditions, and what evidence is relevant.",
        examples=("country/region", "climate/terrain", "or 'unknown'"),
        default="No location assumed; only evidence that does not depend on location is treated as transferable.",
    ),
    ClarificationQuestion(
        "users",
        "Who will use, operate, or maintain it, and about how many people must it serve?",
        "Capacity, maintenance skill, accessibility, and operating model depend on the intended users.",
        examples=("households", "school or clinic", "village utility", "field team"),
        default="Assume non-specialist operators at small scale; no capacity claim is made.",
    ),
    ClarificationQuestion(
        "problem",
        "What specific problem must the system solve?",
        "A design cannot be evaluated correctly until the failure or need being addressed is explicit.",
        examples=("microbial contamination", "sediment", "chemicals", "salinity", "cost", "reliability"),
        default="Survey the main problem types without choosing one; the choice stays open.",
    ),
    ClarificationQuestion(
        "success",
        "What measurable outcome would count as success?",
        "Success criteria determine what evidence, design alternatives, and tests are actually relevant.",
        examples=("liters/day", "target removal", "operating life", "price target"),
        default="No success threshold is assumed; results report measured values only.",
    ),
    ClarificationQuestion(
        "cost",
        "What does 'low cost' mean here?",
        "An explicit cost boundary prevents LI from optimizing for a design that is technically plausible but economically unusable.",
        examples=("maximum installed cost", "cost per liter", "monthly operating cost"),
        default="Costs are reported as found; no cost ceiling is assumed to be met.",
    ),
    ClarificationQuestion(
        "constraints",
        "What constraints matter most?",
        "Constraints can change the feasible design space before research begins.",
        examples=("no grid electricity", "local repair only", "limited replacement parts", "transport limits"),
        default="Assume the most restrictive common constraints: no grid power and locally repairable parts.",
    ),
    ClarificationQuestion(
        "source_or_environment",
        "What source, operating environment, or starting conditions should LI assume?",
        "Input conditions determine which mechanisms are relevant and which claims can safely transfer.",
        examples=("water source", "existing system", "available data", "environmental conditions"),
        default="Assume worst-case plausible starting conditions; nothing about the environment is treated as known.",
    ),
)
DESIGN_QUESTIONS = BASE_QUESTIONS

MEDICAL_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "purpose",
        "What is this research for: general education, a research project, product development, or "
        "preparing questions to discuss with a clinician? (LI does not support decisions about an "
        "individual's care.)",
        "The purpose sets the depth, the evidence bar and the framing; individual diagnosis or treatment "
        "decisions belong with a clinician and are out of scope.",
        examples=("education", "research project", "product development", "clinician-discussion prep"),
        default="General education; no individual-care framing.",
    ),
    ClarificationQuestion(
        "population",
        "Which population should the research cover: adults, children, or both?",
        "Evidence, risks and approved options often differ sharply between adults and children.",
        examples=("adults", "children", "both"),
        default="Both, reported separately; findings are never transferred between age groups.",
    ),
    ClarificationQuestion(
        "focus",
        "Which condition subtype or treatment problem should LI focus on?",
        "A named subtype or problem determines which studies are relevant and avoids averaging over "
        "groups that respond differently.",
        examples=("a specific subtype", "treatment resistance", "side-effect burden", "access to care"),
        default="The condition as a whole, with subtypes reported separately where studies distinguish them.",
    ),
    ClarificationQuestion(
        "emphasis",
        "What should the research emphasize: established care, emerging approaches, prevention, or "
        "research gaps?",
        "Established care and emerging research need different evidence standards and are easy to conflate.",
        examples=("established care", "emerging approaches", "prevention", "research gaps"),
        default="Established care first; emerging approaches labelled as preliminary.",
    ),
    ClarificationQuestion(
        "evidence_types",
        "Which evidence types should count, and should preclinical (animal or laboratory) studies be included?",
        "The evidence types allowed determine how strong each conclusion can be.",
        examples=("systematic reviews and trials", "clinical guidelines", "observational studies",
                  "include or exclude preclinical"),
        default="Human studies only (systematic reviews, trials, guidelines); preclinical excluded.",
    ),
    ClarificationQuestion(
        "evidence_cutoff",
        "What publication date range should the evidence cover?",
        "Medical evidence changes quickly; a cutoff makes clear what the findings can and cannot reflect.",
        examples=("last 5 years", "last 10 years", "everything up to a given date"),
        default="The last 10 years, with older landmark studies labelled by date.",
    ),
    ClarificationQuestion(
        "depth_budget",
        "How deep should the research go, and what time, source access and budget apply?",
        "Depth and source access decide whether a quick overview or a full evidence review is feasible.",
        examples=("quick overview", "full evidence review", "open-access sources only", "max spend"),
        default="A focused overview from open-access sources within the case budget.",
    ),
)

FINANCIAL_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "purpose",
        "What decision will this research support, and for whom?",
        "The decision determines which options are comparable and what counts as a defensible answer.",
        examples=("choose a business to launch", "compare opportunities", "investor or partner briefing"),
        default="Inform an initial go/no-go comparison only; no commitment is implied.",
    ),
    ClarificationQuestion(
        "baseline_assets",
        "What do you already have to start with: audience, email list, brand, capital, skills or suppliers?",
        "Existing assets change which opportunities are realistic and how fast revenue can start.",
        examples=("no audience", "an email list of a given size", "an existing brand", "capital available"),
        default="Assume a cold start: no audience, list, brand or prior customers.",
    ),
    ClarificationQuestion(
        "revenue_definition",
        "How should revenue be defined: one-time sales or monthly recurring revenue, gross or net, and "
        "over what timeframe?",
        "Revenue targets are meaningless until the definition and time window are fixed.",
        examples=("MRR by day 30", "gross sales in the first 90 days", "net profit per month"),
        default="Net monthly recurring revenue by the stated date; one-time sales reported separately.",
    ),
    ClarificationQuestion(
        "launch_budget",
        "What launch budget is allowed, and is any spending authorized?",
        "Budget limits the feasible options, and spending must never happen without explicit authorization.",
        examples=("$0, research only", "up to a stated amount", "each spend needs approval"),
        default="Research only: no spending is authorized.",
    ),
    ClarificationQuestion(
        "channel_constraints",
        "Which channels and platforms are required or ruled out (for example, must {platform} be used, or is "
        "it only preferred)?",
        "Platform requirements and channel rules narrow the business models that qualify.",
        examples=("{platform} required", "{platform} preferred", "no paid ads", "organic only"),
        default="Named platforms are treated as required; no other channel is assumed available.",
    ),
    ClarificationQuestion(
        "risk_and_hours",
        "What risk tolerance do you have, and how many hours per week can the owner commit?",
        "Risk tolerance and available time decide which opportunities are actually operable.",
        examples=("low risk", "moderate risk", "10 hours/week", "full time"),
        default="Low risk tolerance and 10 owner hours per week.",
    ),
    ClarificationQuestion(
        "evidence_standard",
        "What evidence standard applies: real demand evidence (sales, search volume, competitors' revenue) or "
        "reasoned assumptions?",
        "Separating observed demand from assumptions keeps projections honest.",
        examples=("real demand evidence only", "assumptions allowed if labelled"),
        default="Real demand evidence required; any assumption is labelled as an assumption.",
    ),
)

LEGAL_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "jurisdiction",
        "Which jurisdiction(s) apply?",
        "Law differs by country, state and sometimes city; findings do not transfer between jurisdictions.",
        examples=("country", "state/province", "EU", "multiple"),
        default="No jurisdiction assumed; findings are reported per jurisdiction found.",
    ),
    ClarificationQuestion(
        "parties",
        "What kinds of parties are involved (by role, not by name)?",
        "Obligations depend on the roles involved, such as business, consumer, employer or regulator.",
        examples=("small business", "consumer", "employer and employee", "platform and seller"),
        default="A generic small business; no specific party.",
    ),
    ClarificationQuestion(
        "purpose",
        "What is this research for? (LI provides research, not legal advice.)",
        "The purpose sets the depth and framing; decisions with legal effect need a qualified lawyer.",
        examples=("general understanding", "preparing questions for a lawyer", "compliance research"),
        default="General understanding to prepare questions for a qualified lawyer.",
    ),
    ClarificationQuestion(
        "time",
        "What time period matters: current law only, or law as of a specific date?",
        "Laws and regulations change; an effective date keeps the findings accurate.",
        examples=("current law", "as of a given date", "including pending changes"),
        default="Current law as of the research date, with pending changes flagged.",
    ),
    ClarificationQuestion(
        "sources",
        "Which sources should count: statutes and regulations, regulator guidance, case law, or secondary commentary?",
        "Primary law and secondary commentary carry very different weight.",
        examples=("primary law only", "regulator guidance", "case law", "secondary commentary allowed"),
        default="Primary law and official regulator guidance; commentary labelled as secondary.",
    ),
)

SAFETY_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "jurisdiction",
        "Which jurisdiction or location applies?",
        "Safety requirements, codes and enforcement differ by jurisdiction.",
        examples=("country", "state/province", "city", "site type"),
        default="No jurisdiction assumed; requirements are reported per jurisdiction found.",
    ),
    ClarificationQuestion(
        "standards",
        "Which standards or codes must be met or compared?",
        "Naming the standards fixes what counts as compliant and which tests are relevant.",
        examples=("national building code", "ISO/IEC standard", "regulator guidance", "unknown"),
        default="The strictest widely recognized standard found, labelled as such.",
    ),
    ClarificationQuestion(
        "affected_population",
        "Who could be affected, and roughly how many people?",
        "The affected population sets the acceptable risk and which harms matter most.",
        examples=("workers on site", "residents", "a whole town", "vulnerable groups"),
        default="Assume a vulnerable general population; no risk is treated as acceptable by default.",
    ),
    ClarificationQuestion(
        "purpose",
        "What decision will this research support?",
        "The decision defines the scope and keeps findings from being read as a safety sign-off.",
        examples=("background briefing", "options comparison", "preparing for an expert review"),
        default="Background briefing for qualified experts; no safety decision is made.",
    ),
)

GENERAL_QUESTIONS: tuple[ClarificationQuestion, ...] = (
    ClarificationQuestion(
        "purpose",
        "What is this research for, and what decision or output will it feed?",
        "The purpose sets the depth and which findings matter most.",
        examples=("learning", "a report", "a decision", "a publication"),
        default="General understanding; no decision is implied.",
    ),
    ClarificationQuestion(
        "scope",
        "Which parts of the question matter most, and what is out of scope?",
        "A broad objective has several sub-questions; ranking them prevents shallow coverage of everything.",
        examples=("one sub-question first", "all equally", "exclude a named area"),
        default="Cover each sub-question at equal, shallow depth and flag where more depth is needed.",
    ),
    ClarificationQuestion(
        "time_cutoff",
        "What time period should the evidence cover?",
        "A time window makes clear what the findings can and cannot reflect.",
        examples=("last 5 years", "last 10 years", "all time", "up to a given date"),
        default="The last 10 years, with older sources labelled by date.",
    ),
    ClarificationQuestion(
        "sources",
        "Which sources should count, and which should be excluded?",
        "Source rules determine how strong and how checkable the conclusions are.",
        examples=("peer-reviewed only", "official statistics", "industry reports", "open-access only"),
        default="Primary and peer-reviewed sources; secondary sources labelled as such.",
    ),
    ClarificationQuestion(
        "depth_budget",
        "How deep should the research go, and what time and budget apply?",
        "Depth decides whether a quick overview or a full review is feasible within the budget.",
        examples=("quick overview", "full review", "max spend"),
        default="A focused overview within the case budget.",
    ),
)

QUESTION_BANKS: dict[str, tuple[ClarificationQuestion, ...]] = {
    DOMAIN_MEDICAL: MEDICAL_QUESTIONS,
    DOMAIN_FINANCIAL: FINANCIAL_QUESTIONS,
    DOMAIN_LEGAL: LEGAL_QUESTIONS,
    DOMAIN_SAFETY: SAFETY_QUESTIONS,
    DOMAIN_DESIGN: DESIGN_QUESTIONS,
    DOMAIN_GENERAL: GENERAL_QUESTIONS,
}

SAFETY_NOTICES: dict[str, str] = {
    DOMAIN_MEDICAL: (
        "Lofgren Intelligence does not diagnose, treat or give medical advice; it summarizes published "
        "evidence for education and research. Decisions about anyone's care belong with a qualified "
        "clinician. Do not share identifiable patient information."
    ),
    DOMAIN_FINANCIAL: (
        "Lofgren Intelligence does not give personalized financial, investment, legal or tax advice and "
        "does not guarantee revenue; projections are research estimates, not promises. No money is spent "
        "and no commitment is made on your behalf."
    ),
    DOMAIN_LEGAL: (
        "Lofgren Intelligence provides legal research, not legal advice, and creates no lawyer-client "
        "relationship. Decisions with legal effect need a qualified lawyer in the relevant jurisdiction."
    ),
    DOMAIN_SAFETY: (
        "Lofgren Intelligence findings are research, not a safety certification or code-compliance "
        "sign-off. Safety decisions need qualified professionals and the responsible authority."
    ),
}

# Appended to every domain notice: a notice is information, never a substitute for the process.
NOTICE_SCOPE = (
    " This notice does not replace clarification, evidence verification or the account owner's "
    "approval of the Case Charter."
)
SAFETY_NOTICES = {k: v + NOTICE_SCOPE for k, v in SAFETY_NOTICES.items()}

# Research is not execution: every charter authorizes research only.
RESEARCH_ONLY = (
    "This Case Charter authorizes research only. Carrying out anything the research recommends "
    "(spending, publishing, contacting people, treatment, filings or other real-world actions) is "
    "operational execution outside this charter and needs its own explicit authorization."
)

_GENERIC_EXCLUSIONS: tuple[str, ...] = (
    "Do not claim real-world performance without appropriate testing.",
    "Do not silently convert an unknown or assumption into a verified fact.",
    "Do not perform consequential external actions without explicit authorization.",
)

DOMAIN_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    DOMAIN_MEDICAL: (
        "No diagnosis or individual treatment advice.",
        "No dosing or treatment instructions.",
        "No patient contact.",
        "No collection or use of private medical data or identifiable patient information.",
    ),
    DOMAIN_FINANCIAL: (
        "No spending without explicit approval.",
        "No publishing (stores, listings, ads or posts) without explicit approval.",
        "No customer contact without explicit approval.",
        "No supplier commitments without explicit approval.",
        "No personalized investment advice or revenue guarantee.",
    ),
    DOMAIN_LEGAL: (
        "No legal advice or representation.",
        "No filings or contact with parties, courts or authorities without explicit approval.",
    ),
    DOMAIN_SAFETY: (
        "No safety certification or code-compliance sign-off.",
        "No contact with authorities, operators or the public without explicit approval.",
    ),
}


def question_bank(domain: str) -> tuple[ClarificationQuestion, ...]:
    return QUESTION_BANKS.get(domain, GENERAL_QUESTIONS)


def exclusions_for(domain: str, signals: tuple[str, ...] = ()) -> list[str]:
    out = list(DOMAIN_EXCLUSIONS.get(domain, ()))
    if "operational_execution_requested" in signals:
        out.append("The objective names operational actions; this case researches them and executes none of them.")
    if "individual_advice_request" in signals:
        out.append("No advice about the requester's own situation; findings are general research only.")
    return out + list(_GENERIC_EXCLUSIONS)


def _specialise(bank: tuple[ClarificationQuestion, ...], objective: str) -> tuple[ClarificationQuestion, ...]:
    """Fill the {platform} slot from the objective, or a neutral phrase -- never another case's example."""
    low = objective.lower()
    platform = next((p for p in _PLATFORMS if re.search(r"\b" + re.escape(p) + r"\b", low)), None)
    name = platform.title() if platform else "a named platform"
    out = []
    for q in bank:
        if "{platform}" in q.prompt or any("{platform}" in e for e in q.examples):
            q = replace(q, prompt=q.prompt.replace("{platform}", name),
                        examples=tuple(e.replace("{platform}", name) for e in q.examples))
        out.append(q)
    return tuple(out)


def requires_clarification(objective: str) -> bool:
    """Clarify any high-consequence or broad objective, and any short design objective.

    Simple factual questions (standard consequence, one narrow question, no design intent)
    skip clarification.
    """
    profile = classify_objective(objective)
    if profile.consequence == CONSEQUENCE_HIGH or profile.broad:
        return True
    # The original engineering-design rule, unchanged.
    return profile.design_intent and len(_words(objective)) < 40


def _normalize_answers(answers: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (answers or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            v = value.strip()
            if not v:
                continue
            low = v.lower()
            if low in UNKNOWN_VALUES:
                out[key] = {"state": "unknown", "value": None}
            elif low in DEFAULT_VALUES:
                out[key] = {"state": "default_requested", "value": None}
            else:
                out[key] = {"state": "answered", "value": v}
        else:
            out[key] = {"state": "answered", "value": value}
    return out


def _missing(
    answers: dict[str, Any], bank: tuple[ClarificationQuestion, ...] = BASE_QUESTIONS,
) -> list[ClarificationQuestion]:
    missing: list[ClarificationQuestion] = []
    for q in bank:
        state = answers.get(q.key, {}).get("state")
        if state not in {"answered", "unknown", "default_requested"}:
            missing.append(q)
    return missing


def _applied_defaults(normalized: dict[str, Any], bank: tuple[ClarificationQuestion, ...]) -> dict[str, Any]:
    """Each 'use a reasonable default' answer becomes an explicit, conservative, recorded default."""
    by_key = {q.key: q for q in bank}
    out: dict[str, Any] = {}
    for key, item in normalized.items():
        if item.get("state") != "default_requested":
            continue
        q = by_key.get(key)
        out[key] = {
            "value": q.default if q and q.default else "No assumption: treated as unspecified.",
            "recorded_as": "default",
            "verified": False,
        }
    return out


def clarify_objective(
    objective: str,
    answers: dict[str, Any] | None = None,
    *,
    max_first_round: int = 7,
) -> ClarificationResult:
    text = objective.strip()
    if not text:
        raise ValueError("objective is empty")

    profile = classify_objective(text)
    bank = _specialise(question_bank(profile.domain), text)
    safety_notice = SAFETY_NOTICES.get(profile.domain)
    exclusions = exclusions_for(profile.domain, profile.signals)
    common: dict[str, Any] = {
        "domain": profile.domain,
        "consequence": profile.consequence,
        "classification": profile.to_dict(),
        "safety_notice": safety_notice,
        "exclusions": exclusions,
    }

    normalized = _normalize_answers(answers)
    if not requires_clarification(text):
        charter = {
            "objective": text,
            "status": "ready_for_scope_approval",
            "domain": profile.domain,
            "consequence": profile.consequence,
            "safety_notice": safety_notice,
            "authorizes": "research_only",
            "research_only": RESEARCH_ONLY,
            "exclusions": exclusions,
            "answers": normalized,
            "critical_unknowns": [],
            "approval_required_before_research": True,
        }
        return ClarificationResult(
            "READY_FOR_SCOPE_APPROVAL", text, 0,
            accepted_answers=normalized, case_charter=charter, **common,
        )

    missing = _missing(normalized, bank)
    if missing:
        round_no = 1 if not normalized else 2
        # Ask no more than 3-7 questions; preserve deterministic high-information order.
        qs = missing[: max(3, min(max_first_round, 7))]
        critical_unknowns = [
            key for key, item in normalized.items() if item.get("state") == "unknown"
        ]
        return ClarificationResult(
            "CLARIFICATION_REQUIRED",
            text,
            round_no,
            questions=qs,
            accepted_answers=normalized,
            critical_unknowns=critical_unknowns,
            **common,
        )

    unknowns = [
        key for key, item in normalized.items() if item.get("state") == "unknown"
    ]
    defaults = [
        key for key, item in normalized.items() if item.get("state") == "default_requested"
    ]
    charter = {
        "objective": text,
        "status": "ready_for_scope_approval",
        "domain": profile.domain,
        "consequence": profile.consequence,
        "safety_notice": safety_notice,
        "authorizes": "research_only",
        "research_only": RESEARCH_ONLY,
        "decision": "Define the exact decision before substantial research begins.",
        "scope": {
            key: item.get("value")
            for key, item in normalized.items()
            if item.get("state") == "answered"
        },
        "critical_unknowns": unknowns,
        "defaults_requested": defaults,
        "defaults_applied": _applied_defaults(normalized, bank),
        "exclusions": exclusions,
        "excluded_scope": exclusions,
        "approval_required_before_research": True,
    }
    return ClarificationResult(
        "READY_FOR_SCOPE_APPROVAL",
        text,
        2,
        accepted_answers=normalized,
        critical_unknowns=unknowns,
        case_charter=charter,
        **common,
    )


def to_dict(result: ClarificationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "objective": result.objective,
        "round": result.round,
        "domain": result.domain,
        "consequence": result.consequence,
        "classification": result.classification,
        "safety_notice": result.safety_notice,
        "exclusions": list(result.exclusions),
        "questions": [
            {
                "key": q.key,
                "question": q.prompt,
                "why_it_matters": q.why,
                "required": q.required,
                "examples": list(q.examples),
            }
            for q in result.questions
        ],
        "accepted_answers": result.accepted_answers,
        "critical_unknowns": result.critical_unknowns,
        "case_charter": result.case_charter,
    }
