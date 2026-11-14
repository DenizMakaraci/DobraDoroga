"""Deterministic procurement indicators computed from pulled open data. Each is a lead for review with
an innocent explanation; none is evidence of wrongdoing. Citations quote the open-data documents."""
from collections import Counter, defaultdict

from .opendata.common import canonical_company


def _dataset(project, key):
    return next((d for d in project.get("open_data", []) if d["key"] == key), None)


def _cite(dataset, project, quote):
    """Cite the page of the dataset document that contains `quote`."""
    document = next((e for e in project["evidence"] if e["id"] == dataset["evidence_id"]), None)
    normalize = lambda text: " ".join(text.split())
    for page in (document or {}).get("pages", []):
        if normalize(quote) in normalize(page["text"]):
            return {"evidence_id": dataset["evidence_id"], "page": page["number"], "quote": quote}
    return None


def _finding(title, explanation, alternative, next_step, citations):
    citations = [c for c in citations if c]
    if not citations:
        return None
    return {"title": title, "explanation": explanation, "alternative_explanation": alternative,
            "next_step": next_step, "citations": citations[:8], "origin": "open_data", "priority": "review"}


def _money(amount):
    return f"CAD {amount:,.0f}"


def _scope(project):
    """'this street' or 'these N streets' for finding text."""
    n = len(project.get("locations") or [])
    return f"these {n} streets" if n > 1 else "this street"


def repeat_winner(project):
    data = _dataset(project, "awarded")
    if not data:
        return None
    road_awards = [r for r in data["records"] if r.get("road_work")]
    wins = Counter(r["supplier"] for r in road_awards if r["supplier"])
    repeats = [(s, n) for s, n in wins.items() if n >= 2]
    if not repeats or len(road_awards) < 3:
        return None
    supplier, count = max(repeats, key=lambda item: item[1])
    total = sum(r["amount"] for r in road_awards if r["supplier"] == supplier)
    streets = {r.get("street") for r in road_awards if r["supplier"] == supplier and r.get("street")}
    street_share = count / len(road_awards) * 100
    city = ((data.get("baselines") or {}).get("city_supplier_shares") or {}).get(supplier) or \
           (((_dataset(project, "bids") or {}).get("baselines") or {}).get("city_supplier_shares") or {}).get(supplier)
    baseline_note = ""
    if city and city.get("share_pct") is not None:
        if street_share < 1.5 * max(city["share_pct"], 1):
            return None  # the firm wins about as often here as it does citywide: not a street-specific pattern
        baseline_note = (f" Citywide, {supplier} won {city['share_pct']}% of comparable road awards ({city['awards']}); on these "
                         f"streets it won {street_share:.0f}% - {street_share / max(city['share_pct'], 1):.1f}x its citywide share.")
    cites = [_cite(data, project, f"Successful supplier: {supplier}")]
    return _finding(
        "One supplier won a large share of road work on the case's street(s)",
        f"{supplier} won {count} of {len(road_awards)} road-related competitive awards naming the case's "
        f"street(s) since {data['since_year']}, totalling {_money(total)} in award value"
        + (f", across {len(streets)} streets ({', '.join(sorted(streets))})" if len(streets) > 1 else "") + "." + baseline_note,
        "A contractor with nearby crews or equipment often bids lowest on adjacent work; the City awards tenders "
        "to the lowest compliant bid, so repeat wins can simply reflect competitive pricing.",
        "Request the bid tabulations for these tenders: number of bidders, bid spread, and the engineer's estimate "
        "for each. A pattern of single or clustered bids would merit a closer look.",
        cites)


def rework_interval(project):
    data = _dataset(project, "road_program")
    awards = _dataset(project, "awarded")
    if not data:
        return None
    streets = {r.get("street") for r in data["records"]} | {r.get("street") for r in (awards or {"records": []})["records"]}
    close = []
    for street in streets:
        years = sorted({r["start_year"] for r in data["records"] if r.get("start_year") and r.get("street") == street})
        award_years = sorted({int(r["award_date"][:4]) for r in (awards or {"records": []})["records"]
                              if r.get("road_work") and r.get("award_date") and r.get("street") == street})
        close += [(street, a, b, False) for a, b in zip(years, years[1:]) if b - a <= 3]
        close += [(street, a, b, True) for a in award_years for b in years if 0 < b - a <= 3]
    if not close:
        return None
    street, a, b, from_award = close[0]
    cites = [_cite(data, project, f"Start year: {b}")]
    if from_award:
        record = next(r for r in awards["records"] if r.get("award_date", "").startswith(str(a)) and r.get("street") == street)
        cites.append(_cite(awards, project, f"Award date: {record['award_date']}"))
    return _finding(
        "Road work scheduled within three years of earlier work on the same street",
        f"The city's road program lists work on {street or 'this street'} starting in {b}, within three years of "
        f"work recorded in {a}. Major resurfacing is normally expected to last well beyond that.",
        "Different segments of a long street, utility-driven cuts, or a planned staged program (e.g. watermain "
        "first, then resurfacing) legitimately produce closely spaced entries.",
        "Compare the from/to limits of each entry. If the same segment was rebuilt twice, request the warranty "
        "inspection and the reason for the second project.",
        cites)


def sole_source_share(project):
    data = _dataset(project, "non_competitive")
    awards = _dataset(project, "awarded")
    if not data or not data["records"]:
        return None
    by_supplier = defaultdict(list)
    for r in data["records"]:
        by_supplier[r["supplier"]].append(r)
    supplier, records = max(by_supplier.items(), key=lambda item: sum(r["amount"] for r in item[1]))
    total = sum(r["amount"] for r in records)
    reasons = Counter(r.get("reason") or "unspecified" for r in records)
    return _finding(
        "A contractor on the case's street(s) also received non-competitive contracts",
        f"{supplier}, a supplier on {_scope(project)}, received {len(records)} non-competitive City contract(s) since {data['since_year']} "
        f"totalling {_money(total)}; reasons given: "
        + ", ".join(f"{reason} ({n})" for reason, n in reasons.most_common(4)) + ".",
        "Emergency repairs, proprietary parts and warranty follow-ups are legitimate grounds for non-competitive "
        "awards under the Purchasing By-law, and these contracts may be unrelated to this road.",
        "Request the non-competitive procurement justifications and check whether any relate to this street "
        "or follow directly from the competitive award.",
        [_cite(data, project, f"Supplier: {supplier}")] +
        ([_cite(awards, project, f"Successful supplier: {supplier}")] if awards else []))


DAMAGE_WORDS = ("pot hole", "pothole", "damaged", "sinking", "utility cut",
                "nid-de-poule", "pavage", "trottoir ou bordure", "affaissement")


def complaint_spike(project):
    data = _dataset(project, "requests_311")
    if not data or not data.get("summary"):
        return None
    by_year = Counter()
    for item in data["summary"]:
        if item["year"] and any(k in item["type"].lower() for k in DAMAGE_WORDS):
            by_year[item["year"]] += item["count"]
    years = sorted(by_year)
    city = (data.get("baselines") or {}).get("city_road_damage_by_year") or {}
    spikes = []
    for a, b in zip(years, years[1:]):
        if by_year[b] < 10 or by_year[b] < 2 * by_year[a]:
            continue
        if city.get(a) and city.get(b):
            city_growth = city[b] / city[a]
            if by_year[b] / max(by_year[a], 1) <= city_growth * 1.25:
                continue  # the whole city rose as much that year: a winter or reporting effect, not this street
        spikes.append((a, b))
    if not spikes:
        return None
    a, b = spikes[-1]
    city_note = (f" Citywide, road-damage requests went from {city[a]:,} to {city[b]:,} ({city[b] / city[a]:.2f}x) over the same years; "
                 f"this street rose {by_year[b] / max(by_year[a], 1):.2f}x.") if city.get(a) and city.get(b) else ""
    completed = project.get("completed_on") or ""
    timing = (f" The case records completion on {completed}." if completed[:4] in (a, b) else "")
    context = []
    climate = _dataset(project, "climate")
    season = next((r for r in (climate or {"records": []})["records"] if str(r.get("year")) == b), None)
    if season and season.get("freeze_thaw_vs_average_pct") is not None:
        context.append(f"winter {season['season']} had {season['freeze_thaw_days']} freeze-thaw days "
                       f"({season['freeze_thaw_vs_average_pct']:+d}% vs the local average)")
    development = _dataset(project, "development")
    permits = [r for r in (development or {"records": []})["records"] if (r.get("date") or "")[:4] in (a, b)]
    if permits:
        context.append(f"{len(permits)} major building/demolition permit(s) were issued on the street(s) in {a}-{b}")
    candidates = [i for i in data["summary"] if i["year"] == b and any(k in i["type"].lower() for k in DAMAGE_WORDS)]
    top = max(candidates, key=lambda i: i["count"], default=None)
    sample = f"{b} | {top['type']}: {top['count']}" if top else None
    return _finding(
        "Road-damage complaints roughly doubled year over year",
        f"311 requests for potholes, damaged or sinking road and utility-cut settlement at intersections on "
        f"{_scope(project)} rose from {by_year[a]} in {a} to {by_year[b]} in {b}.{city_note}{timing}",
        "Harsh winters, nearby construction traffic, utility cuts by other parties, or simply more residents "
        "using 311 can all raise complaint counts without any defect in the contracted work."
        + (" In this case: " + "; ".join(context) + "." if context else ""),
        "Map the complaints against the contract limits and dates. If they cluster on the rebuilt segment within "
        "the warranty period, request the warranty inspection and any repair orders.",
        [_cite(data, project, sample)] if sample else [])


def other_contractors(project):
    data = _dataset(project, "restrictions")
    awards = _dataset(project, "awarded")
    if not data or not awards:
        return None
    winners = {r["supplier"].casefold() for r in awards["records"] if r.get("road_work") and r["supplier"]}
    if not winners:
        return None
    others = Counter((r.get("contractor") or "").strip() for r in data["records"]
                     if r.get("type") == "CONSTRUCTION" and r.get("contractor")
                     and r["contractor"].strip().casefold() not in winners)
    if not others:
        return None
    name, count = others.most_common(1)[0]
    flagged = [r for r in data["records"] if r.get("within_limits") is not None]
    inside = sum(1 for r in flagged if r["within_limits"] and r.get("type") == "CONSTRUCTION")
    limits_note = f" {inside} of them fall within the stated contract limits." if flagged else ""
    return _finding(
        "Construction permits name contractors other than the award winner",
        f"The live permit feed shows {sum(others.values())} construction restriction(s) on {_scope(project)} held by "
        f"contractors other than the competitive award winner(s), most often {name} ({count}).{limits_note}",
        "Utilities, transit agencies, developers and other City divisions obtain their own permits; "
        "subcontractors also appear under their own names.",
        "Check whether the permit holder is a subcontractor of the award winner (the contract's subcontractor "
        "list) or an unrelated utility, and whether the work overlaps the contract's limits and dates.",
        [_cite(data, project, f"Contractor: {name}")])


def supplier_lobbying(project):
    data = _dataset(project, "lobbying")
    awards = _dataset(project, "awarded")
    if not data or not awards:
        return None
    linked = [r for r in data["records"] if r.get("supplier")]
    if not linked:
        return None
    by_supplier = defaultdict(list)
    for r in linked:
        by_supplier[r["supplier"]].append(r)
    supplier, records = max(by_supplier.items(), key=lambda item: len(item[1]))
    award_dates = sorted(a["award_date"] for a in awards["records"] if a.get("supplier") == supplier and a.get("award_date"))
    near = []
    for r in records:
        for d in (r.get("first_communication"), r.get("last_communication")):
            for a in award_dates:
                if d and 0 <= (int(a[:4]) * 12 + int(a[5:7])) - (int(d[:4]) * 12 + int(d[5:7])) <= 12:
                    near.append((d, a))
    subjects = Counter(r.get("subject") or "unspecified" for r in records)
    officials = Counter()
    for r in records:
        for part in (r.get("officials_lobbied") or "").split("; "):
            if part:
                officials[part.rsplit(" x", 1)[0]] += 1
    detail = (f" Communications on {near[0][0]} fall within 12 months before the {near[0][1]} award." if near
              else " No communication dates fall within 12 months before an award in the records.")
    return _finding(
        "A supplier on the case's street(s) is a registered lobbyist of the City",
        f"{supplier} appears as the client in {len(records)} Lobbyist Registry subject matter(s) "
        f"({', '.join(f'{k} ({n})' for k, n in subjects.most_common(3))})"
        + (f", lobbying {', '.join(k for k, _ in officials.most_common(3))}" if officials else "") + "." + detail,
        "Registering to lobby is lawful and common for firms that bid on City work; registry entries often cover "
        "general 'services offered' outreach rather than any specific tender.",
        "Compare the lobbying dates and officials against the tender's evaluation period and decision body. "
        "Check whether any lobbied official sat on the awarding panel or committee.",
        [_cite(data, project, f"Supplier: {supplier}"), _cite(awards, project, f"Successful supplier: {supplier}")])


def single_bidder(project):
    data = _dataset(project, "bids")
    if not data:
        return None
    rows = [r for r in data["records"] if r.get("road_work") and r.get("method") != "direct" and (r.get("bidders") or 0) == 1 and r.get("award_amount")]
    if not rows:
        return None
    total = sum(r["award_amount"] for r in rows)
    sample = rows[0]
    return _finding(
        "Road tenders awarded with a single bidder",
        f"{len(rows)} tender(s) naming the case's street(s) or won by its suppliers received exactly one bid and were "
        f"awarded ({_money(total)} in total), e.g. {sample['title'][:120]} awarded to {sample['supplier']} on {sample['date']}.",
        "Specialised or small jobs, short bidding windows and busy construction seasons routinely attract one bid; "
        "public bodies may award to a sole compliant bidder after checking the price against the estimate.",
        "Request the engineer's estimate and the list of firms that obtained the documents (SEAO records it). A sole "
        "bid far above estimate, or repeated sole bids by the same firm, would warrant review.",
        [_cite(data, project, f"OCID: {sample['ocid']}")])


def tight_or_inverted_bids(project):
    data = _dataset(project, "bids")
    if not data:
        return None
    rows = [r for r in data["records"] if r.get("road_work") and r.get("spread_pct") is not None]
    tight = [r for r in rows if r["spread_pct"] <= 2.0]
    inverted = [r for r in rows if r.get("winner_is_lowest") is False and (r.get("bidders") or 0) > 1]
    if not tight and not inverted:
        return None
    parts, cites = [], []
    if tight:
        parts.append(f"{len(tight)} tender(s) where the two lowest bids were within 2% of each other "
                     f"(e.g. {tight[0]['spread_pct']}% on {tight[0]['title'][:80]})")
        cites.append(_cite(data, project, f"OCID: {tight[0]['ocid']}"))
    if inverted:
        parts.append(f"{len(inverted)} tender(s) where the award did not go to the lowest bid "
                     f"(e.g. {inverted[0]['supplier']} on {inverted[0]['date']}, {inverted[0]['bidders']} bidders)")
        cites.append(_cite(data, project, f"OCID: {inverted[0]['ocid']}"))
    return _finding(
        "Bid patterns worth checking: near-identical low bids or award not to the lowest bidder",
        "; ".join(parts) + ".",
        "Unit-price tenders on standard work legitimately produce close bids; awards skip the lowest bid when it is "
        "non-compliant, withdrawn, or when the evaluation weighs quality (RFP) rather than price alone.",
        "Obtain the bid evaluation report: compliance decisions, any disqualification of the low bidder, and the "
        "estimate. Compare the same firms' bids against each other across several tenders.",
        cites)


def debarred_company(project):
    data = _dataset(project, "debarment")
    if not data or not data["records"]:
        return None
    awards = _dataset(project, "awarded")
    hit = max(data["records"], key=lambda r: (r.get("won_while_barred") or 0, r.get("contracts_won_in_case") or 0, r.get("tenders_bid_in_case") or 0))
    won, during, bid_on = hit.get("contracts_won_in_case") or 0, hit.get("won_while_barred") or 0, hit.get("tenders_bid_in_case") or 0
    role = (f"won {won} contract(s) in this case" + (f", {during} of them awarded while barred" if during else "")) if won else \
           (f"bid on {bid_on} tender(s) in this case" if bid_on else "appears among the case's companies")
    if hit.get("example"):
        role += f" (e.g. {hit['example']})"
    return _finding(
        "A company in this case is on Québec's register of firms barred from public contracts (RENA)",
        f"{hit['company']} (NEQ {hit['neq']}) is registered in RENA from {hit['registered']} to {hit['ends']}"
        + (f" - {hit['infraction'][:160]}" if hit.get("infraction") else "") + ". "
        f"In this case the company {role}. Linked person: {hit.get('linked_person') or 'n/a'}; majority shareholder: {hit.get('majority_shareholder') or 'n/a'}.",
        "RENA covers many offences (tax, labour-relations, Competition Act) and bars apply from the registration date; "
        "contracts awarded before registration, or to a different company with a similar name, are lawful.",
        "Confirm the NEQ matches the contracting party; check whether any award or subcontract falls inside the ineligibility "
        "period; look for other companies sharing the linked person or shareholder among the bidders.",
        [_cite(data, project, f"NEQ: {hit['neq']}")] + ([_cite(awards, project, f"Successful supplier: {hit['supplier']}")] if won else []))


def estimate_gap(project):
    data = _dataset(project, "planning")
    if not data:
        return None
    rows = [r for r in data["records"] if r.get("kind") == "estimate" and r.get("gap_pct") is not None and abs(r["gap_pct"]) >= 15]
    if not rows:
        return None
    worst = max(rows, key=lambda r: abs(r["gap_pct"]))
    direction = "above" if worst["gap_pct"] > 0 else "below"
    return _finding(
        "Winning bid far from the City's own estimate",
        f"{len(rows)} tender(s) on the case's street(s) closed {'above' if worst['gap_pct'] > 0 else 'below'} the City's "
        f"internal estimate by 15% or more; the largest gap is {abs(worst['gap_pct'])}% {direction} estimate "
        f"({_money(worst['winning_bid'])} vs {_money(worst['estimate'])}) for {worst['description'][:100]} won by {worst['supplier']}.",
        "Estimates are prepared months before bidding; material prices, market saturation and scope changes move "
        "bids in both directions. A bid far below estimate can also signal an aggressive bid to be recovered via extras.",
        "Request the estimate file and the change-order register for that contract; compare the final cost to both "
        "the estimate and the winning bid.",
        [_cite(data, project, f"Gap percent: {worst['gap_pct']}")])


def road_indicators(project):
    findings = [f for check in (repeat_winner, rework_interval, sole_source_share, complaint_spike, other_contractors,
                                supplier_lobbying, single_bidder, tight_or_inverted_bids, estimate_gap, debarred_company)
                if (f := check(project))]
    gaps = []
    if project.get("locations") and not project.get("open_data"):
        gaps.append("Open data has not been pulled for the selected streets. Pull it from the Map tab.")
    pulled = project.get("open_data") or []
    if pulled and {l["road"] for l in project.get("locations", [])} - set(pulled[0].get("streets") or []):
        gaps.append("Streets were added after the last open-data pull. Pull again to include them.")
    data = _dataset(project, "awarded")
    if data is not None and not data["records"]:
        gaps.append("No awarded contract in the open-data portal names this street since the chosen year. Older "
                    "awards and awards described by contract number only must be found in council documents.")
    return findings, gaps
