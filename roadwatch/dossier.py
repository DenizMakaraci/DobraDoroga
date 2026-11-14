"""Deterministic dossiers handed to the model before it reasons: a dated timeline of everything on the street,
one dossier per supplier, the money table per contract, and - when the case is small enough - the full text
of the uploaded documents. Built by code, so the model spends its budget on reasoning, not retrieval."""
from collections import Counter, defaultdict
from decimal import Decimal

FULL_TEXT_LIMIT = 90_000  # characters of uploaded documents given to the model verbatim


def _data(project, key):
    return next((d for d in project.get("open_data", []) if d["key"] == key), None)


def _rows(project, key):
    return (_data(project, key) or {}).get("records", [])


def _money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def timeline(project):
    events = []
    add = lambda date, kind, text, **extra: events.append({"date": str(date)[:10], "kind": kind, "text": text[:220], **extra}) if date else None
    for r in _rows(project, "awarded"):
        if not r.get("road_work"):
            continue
        add(r.get("award_date"), "award", f"{r.get('supplier')} awarded {r.get('amount'):,.0f} - {r.get('description')}", street=r.get("street"), within_limits=r.get("within_limits"))
    for r in _rows(project, "bids"):
        if r.get("road_work"):
            add(r.get("date"), "tender", f"{r.get('bidders')} bidder(s), spread {r.get('spread_pct')}%, winner {r.get('supplier')} - {r.get('title')}", street=r.get("street"))
    for r in _rows(project, "planning"):
        if r.get("kind") == "estimate":
            add(r.get("date"), "estimate", f"estimate {r.get('estimate') or 0:,.0f} vs winning bid {r.get('winning_bid') or 0:,.0f} ({r.get('gap_pct')}%) - {r.get('description')}", street=r.get("street"))
        elif r.get("kind") in ("pipeline", "sidewalk_program"):
            add(f"{r.get('date')}-01-01" if r.get("date") and len(str(r.get("date"))) == 4 else r.get("date"), "plan", r.get("description") or "", street=r.get("street"))
    for r in _rows(project, "road_program"):
        if r.get("start_year"):
            add(f"{r['start_year']}-01-01", "program", f"{r.get('project')} {r.get('status')} - {r.get('location')} ({r.get('timeline')})", street=r.get("street"))
    for r in _rows(project, "restrictions"):
        add(r.get("start"), "permit", f"{r.get('contractor') or 'unknown contractor'} - {r.get('type')} {r.get('work_type') or ''} - {r.get('extent') or r.get('description') or ''}", street=r.get("street"), within_limits=r.get("within_limits"), ends=r.get("end"))
    for r in _rows(project, "lobbying"):
        if r.get("supplier"):
            add(r.get("first_communication") or r.get("last_communication"), "lobbying", f"{r.get('supplier')} lobbied: {r.get('subject')} - {r.get('officials_lobbied') or 'no dated contacts'}")
    for r in _rows(project, "development"):
        add(r.get("date"), "construction", f"{r.get('type')} at {r.get('address')} - {r.get('description') or ''}", street=r.get("street"))
    for r in _rows(project, "debarment"):
        add(r.get("registered"), "RENA", f"{r.get('company')} barred until {r.get('ends')} - {r.get('infraction') or ''}")
    summary = (_data(project, "requests_311") or {}).get("summary") or []
    by_year = Counter()
    for item in summary:
        if item.get("year") and any(k in item["type"].lower() for k in ("pot hole", "pothole", "damaged", "sinking", "utility cut", "nid-de-poule", "pavage", "trottoir ou bordure")):
            by_year[item["year"]] += item["count"]
    for year, n in sorted(by_year.items()):
        add(f"{year}-12-31", "311", f"{n} road-damage requests in {year}")
    for r in _rows(project, "climate"):
        if r.get("days", 0) >= 150:
            add(f"{r['year']}-04-30", "winter", f"winter {r['season']}: {r['freeze_thaw_days']} freeze-thaw days ({r.get('freeze_thaw_vs_average_pct')}% vs average)")
    for o in project.get("observations", []):
        add(o.get("observed_on"), "observation", f"{o.get('severity')}: {o.get('description')}")
    if project.get("completed_on"):
        add(project["completed_on"], "completion", "contract completion date recorded in the case")
    events.sort(key=lambda e: e["date"], reverse=True)
    kept, per_kind = [], Counter()
    for e in events:  # newest first; cap chatty kinds so the model sees the whole span, not just one feed
        limit = {"tender": 40, "award": 40, "permit": 30, "construction": 20}.get(e["kind"], 60)
        if per_kind[e["kind"]] < limit:
            kept.append(e); per_kind[e["kind"]] += 1
    return sorted(kept, key=lambda e: e["date"])


def suppliers(project):
    out = defaultdict(lambda: {"awards": 0, "award_total": 0.0, "award_dates": [], "streets": set(), "bids_won": 0, "bids_lost": 0,
                               "single_bidder_wins": 0, "spreads": [], "non_competitive": 0, "non_competitive_total": 0.0,
                               "lobbying_entries": 0, "officials": Counter(), "rena": None, "estimate_gaps": [], "integrity_mentions": 0})
    for r in _rows(project, "awarded"):
        s = out[r["supplier"]]
        s["awards"] += 1; s["award_total"] += _money(r.get("amount")); s["award_dates"].append(r.get("award_date")); s["streets"].add(r.get("street"))
    for r in _rows(project, "bids"):
        if not r.get("road_work"):
            continue
        winner = r.get("supplier")
        if winner and r.get("award_amount"):
            s = out[winner]; s["bids_won"] += 1
            if (r.get("bidders") or 0) == 1:
                s["single_bidder_wins"] += 1
            if r.get("spread_pct") is not None:
                s["spreads"].append(r["spread_pct"])
        for name in (r.get("bidder_names") or "").split("; "):
            from .opendata.common import canonical_company
            c = canonical_company(name)
            if c and c != winner and c in out:
                out[c]["bids_lost"] += 1
    for r in _rows(project, "non_competitive"):
        s = out[r["supplier"]]; s["non_competitive"] += 1; s["non_competitive_total"] += _money(r.get("amount"))
    for r in _rows(project, "lobbying"):
        if r.get("supplier"):
            s = out[r["supplier"]]; s["lobbying_entries"] += 1
            for part in (r.get("officials_lobbied") or "").split("; "):
                if part:
                    s["officials"][part.rsplit(" x", 1)[0]] += 1
    for r in _rows(project, "debarment"):
        out[r["supplier"]]["rena"] = f"{r.get('company')} barred {r.get('registered')} to {r.get('ends')}; linked person {r.get('linked_person') or 'n/a'}"
    for r in _rows(project, "planning"):
        if r.get("kind") == "estimate" and r.get("supplier") and r.get("gap_pct") is not None:
            out[r["supplier"]]["estimate_gaps"].append(r["gap_pct"])
    for r in _rows(project, "integrity"):
        if r.get("supplier"):
            out[r["supplier"]]["integrity_mentions"] += 1
    shares = {}
    for key in ("awarded", "bids"):
        shares.update(((_data(project, key) or {}).get("baselines") or {}).get("city_supplier_shares") or {})
    dossiers = []
    for name, s in out.items():
        if not name:
            continue
        dossiers.append({"supplier": name, "awards_on_streets": s["awards"], "award_total": round(s["award_total"]),
                         "citywide_share_pct": (shares.get(name) or {}).get("share_pct"),
                         "award_dates": sorted(d for d in s["award_dates"] if d), "streets": sorted(x for x in s["streets"] if x),
                         "seao_tenders_won": s["bids_won"], "seao_tenders_lost": s["bids_lost"], "single_bidder_wins": s["single_bidder_wins"],
                         "avg_bid_spread_pct": round(sum(s["spreads"]) / len(s["spreads"]), 1) if s["spreads"] else None,
                         "non_competitive_contracts": s["non_competitive"], "non_competitive_total": round(s["non_competitive_total"]),
                         "lobbying_entries": s["lobbying_entries"], "officials_lobbied": [k for k, _ in s["officials"].most_common(5)],
                         "rena": s["rena"], "estimate_gaps_pct": s["estimate_gaps"], "auditor_mentions": s["integrity_mentions"]})
    dossiers.sort(key=lambda d: (d["award_total"], d["seao_tenders_won"]), reverse=True)
    return dossiers[:25]


def money_table(project):
    groups = defaultdict(lambda: {"award": None, "amendments": [], "payments": [], "basis": None})
    for r in project.get("finances", []):
        g = groups[r["scope"]]
        g["basis"] = r["basis"]
        if r["kind"] == "award":
            g["award"] = Decimal(r["amount"])
        elif r["kind"] == "amendment":
            g["amendments"].append(Decimal(r["amount"]))
        else:
            g["payments"].append((r["reference"], Decimal(r["amount"])))
    rows = []
    for scope, g in groups.items():
        authorised = (g["award"] or Decimal(0)) + sum(g["amendments"], Decimal(0))
        paid = sum((a for _, a in g["payments"]), Decimal(0))
        rows.append({"scope": scope, "basis": g["basis"], "award": str(g["award"]) if g["award"] is not None else None,
                     "amendments": [str(a) for a in g["amendments"]], "authorised": str(authorised), "payments": [(ref, str(a)) for ref, a in g["payments"]],
                     "paid": str(paid), "paid_minus_authorised": str(paid - authorised) if g["award"] is not None else None})
    return rows


def full_text(project):
    docs = [d for d in project.get("evidence", []) if d.get("kind") != "open_data"]
    total = sum(len(p["text"]) for d in docs for p in d["pages"])
    if not docs or total > FULL_TEXT_LIMIT:
        return None
    return [{"evidence_id": d["id"], "title": d["title"], "kind": d["kind"],
             "pages": [{"page": p["number"], "text": p["text"]} for p in d["pages"]]} for d in docs]


def previous_findings(project):
    reports = project.get("reports", [])
    if not reports:
        return []
    seen, out = set(), []
    for report in reversed(reports[-3:]):
        for f in report.get("findings", []):
            key = f.get("key") or f.get("title")
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": f.get("key"), "title": f.get("title"), "origin": f.get("origin"), "confidence": f.get("confidence"),
                        "report_date": report.get("created_at", "")[:10]})
    return out[:30]


def build(project):
    from . import ml
    return {"timeline": timeline(project), "suppliers": suppliers(project), "money": money_table(project),
            "documents_full_text": full_text(project), "previous_findings": previous_findings(project),
            "anomalies": ml.anomaly_scores(project)}
