from collections import defaultdict
from decimal import Decimal

from .indicators import road_indicators
from .indicators_vendor import vendor_indicators

TAX_LABELS = {
    "before_tax": "before tax",
    "including_hst": "including HST",
    "net_hst_recoveries": "net of HST recoveries",
}


def citation_valid(citation, project):
    evidence = next((e for e in project["evidence"] if e["id"] == citation["evidence_id"]), None)
    if not evidence:
        return False
    page = next((p for p in evidence["pages"] if p["number"] == citation["page"]), None)
    normalize = lambda text: " ".join(text.split())
    return bool(page and normalize(citation["quote"]) in normalize(page["text"]))


def finding_key(finding):
    """Stable id for a finding across report runs so statuses and threads can follow it: indicators are keyed by
    title (one per indicator); AI findings by title plus the start of the explanation."""
    import hashlib
    seed = f"{finding.get('origin')}|{finding.get('title')}" + (f"|{(finding.get('explanation') or '')[:60]}" if finding.get("origin") == "ai" else "")
    return hashlib.sha1(seed.encode()).hexdigest()[:10]


def money(amount):
    return f"CAD {amount:,.2f}"


def record_citation(record):
    return {key: record[key] for key in ("evidence_id", "page", "quote")}


def analyze(project):
    findings, totals, gaps = [], [], []
    groups = defaultdict(list)
    for record in project["finances"]:
        groups[(record["scope"].casefold().strip(), record["basis"])].append(record)
    for (scope, basis), records in groups.items():
        basis_label = TAX_LABELS[basis]
        awards = [r for r in records if r["kind"] == "award"]
        payments = [r for r in records if r["kind"] == "payment"]
        amendments = [r for r in records if r["kind"] == "amendment"]
        if len(awards) != 1:
            gaps.append(f"{scope} ({basis_label}): enter exactly one original award to compare payments. "
                        "Multiple awards may be revisions or separate work.")
            continue
        authorized = Decimal(awards[0]["amount"]) + sum(
            (Decimal(r["amount"]) for r in amendments), Decimal(0)
        )
        paid = sum((Decimal(r["amount"]) for r in payments), Decimal(0))
        totals.append({"scope": scope, "basis": basis, "authorized": str(authorized), "paid": str(paid)})
        seen = defaultdict(list)
        for payment in payments:
            seen[(payment["reference"].strip().casefold(), payment["amount"])].append(payment)
        duplicates = [items for items in seen.values() if len(items) > 1]
        for items in duplicates:
            findings.append({
                "title": "Repeated payment reference",
                "explanation": f"Reference {items[0]['reference']} appears {len(items)} times for "
                               f"{money(Decimal(items[0]['amount']))} each.",
                "alternative_explanation": "The same payment may have been entered twice, or these may "
                                           "be separate legitimate transactions sharing a reference.",
                "next_step": "Reconcile entries against the payment ledger and bank transaction identifiers.",
                "citations": [record_citation(r) for r in items],
                "origin": "calculation", "priority": "review",
            })
        if payments and paid > authorized:
            findings.append({
                "title": "Recorded payments exceed recorded authorization",
                "explanation": f"For {scope} ({basis_label}), entered payments total {money(paid)} against "
                               f"{money(authorized)} in entered award and amendment records; "
                               f"the difference is {money(paid - authorized)}."
                               + (" Repeated references may inflate this total." if duplicates else ""),
                "alternative_explanation": "Missing amendments, misclassified tax treatment, cumulative "
                                           "payment certificates, or duplicate entries could explain this.",
                "next_step": "Obtain the complete amendment register and payment ledger. Confirm entries "
                             "are individual payments, not cumulative certificate totals.",
                "citations": [record_citation(r) for r in records],
                "origin": "calculation", "priority": "review",
            })
        if not payments:
            gaps.append(f"{scope} ({basis_label}): no actual payment entries; an award is not evidence of spending.")
    if len({r["basis"] for r in project["finances"]}) > 1:
        gaps.append("Multiple tax bases are present. Totals are kept separate; confirm the same basis before comparison.")
    if not project["finances"]:
        gaps.append("No sourced financial entries. Add the original award and actual payment records.")
    vendor_case = project.get("anchor") == "vendor"
    kinds = {e["kind"] for e in project["evidence"]}
    for kind, request in (VENDOR_RECORDS if vendor_case else ROAD_RECORDS):
        if kind not in kinds:
            gaps.append(request)
    if project["observations"]:
        gaps.append("Citizen condition reports are unverified. Obtain an engineering assessment and check "
                    "weather, truck traffic, drainage, and repair scope before attributing a cause.")
    indicator_findings, indicator_gaps = (vendor_indicators if vendor_case else road_indicators)(project)
    findings.extend(indicator_findings)
    gaps.extend(indicator_gaps)
    limitations = [
        "Checks cover only entered records; completeness is unknown.",
        "Amounts, tax bases, and scopes are entered by a person and have not been independently verified.",
        "A matching quotation confirms source text exists; it does not prove the interpretation is correct.",
        "Discrepancies and road damage do not establish corruption. No corruption probability is calculated.",
    ]
    if project.get("open_data") and vendor_case:
        limitations.append("Open-data indicators are statistical red flags used in procurement audits; each has common innocent "
                           "explanations and matches records by company name (exact or as a name prefix) across governments, so "
                           "name variants, subsidiaries and unrelated firms with similar names can be missed or over-matched - "
                           "check the 'Vendor name' field of each cited row.")
    elif project.get("open_data"):
        limitations.append("Open-data indicators are statistical red flags used in procurement audits; each has "
                           "common innocent explanations and matches records by street name, which can include "
                           "unrelated work on other segments of a long street.")
    return {"findings": findings, "totals": totals, "missing_records": gaps, "limitations": limitations}


ROAD_RECORDS = [
    ("contract", "Signed contract, scope of work, quantities, specifications, and original award."),
    ("amendment", "Complete amendment register, including confirmation if there were no changes."),
    ("payment", "Final payment ledger, invoices, and individual progress payments."),
    ("inspection", "Dated inspections, material tests, deficiency reports, and warranty repairs."),
    ("bid", "Bid submissions, bidder list, evaluation records, and engineer's estimate."),
]
VENDOR_RECORDS = [
    ("contract", "Signed contracts and statements of work for the largest awards (access-to-information request if not published)."),
    ("amendment", "Amendment approvals and justifications for every contract whose value grew after award."),
    ("payment", "Payment records from the departments' financial systems - disclosed contract values are commitments, not payments."),
    ("bid", "Sole-source justifications (Government Contracts Regulations s.6 exceptions), bid evaluation records and ACAN challenges."),
]
