"""Deterministic indicators for a supplier case: plain code over the federal, provincial and municipal rows pulled for one
company. Each is a lead with an innocent explanation and a next step, quoting the dataset documents; none is a conclusion."""
import re
from collections import Counter, defaultdict
from datetime import date

from .indicators import _cite, _dataset, _finding, _money
from .opendata.canada import committed
from .opendata.common import fold

SOLE_SOURCE_CEILING = 40_000   # Government Contracts Regulations s.6(b): competition may be waived below this amount
SPLIT_WINDOW_DAYS = 90
NUMBERED = re.compile(r"^\d{6,8}\s+(CANADA|ONTARIO|QUEBEC|QUÉBEC|ALBERTA|B\.?C\.?|MANITOBA|N\.?B\.?|N\.?S\.?)\b", re.I)


def _iso(text):
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def _originals(data):
    return [r for r in data["records"] if not r.get("is_amendment") and r.get("date")]


def _value(record):
    return record.get("original_value") or record.get("value") or 0


def _window(data):
    """' (among the N most recent rows kept on file)' when the case keeps only part of a large dataset."""
    total, kept = data.get("total") or data.get("count") or len(data["records"]), len(data["records"])
    return f" (among the {kept} most recent of {total} rows kept for checks)" if total > kept else ""


def _rows(project, key):
    return (_dataset(project, key) or {"records": []})["records"]


def contract_splitting(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    by_department = defaultdict(list)
    for r in _originals(data):
        if 0 < _value(r) < SOLE_SOURCE_CEILING and _iso(r["date"]):
            by_department[r.get("department") or "Unknown department"].append(r)
    best = None
    for department, rows in by_department.items():
        rows.sort(key=lambda r: r["date"])
        for i, start in enumerate(rows):
            cluster = [r for r in rows[i:] if (_iso(r["date"]) - _iso(start["date"])).days <= SPLIT_WINDOW_DAYS]
            if len(cluster) >= 3 and (best is None or len(cluster) > len(best[1])):
                best = (department, cluster)
    if not best:
        return None
    department, cluster = best
    total = sum(_value(r) for r in cluster)
    near = sum(1 for r in cluster if _value(r) >= 0.8 * SOLE_SOURCE_CEILING)
    return _finding(
        "Several small contracts from one department within a short period",
        f"{department} published {len(cluster)} separate contracts with the supplier between {cluster[0]['date']} and {cluster[-1]['date']}, "
        f"each under the {_money(SOLE_SOURCE_CEILING)} threshold below which competition may be waived, totalling {_money(total)}"
        + (f"; {near} of them within 20% of the threshold" if near else "") + f".{_window(data)}",
        "Departments legitimately issue many small task-based contracts to the same firm (call-ups against a standing offer, distinct "
        "deliverables, different branches buying independently); the regulations only prohibit splitting a single requirement.",
        "Ask the department which requirement each contract met and whether they share a statement of work or project. Contracts that "
        "together exceed the threshold for one requirement should have been competed.",
        [_cite(data, project, f"Reference: {r['reference']}") for r in cluster[:3] if r.get("reference")])


def amendment_creep(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    groups = defaultdict(list)
    for r in data["records"]:
        if r.get("contract_number"):
            groups[(r.get("department"), r["contract_number"])].append(r)
    grown = []
    for (department, number), rows in groups.items():
        amendments = [r for r in rows if r.get("is_amendment")]
        original = next((r["original_value"] for r in rows if r.get("original_value")), None) or min((r["value"] for r in rows if r.get("value")), default=0)
        final = max((r.get("value") or 0 for r in rows), default=0)
        if amendments and original and final >= 1.5 * original:
            grown.append((department, number, original, final, len(amendments), rows))
    if not grown:
        return None
    grown.sort(key=lambda g: g[3] - g[2], reverse=True)
    department, number, original, final, n, rows = grown[0]
    added = sum(g[3] - g[2] for g in grown)
    return _finding(
        "Contracts that grew well beyond their original value through amendments",
        f"{len(grown)} contract(s) with the supplier ended at least 50% above their original value; the largest, {number} with {department}, "
        f"went from {_money(original)} to {_money(final)} through {n} amendment(s). Together these contracts added {_money(added)} after award.{_window(data)}",
        "Options exercised as planned, multi-year extensions reported as amendments, scope added at the department's request or price "
        "adjustments allowed by the contract all raise the value without any irregularity.",
        "Request the amendment approvals and the original statement of work; compare the final deliverables with what was competed. A contract "
        "that doubled without a new competition is a question for the department's procurement review board.",
        [_cite(data, project, f"Reference: {r['reference']}") for r in sorted(rows, key=lambda r: r.get("date") or "") if r.get("reference")][:3])


def non_competitive_share(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    originals = [r for r in data["records"] if not r.get("is_amendment") and r.get("competitive") is not None]
    if len(originals) < 3:
        return None
    non = [r for r in originals if not r["competitive"]]
    value_all, value_non = sum(committed(r) for r in originals) or 1, sum(committed(r) for r in non)
    if len(non) / len(originals) < 0.5 and value_non / value_all < 0.5:
        return None
    reasons = Counter(r.get("tendering_reason") or "not stated" for r in non)
    procedures = Counter(r.get("procedure") for r in non if r.get("procedure"))
    sample = max(non, key=committed)
    return _finding(
        "Most of the supplier's federal contracts were awarded without competition",
        f"{len(non)} of {len(originals)} contracts ({round(100 * value_non / value_all)}% of {_money(value_all)} by value) were awarded as "
        f"{' or '.join(procedures)}; limited-tendering reasons given: " + ", ".join(f"{k} ({n})" for k, n in reasons.most_common(4)) + f".{_window(data)}",
        "Sole-source awards are lawful below the regulatory threshold, for exclusive rights or urgency, and an Advance Contract Award Notice "
        "is a public notice other suppliers could have challenged; specialised firms are often the only qualified source.",
        "Request the sole-source justifications (Government Contracts Regulations s.6 exceptions) for the largest awards and check whether "
        "any ACAN drew statements of capability that were set aside.",
        [_cite(data, project, f"Reference: {sample['reference']}")] + [_cite(data, project, f"Procedure | {p} |") for p in list(procedures)[:1]])


def single_bid_share(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    competed = [r for r in data["records"] if not r.get("is_amendment") and r.get("competitive") and r.get("bids") is not None]
    single = [r for r in competed if r["bids"] == 1]
    if len(single) < 3 or len(single) / len(competed) < 0.4:
        return None
    sample = max(single, key=committed)
    return _finding(
        "Competitive tenders that drew a single bid",
        f"{len(single)} of {len(competed)} competitive contracts with the supplier report exactly one bid, e.g. {sample['reference']} "
        f"({_money(committed(sample))}, {sample.get('department')}).{_window(data)}",
        "Niche requirements, short solicitation periods and incumbent advantage routinely produce one bid; the department may still award "
        "after checking the price against its estimate.",
        "Ask for the solicitation period, the number of firms that downloaded the documents and the price-support analysis for these awards.",
        [_cite(data, project, f"Reference: {r['reference']}") for r in sorted(single, key=committed, reverse=True)[:3]])


def year_end_clustering(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    dated = [r for r in _originals(data) if _iso(r["date"])]
    if len(dated) < 6:
        return None
    march = [r for r in dated if _iso(r["date"]).month == 3]
    if len(march) / len(dated) < 0.3:
        return None
    return _finding(
        "Contracts cluster in March, the last month of the federal fiscal year",
        f"{len(march)} of {len(dated)} contracts with the supplier ({round(100 * len(march) / len(dated))}%, {_money(sum(committed(r) for r in march))}) "
        f"are dated in March, against about 8% if awards were spread evenly over the year.{_window(data)}",
        "Year-end spending is common and lawful: departments commit remaining budgets before they lapse, and many services renew on a fiscal-year cycle.",
        "Check whether the March contracts were delivered in the following fiscal year and whether their requirements existed earlier; the "
        "Procurement Ombud and the Auditor General have both reviewed year-end spending patterns.",
        [_cite(data, project, f"Contract date: {r['date']}") for r in sorted(march, key=committed, reverse=True)[:3]])


def _institution_matches(institution, department):
    """Loose match between a lobbying institution and a contracting department (both free text, names change over time)."""
    a, b = fold(institution), fold(department)
    if not a or not b:
        return False
    generic = {"canada", "department", "ministry", "government", "agency", "services", "office", "canadian", "national", "affairs"}
    words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s) if w not in generic}
    wa, wb = words(a), words(b)
    return a in b or b in a or (bool(wa and wb) and len(wa & wb) >= max(1, min(len(wa), len(wb)) // 2))


def lobbying_before_award(project):
    lobby, contracts = _dataset(project, "fed_lobbying"), _dataset(project, "fed_contracts")
    if not lobby or not contracts or not lobby["records"]:
        return None
    pairs = []
    for c in _originals(contracts):
        award = _iso(c["date"])
        for l in lobby["records"]:
            when = _iso(l.get("date"))
            if award and when and 0 <= (award - when).days <= 365 and any(
                    _institution_matches(i, c.get("department") or "") for i in (l.get("institutions") or "").split("; ")):
                pairs.append((l, c, (award - when).days))
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[2])
    l, c, days = pairs[0]
    departments = Counter(c.get("department") for _, c, _ in pairs)
    contracts_hit = {c.get("reference") for _, c, _ in pairs}
    return _finding(
        "Lobbying communications with a department in the year before it awarded the supplier a contract",
        f"{len(pairs)} communication report(s) name officials of departments that awarded the supplier {len(contracts_hit)} contract(s) within the "
        f"following 12 months ({', '.join(f'{d} ({n})' for d, n in departments.most_common(3))}). Closest: a communication on {l['date']} with "
        f"{(l.get('officials') or 'officials')[:120]} at {l.get('institutions')}, {days} days before contract {c.get('reference')} ({_money(committed(c))}).",
        "Registered lobbying is lawful and routine for firms selling to government; communications often concern policy or other business lines, "
        "and the officials met may have had no role in the award.",
        "Compare the subjects of the communications with the contract's requirement, and check whether any official named sat in the branch that "
        "signed it (the buyer named on the contract row).",
        [_cite(lobby, project, f"Report: {l['report']}") if l.get("report") else _cite(lobby, project, f"Communication date: {l['date']}"),
         _cite(contracts, project, f"Reference: {c['reference']}") if c.get("reference") else None])


def barred_while_paid(project):
    listings = [(d, kind, r) for key, kind in (("fed_ineligible", "PSPC"), ("qc_debarment", "RENA"))
                for d in [_dataset(project, key)] if d for r in d["records"]]
    if not listings:
        return None
    data, kind, r = listings[0]
    start, end = r.get("from") or r.get("registered") or "", r.get("until") or r.get("ends") or ""
    inside = []
    for key, field, label in (("fed_contracts", "date", "federal contract"), ("city_awards", "award_date", "municipal award")):
        d = _dataset(project, key)
        for row in (d or {"records": []})["records"]:
            when = (row.get(field) or "")[:10]
            if start and when and start <= when <= (end or "9999"):
                inside.append((d, row, label))
    paid = [row for row in _rows(project, "on_payments") if start and row.get("fiscal_year") and start[:4] <= row["fiscal_year"][:4] <= (end or "9999")[:4]]
    listed = "PSPC's federal Ineligibility and Suspension List" if kind == "PSPC" else "Québec's RENA register of companies barred from public contracts"
    first = inside[0] if inside else None
    detail = (f" {len(inside)} contract or award row(s) in this case are dated inside that period, e.g. the {first[2]} "
              f"{first[1].get('reference') or first[1].get('document_number')} on {(first[1].get('date') or first[1].get('award_date') or '')[:10]}."
              if inside else " No contract or award row on file is dated inside the period.")
    if paid:
        detail += f" Ontario's Public Accounts record {_money(sum(x['amount'] for x in paid))} paid in fiscal years overlapping it."
    return _finding(
        "A company in this case is on a register of suppliers barred from public contracts",
        f"{r.get('company')} is {(r.get('status') or 'listed').lower()} on {listed} from {start or 'an unstated date'} to {end or 'an unstated date'}"
        + (f" ({r['infraction'][:140]})" if r.get("infraction") else "") + f".{detail}",
        "Bars apply from the start date and to the named legal entity: earlier contracts, contracts of governments the list does not bind, and a "
        "different company with a similar name are not breaches; PSPC can also invoke a public-interest exception.",
        "Confirm the legal entity (business number or NEQ) matches the contracting party; for any row inside the period, ask the contracting "
        "authority whether an exception was invoked or the contract terminated.",
        [_cite(data, project, f"Company: {r.get('company')}")]
        + [_cite(d, project, f"Reference: {row['reference']}") if row.get("reference") else _cite(d, project, f"Document number: {row.get('document_number')}")
           for d, row, _ in inside[:2]])


def cross_government(project):
    tiers = []
    fed = _dataset(project, "fed_contracts")
    if fed and fed.get("summary"):
        tiers.append(("federal contracts", {y["year"]: y["committed"] for y in fed["summary"]["by_year"] if y["year"]}, fed, "fed"))
    on = _dataset(project, "on_payments")
    if on and on["records"]:
        tiers.append(("Ontario payments", {int(y["year"][:4]): y["amount"] for y in on["summary"]["by_year"]}, on, "on"))
    city = _dataset(project, "city_awards")
    if city and city["records"]:
        by_year = defaultdict(float)
        for r in city["records"]:
            if r.get("year"):
                by_year[r["year"]] += r.get("amount") or 0
        tiers.append((f"{city['records'][0].get('city')} awards", dict(by_year), city, "city"))
    active = [t for t in tiers if sum(t[1].values()) > 0]
    overlap = sorted(y for y in set().union(*(set(t[1]) for t in active)) if sum(1 for t in active if t[1].get(y)) >= 2) if len(active) >= 2 else []
    if not overlap:
        return None
    peak = max(overlap, key=lambda y: sum(t[1].get(y, 0) for t in active))

    def cite(tier):
        name, values, d, kind = tier
        if kind == "fed":
            return _cite(d, project, f"{peak} | ")
        if kind == "on":
            label = next((r["fiscal_year"] for r in d["records"] if int(r["fiscal_year"][:4]) == peak), None)
            return _cite(d, project, f"Fiscal year: {label}") if label else None
        when = next((r["award_date"] for r in d["records"] if r.get("year") == peak and r.get("award_date")), None)
        return _cite(d, project, f"Award date: {when}") if when else None
    return _finding(
        "The supplier is paid by more than one level of government in the same years",
        f"Records show money from {len(active)} tiers in {len(overlap)} overlapping year(s): "
        + "; ".join(f"{name} {_money(sum(v.values()))}" for name, v, _, _ in active) + f". Peak year {peak}: "
        + ", ".join(f"{name} {_money(v.get(peak, 0))}" for name, v, _, _ in active)
        + ". Federal figures are commitments, Ontario figures are payments including tax, city figures are award values.",
        "Large and specialised firms routinely serve federal, provincial and municipal clients at once; the tiers buy independently and nothing "
        "requires them to coordinate.",
        "Check whether the same deliverable appears under more than one funder (duplicate funding of one project) and whether the firm's capacity "
        "matches the combined volume; cost-shared infrastructure legitimately shows up as both a federal contribution and a municipal award.",
        [cite(t) for t in active if t[1].get(peak)])


def numbered_company(project):
    for key, label in (("fed_contracts", "Vendor name"), ("city_awards", "Vendor name"), ("on_payments", "Recipient"), ("fed_grants", "Recipient")):
        data = _dataset(project, key)
        for r in (data or {"records": []})["records"]:
            name = (r.get("vendor_name") or r.get("recipient") or "").strip()
            if name and NUMBERED.match(name):
                return _finding(
                    "The contracting party is a numbered company",
                    f"Records name the supplier as {name}: a numbered company's registered name says nothing about who operates it or what it does.",
                    "Numbered companies are ordinary and common, especially for small firms, holding companies and franchisees; the operating name is "
                    "often disclosed elsewhere ('o/a', 'doing business as').",
                    "Look the number up in Corporations Canada, the Ontario Business Registry or Québec's REQ for directors and the operating name; "
                    "check whether any director is a former official of the awarding department.",
                    [_cite(data, project, f"{label}: {name}")])
    return None


def former_public_servant(project):
    data = _dataset(project, "fed_contracts")
    if not data:
        return None
    rows = [r for r in data["records"] if str(r.get("former_public_servant") or "").strip().upper() in ("Y", "YES", "OUI", "TRUE")]
    if not rows:
        return None
    sample = max(rows, key=committed)
    return _finding(
        "Contracts flagged as awarded to a former public servant receiving a pension",
        f"{len(rows)} disclosed row(s) totalling {_money(sum(committed(r) for r in rows))} carry the 'former public servant' flag (a former public "
        f"servant in receipt of a Public Service Superannuation Act pension), e.g. {sample.get('reference')} with {sample.get('department')}.",
        "Hiring former officials for their expertise is lawful; the flag exists for transparency, and Treasury Board policy requires extra "
        "justification and, for some, a fee abatement.",
        "Request the contracting justification and the post-employment conflict-of-interest check for the individual; compare the award date "
        "with their departure date from the department.",
        [_cite(data, project, f"Reference: {r['reference']}") for r in sorted(rows, key=committed, reverse=True)[:3] if r.get("reference")])


def grant_and_contract(project):
    grants, contracts = _dataset(project, "fed_grants"), _dataset(project, "fed_contracts")
    if not grants or not contracts or not grants["records"]:
        return None
    departments = {r.get("department") for r in grants["records"]} & {r.get("department") for r in contracts["records"]}
    if not departments:
        return None
    g = [r for r in grants["records"] if r.get("department") in departments]
    c = [r for r in contracts["records"] if r.get("department") in departments and not r.get("is_amendment")]
    return _finding(
        "The same department both funds the company and buys from it",
        f"{', '.join(sorted(d for d in departments if d))} disclosed {len(g)} grant or contribution agreement(s) worth {_money(sum(r.get('value') or 0 for r in g))} "
        f"and {len(c)} contract(s) worth {_money(sum(committed(r) for r in c))} with the same company.",
        "Contribution agreements and service contracts serve different purposes, and a firm active in a sector can properly receive both.",
        "Check that the contracted deliverables were not also claimed as eligible costs under the contribution agreement (double funding), and "
        "that the contribution's conditions did not require arm's-length procurement.",
        [_cite(grants, project, f"Reference: {g[0]['reference']}") if g[0].get("reference") else None,
         _cite(contracts, project, f"Reference: {c[0]['reference']}") if c and c[0].get("reference") else None])


def vendor_indicators(project):
    findings = [f for check in (barred_while_paid, non_competitive_share, amendment_creep, contract_splitting, single_bid_share,
                                lobbying_before_award, year_end_clustering, cross_government, grant_and_contract,
                                former_public_servant, numbered_company)
                if (f := check(project))]
    gaps = []
    pulled = project.get("open_data") or []
    vendor = project.get("vendor") or {}
    if not pulled:
        gaps.append("Open data has not been pulled for this supplier. Pull it from the Supplier tab.")
    else:
        names = [vendor.get("name", "")] + list(vendor.get("aliases") or [])
        if set(n for n in names if n) - set(pulled[0].get("vendor_names") or []):
            gaps.append("Names were added after the last open-data pull. Pull again to include them.")
        if any("index is being built" in e for e in pulled[0].get("errors") or []):
            gaps.append("The federal contracts index is still being built on the server; pull again in a few minutes to add it.")
        data = _dataset(project, "fed_contracts")
        if data is not None and not data["records"]:
            gaps.append("No federal contract over $10,000 names this supplier since the chosen year. Check the spelling against the Registry of "
                        "Lobbyists or Corporations Canada, add other names in the Supplier tab, and remember contracts of $10,000 and under "
                        "are published only as an aggregate.")
        if not vendor.get("province"):
            gaps.append("No province chosen, so provincial and municipal records were not pulled. Set it in the Supplier tab if the company "
                        "works in Ontario or Québec.")
    return findings, gaps
