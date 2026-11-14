"""Ville de Montréal open data for one or more streets (donnees.montreal.ca, Licence CC-BY 4.0).

Datasets:
  awarded          Contracts: central-city delegated ("fonctionnaires"), executive committee, borough councils
  restrictions     Info-travaux: construction permits (contractor, dates) joined to the street segments they block
  requests_311     Requêtes 311 (2014-today across archive files) - road-related activities only

Montréal's CKAN datastore accepts SQL (POST), so rows are filtered server-side, then verified locally with
the same street matcher as Toronto. Record keys mirror the Toronto connector so indicators and the overview
work for both cities. There is no public non-competitive or lobbying dataset; Québec's lobbyist registry is a
separate provincial system without a bulk download.
"""
import json
import os
import re
import time
from collections import Counter

import httpx

from . import climate
from .common import canonical_company, USER_AGENT, cache_dir, dataset_result, fetch_cached, fold, load_json, matching_street, post_json_cached, street_key
from .geo import gpkg_features

NAME = "Montréal"
KEY = "montreal"
LANGUAGE = "fr"
CENTRE = (45.5019, -73.5674)
BBOX = (-73.98, 45.40, -73.47, 45.71)
PORTAL = "https://donnees.montreal.ca/"
CLIMATE_STATION = (51157, "Montréal-Trudeau Intl A")
RENA_URL = ("https://www.donneesquebec.ca/recherche/dataset/registre-des-entreprises-non-admissibles-aux-contrats-publics-rena/"
            "resource/c8843f2e-16ef-4dfe-a5e3-4b4089fc3f9e/download/rena.json")
CKAN = "https://donnees.montreal.ca/api/3/action"
SQL = f"{CKAN}/datastore_search_sql"
RESOURCES = {
    "central": "e4b758ab-3edb-4b6a-8764-2a443b6b9404",
    "executive": "4b2d8744-a257-4102-8897-95a30a20de34",
    "boroughs": "941c7e36-a831-4228-b737-21174c6a1864",
    "permits": "cc41b532-f12d-40fb-9f55-eb58c9a2b12b",
    "permit_impacts": "a2bc8014-488c-495d-941b-e7ae1999d1bd",
    "311_2022": "2cfa0e06-9be4-49a6-b7f1-ee9f2363a872",
    "311_2019": "dbfc05f8-b939-4639-ae52-2e77f738e43f",
    "311_2017": "f180b33d-7525-48de-b0d1-1b8d15a958bb",
    "311_2014": "f62595b0-b26a-4e7b-ba67-05b84521ab64",
    "estimates": "99ec28a4-0a8b-4a6c-9f61-8d5a1c0f1b2e",
    "permits_construction": "5232a72d-235a-48eb-ae20-bb9d501300ad",
    "traffic_recent": "f82f00c0-6b1b-4fd2-9b3f-5b2b7d0d2f4e",
}
TRAFFIC_PACKAGE = "comptage-vehicules-pietons"
PERMITS_PACKAGE = "permis-construction"
PLAN_URL = "https://donnees.montreal.ca/dataset/resultats-plan-intervention-actifs-eau-voirie/resource/c567a675-ee66-4537-b62e-e9771040a15f/download/pim_troncon_unifie_l_j.geojson"
SEAO_PACKAGE = "https://www.donneesquebec.ca/recherche/api/3/action/package_show?id=systeme-electronique-dappel-doffres-seao"
SEAO_MONTHS_PER_PULL = int(os.environ.get("ROADWATCH_SEAO_MONTHS", "6"))
PAGES = {
    "planning": "https://donnees.montreal.ca/dataset/resultats-plan-intervention-actifs-eau-voirie",
    "debarment": "https://www.donneesquebec.ca/recherche/dataset/registre-des-entreprises-non-admissibles-aux-contrats-publics-rena",
    "development": "https://donnees.montreal.ca/dataset/permis-construction",
    "traffic": "https://donnees.montreal.ca/dataset/comptage-vehicules-pietons",
    "climate": "https://climate.weather.gc.ca/historical_data/search_historic_data_e.html",
    "estimates": "https://donnees.montreal.ca/dataset/bilan-des-estimations-infrastructures",
    "integrity": "https://www.bigmtl.ca/publications/",
    "bids": "https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao",
    "condition": "https://donnees.montreal.ca/dataset/condition-chaussees-reseau-routier",
    "awarded": "https://donnees.montreal.ca/dataset/contrats-octroyes-par-les-fonctionnaires-ville-centrale",
    "restrictions": "https://donnees.montreal.ca/dataset/info-travaux",
    "requests_311": "https://donnees.montreal.ca/dataset/requete-311",
}
ROAD_WORK_WORDS = ("chauss", "pavage", "trottoir", "réfection", "refection", "voirie", "égout", "egout", "aqueduc",
                   "asphalte", "bordure", "planage", "reconstruction", "rue ", "piste cyclable", "conduite")
ROAD_311 = ("Nid-de-poule", "Pavage - Réparation", "Voirie - Divers", "Trottoir ou bordure - Réparation",
            "Puisard/TH-Première intervention", "Ingénierie - Pavage", "Ingénierie - Trottoir, bordure")


_last_query = 0.0


def _sql(name, query, ttl=24 * 3600):
    """One datastore SQL query, cached; paced to ~1 request/1.5 s because the portal's WAF blocks bursts,
    with a single retry after a pause if it does."""
    global _last_query
    for attempt in range(2):
        wait = 3.0 - (time.monotonic() - _last_query)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_query = time.monotonic()
            return post_json_cached(f"montreal-{name}", SQL, {"sql": query}, ttl=ttl)["result"]["records"]
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 403 or attempt:
                raise
            time.sleep(45)
    raise RuntimeError("unreachable")


def _date(value):
    """'2025-12-09T..' or '9/12/2025' (month/day/year, as the executive-committee file uses) -> 'YYYY-MM-DD'."""
    text = str(value or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}" if match else text[:10]


def _q(value):
    return "'" + str(value).replace("'", "''") + "'"


def _like_any(column, streets):
    """ILIKE on the accent-folded street base name; accents are matched loosely via unaccent-free wildcards."""
    parts = []
    for street in streets:
        base, _ = street_key(street)
        if base:
            parts.append(f'{column} ILIKE {_q("%" + base.replace(" ", "%") + "%")}')
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"


def _amount(value):
    try:
        return float(str(value).replace(",", ".").replace(" ", "") or 0)
    except ValueError:
        return 0.0


def _year(value):
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _label(streets):
    return ", ".join(streets)


canonical_supplier = canonical_company


def awarded_contracts(streets, since_year):
    rows = []
    sources = [
        ("central", "SELECT \"NOM DU FOURNISSEUR\" supplier, \"NUMERO\" number, \"DATE D'APPROBATION\" date, \"DESCRIPTION\" description, "
                    "\"SERVICE\" service, \"ACTIVITE\" activity, \"MONTANT\" amount FROM \"{rid}\" WHERE {where} AND \"DATE D'APPROBATION\" >= {since} "
                    "ORDER BY \"DATE D'APPROBATION\" DESC LIMIT 400", "DESCRIPTION", "Fonctionnaires (pouvoirs délégués)"),
        ("executive", 'SELECT "FOURNISSEUR" supplier, "NO DE DOSSIER" number, "DATE SIGNATURE" date, "OBJET" description, '
                      '"SERVICE" service, "DIRECTION" activity, "MONTANT" amount, "NO DECISION" decision FROM "{rid}" WHERE {where} AND "DATE SIGNATURE" >= {since} '
                      'ORDER BY "DATE SIGNATURE" DESC LIMIT 400', "OBJET", "Comité exécutif"),
        ("boroughs", 'SELECT "FOURNISSEUR" supplier, "NO DE DOSSIER" number, "DATE SIGNATURE" date, "OBJET" description, '
                     '"SERVICE" service, "DIRECTION" activity, "MONTANT" amount, "NO DECISION" decision FROM "{rid}" WHERE {where} AND "DATE SIGNATURE" >= {since} '
                     'ORDER BY "DATE SIGNATURE" DESC LIMIT 400', "OBJET", "Conseils d'arrondissement"),
    ]
    for name, template, column, body in sources:
        query = template.format(rid=RESOURCES[name], where=_like_any(f'"{column}"', streets), since=_q(f"{since_year}-01-01"))
        for r in _sql(f"contracts-{name}", query):
            description = " ".join(str(r.get("description") or "").split())
            street = matching_street(description, streets)
            date = _date(r.get("date"))
            if not street or (_year(date) or since_year) < since_year:
                continue
            rows.append({
                "street": street, "document_number": r.get("number"), "type": body,
                "supplier": canonical_supplier(r.get("supplier")), "amount": _amount(r.get("amount")),
                "award_date": date, "division": r.get("service"),
                "description": description[:1200], "decision": r.get("decision"),
                "road_work": any(w in description.lower() for w in ROAD_WORK_WORDS),
            })
    rows.sort(key=lambda r: r["award_date"], reverse=True)
    return dataset_result(
        key="awarded", title=f"Montréal - contrats nommant {_label(streets)}",
        source_url=PAGES["awarded"], since=since_year, records=rows,
        description="Contracts awarded by the Ville de Montréal (delegated authority, executive committee and borough "
                    "councils) whose description names one of the case's streets. Amounts are contract values, not payments.",
        fields=[("Street", "street"), ("Document number", "document_number"), ("Awarding body", "type"),
                ("Successful supplier", "supplier"), ("Award amount", "amount"), ("Award date", "award_date"),
                ("Division", "division"), ("Decision", "decision"), ("Description", "description")])


def road_restrictions(streets):
    impacts = _sql("impacts", 'SELECT "id_request", "name", "fromname", "toname", "streetimpacttype", "sidewalk_blockedtype", "length" '
                              f'FROM "{RESOURCES["permit_impacts"]}" WHERE {_like_any('"name"', streets)} LIMIT 1000', ttl=6 * 3600)
    by_request = {}
    for i in impacts:
        street = matching_street(i.get("name") or "", streets)
        if street:
            by_request.setdefault(i["id_request"], []).append({**i, "street": street})
    if not by_request:
        return dataset_result(key="restrictions", title=f"Montréal - info-travaux permits - {_label(streets)}",
                              source_url=PAGES["restrictions"], records=[], description="No current permits name the case's streets.",
                              fields=[])
    ids = ", ".join(_q(k) for k in list(by_request)[:400])
    permits = _sql("permits", 'SELECT "id", "permit_permit_id", "contractnumber", "boroughid", "permitcategory", "currentstatus", '
                              '"duration_start_date", "duration_end_date", "reason_category", "occupancy_name", "submittercategory", '
                              f'"organizationname" FROM "{RESOURCES["permits"]}" WHERE "id" IN ({ids})', ttl=6 * 3600)
    rows = []
    for p in permits:
        segs = by_request.get(p["id"], [])
        rows.append({
            "street": segs[0]["street"] if segs else None, "id": p.get("permit_permit_id"), "road": segs[0]["name"] if segs else None,
            "extent": "; ".join(f"{s['name']} ({s.get('fromname')} → {s.get('toname')})" for s in segs[:3]),
            "type": "CONSTRUCTION" if (p.get("permitcategory") or "").lower().startswith("trav") else (p.get("permitcategory") or ""),
            "work_type": p.get("reason_category"), "permit_type": p.get("submittercategory"),
            "contractor": p.get("organizationname"), "contract_number": p.get("contractnumber"),
            "borough": p.get("boroughid"), "status": p.get("currentstatus"),
            "start": str(p.get("duration_start_date") or "")[:10], "end": str(p.get("duration_end_date") or "")[:10],
            "impact": "; ".join(sorted({s.get("streetimpacttype") or "" for s in segs} - {""})),
            "description": p.get("occupancy_name"),
        })
    rows.sort(key=lambda r: r["start"] or "", reverse=True)
    return dataset_result(
        key="restrictions", title=f"Montréal - info-travaux permits - {_label(streets)}",
        source_url=PAGES["restrictions"], records=rows,
        description="Current and scheduled occupation permits for work on the case's streets, from the City's "
                    "info-travaux feed, with the organisation named on the permit and the contract number when the "
                    "work is for the City. Past permits drop off the feed.",
        fields=[("Street", "street"), ("Permit id", "id"), ("Extent", "extent"), ("Type", "type"),
                ("Work type", "work_type"), ("Permit type", "permit_type"), ("Contractor", "contractor"),
                ("Contract number", "contract_number"), ("Borough", "borough"), ("Status", "status"),
                ("Start", "start"), ("End", "end"), ("Impact", "impact"), ("Description", "description")])


def _search(name, resource, params, ttl=24 * 3600):
    """datastore_search (GET, paged) - the portal's WAF tolerates it better than SQL for the 311 tables."""
    global _last_query
    rows, offset = [], 0
    while True:
        wait = 3.0 - (time.monotonic() - _last_query)
        if wait > 0:
            time.sleep(wait)
        _last_query = time.monotonic()
        try:
            page = post_json_cached(f"montreal-{name}-{offset}", f"{CKAN}/datastore_search",
                                    {"resource_id": resource, "limit": 5000, "offset": offset, **params}, ttl=ttl)["result"]
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:  # archive resource withdrawn from the portal
                return rows
            raise
        rows.extend(page["records"])
        offset += 5000
        if len(page["records"]) < 5000 or offset >= 20000:
            return rows


def requests_311(streets, since_year):
    rows, counts, seen = [], Counter(), set()
    bases = [street_key(s)[0] for s in streets if street_key(s)[0]]
    archives = [name for name in ("311_2022", "311_2019", "311_2017", "311_2014") if int(name.split("_")[1]) + 3 >= since_year]
    for name in archives:
        for base in bases:
            for column in ("RUE", "RUE_INTERSECTION1", "RUE_INTERSECTION2"):
                found = _search(f"{name}-{column}-{base}", RESOURCES[name], {
                    "q": json.dumps({column: base}), "filters": json.dumps({"ACTI_NOM": list(ROAD_311)}),
                    "fields": "DDS_DATE_CREATION,ACTI_NOM,DERNIER_STATUT,RUE,RUE_INTERSECTION1,RUE_INTERSECTION2,ARRONDISSEMENT",
                    "sort": "DDS_DATE_CREATION desc"})
                for r in found:
                    street = matching_street(r.get(column) or "", streets)
                    created = str(r.get("DDS_DATE_CREATION") or "")[:10]
                    if not street or not created or int(created[:4]) < since_year:
                        continue
                    key = (created, r.get("ACTI_NOM"), r.get("RUE"), r.get("RUE_INTERSECTION1"), r.get("RUE_INTERSECTION2"))
                    if key in seen:
                        continue
                    seen.add(key)
                    counts[(created[:4], r.get("ACTI_NOM"))] += 1
                    rows.append({"street": street, "created": created, "type": r.get("ACTI_NOM"), "status": r.get("DERNIER_STATUT"),
                                 "intersection": " & ".join(x for x in (r.get("RUE"), r.get("RUE_INTERSECTION1"), r.get("RUE_INTERSECTION2")) if x),
                                 "ward": r.get("ARRONDISSEMENT"), "division": "311"})
    rows.sort(key=lambda r: r["created"], reverse=True)
    summary = [{"year": y, "type": t, "count": n} for (y, t), n in sorted(counts.items())]
    result = dataset_result(
        key="requests_311", title=f"Montréal - requêtes 311 voirie - {_label(streets)}",
        source_url=PAGES["requests_311"], since=since_year, records=rows[:400],
        description="Pothole (nid-de-poule), paving repair, sidewalk, catch-basin and similar 311 requests on the case's "
                    "streets. Counts by year and type are listed first; individual requests follow (up to 400 most recent).",
        fields=[("Street", "street"), ("Created", "created"), ("Type", "type"), ("Status", "status"),
                ("Intersection", "intersection"), ("Ward", "ward")])
    lines = [f"{s['year']} | {s['type']}: {s['count']}" for s in summary]
    if lines:
        result["pages"][0]["text"] = result["pages"][0]["text"].replace(
            "\n\nRecord 1", "\n\nCounts by year and type\n" + "\n".join(lines) + "\n\nRecord 1", 1)
    result["summary"] = summary
    result["total"] = sum(counts.values())
    return result


def _seao_months():
    """Monthly OCDS files available on Données Québec, newest first: [(yyyymm, url)]."""
    package = load_json(fetch_cached("seao-package.json", SEAO_PACKAGE, ttl=86400))
    months = []
    for r in package["result"]["resources"]:
        match = re.match(r"mensuel_(\d{6})\d{2}_", r.get("name") or "")
        if r.get("format") == "JSON" and match:
            months.append((match.group(1), r["url"]))
    return sorted(set(months), reverse=True)


def _compact_release(release):
    tender, buyer = release.get("tender") or {}, (release.get("buyer") or {}).get("name", "")
    bids = sorted(b["value"] for b in (release.get("bids") or []) if isinstance(b, dict) and isinstance(b.get("value"), (int, float)))
    awards = [{"amount": (a.get("value") or {}).get("amount"), "suppliers": [x.get("name") for x in a.get("suppliers", [])],
               "date": str(a.get("date") or "")[:10]} for a in release.get("awards") or []]
    docs = [d.get("url") for d in tender.get("documents", []) if d.get("url")]
    return {"ocid": release.get("ocid"), "date": str(release.get("date") or "")[:10], "buyer": buyer,
            "title": " ".join(str(tender.get("title") or "").split()), "category": tender.get("mainProcurementCategory"),
            "method": tender.get("procurementMethod"), "bidders": tender.get("numberOfTenderers"),
            "tenderers": [x.get("name") for x in tender.get("tenderers", [])], "bids": bids, "awards": awards,
            "url": docs[0] if docs else None}


def _seao_month(yyyymm, url):
    """Compact Montréal-only extract of one monthly SEAO file, cached indefinitely (the month is closed)."""
    path = cache_dir() / f"seao-montreal-{yyyymm}.json"
    if path.exists():
        return json.loads(path.read_text())
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        data = client.get(url).json()
    rows = [_compact_release(r) for r in data.get("releases", []) if "montr" in fold((r.get("buyer") or {}).get("name", ""))]
    path.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def seao_bids(streets, suppliers, since_year, budget=SEAO_MONTHS_PER_PULL):
    """SEAO tenders (bidders, bid amounts, award) for Montréal buyers that name a street or were won by a
    known supplier. Indexes `budget` new months per call; coverage grows with each pull."""
    wanted = {canonical_supplier(s) for s in suppliers if s}
    months = [(m, u) for m, u in _seao_months() if int(m[:4]) >= since_year]
    indexed, fetched = [], 0
    for yyyymm, url in months:
        cached = (cache_dir() / f"seao-montreal-{yyyymm}.json").exists()
        if not cached and fetched >= budget:
            continue
        fetched += 0 if cached else 1
        indexed.append((yyyymm, _seao_month(yyyymm, url)))
    rows, city_wins, city_total = [], Counter(), 0
    for yyyymm, releases in indexed:
        for r in releases:
            winners = [canonical_supplier(x) for a in r["awards"] for x in a["suppliers"] if x]
            if r["category"] == "works" and winners:
                city_total += 1
                city_wins[winners[0]] += 1
            street = matching_street(r["title"], streets)
            supplier = next((w for w in winners if w in wanted), None)
            if not street and not supplier:
                continue
            bids = r["bids"]
            spread = round((bids[1] - bids[0]) / bids[0] * 100, 1) if len(bids) > 1 and bids[0] else None
            award = r["awards"][0] if r["awards"] else {}
            rows.append({
                "street": street, "supplier": supplier or (winners[0] if winners else None),
                "ocid": r["ocid"], "date": award.get("date") or r["date"], "buyer": r["buyer"], "title": r["title"][:400],
                "category": r["category"], "method": r["method"], "bidders": r["bidders"] or len(r["tenderers"]) or len(bids),
                "bidder_names": "; ".join(r["tenderers"])[:400], "bid_amounts": ", ".join(f"{b:,.0f}" for b in bids),
                "lowest_bid": bids[0] if bids else None, "second_bid": bids[1] if len(bids) > 1 else None,
                "spread_pct": spread, "award_amount": award.get("amount"),
                "winner_is_lowest": (bool(bids) and award.get("amount") is not None and abs(award["amount"] - bids[0]) < 1) if bids else None,
                "road_work": r["category"] == "works" or any(w in r["title"].lower() for w in ROAD_WORK_WORDS),
                "seao_url": r["url"],
            })
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    covered = sorted(m for m, _ in indexed)
    coverage = f"{covered[0][:4]}-{covered[0][4:]} to {covered[-1][:4]}-{covered[-1][4:]}" if covered else "nothing yet"
    remaining = sum(1 for m, _ in months if not (cache_dir() / f"seao-montreal-{m}.json").exists())
    result = dataset_result(
        key="bids", title=f"SEAO tenders and bids - {_label(streets)} and its suppliers",
        source_url=PAGES["bids"], since=since_year, records=rows,
        description="Québec's electronic tendering system (OCDS open data): every bidder, every bid amount and the award "
                    f"for Ville de Montréal tenders naming the case's streets or won by its suppliers. Months indexed: {coverage}"
                    + (f"; {remaining} earlier month(s) not yet indexed - pull again to extend." if remaining else "."),
        fields=[("Street", "street"), ("Supplier", "supplier"), ("OCID", "ocid"), ("Date", "date"), ("Buyer", "buyer"),
                ("Title", "title"), ("Category", "category"), ("Method", "method"), ("Number of bidders", "bidders"),
                ("Bidders", "bidder_names"), ("Bid amounts", "bid_amounts"), ("Lowest bid", "lowest_bid"),
                ("Second bid", "second_bid"), ("Spread percent", "spread_pct"), ("Award amount", "award_amount"),
                ("Winner is lowest", "winner_is_lowest"), ("SEAO notice", "seao_url")])
    result["coverage"] = {"from": covered[0] if covered else None, "to": covered[-1] if covered else None, "remaining_months": remaining}
    result["baselines"] = {"city_works_tenders": city_total, "city_supplier_shares": {
        r["supplier"]: {"awards": city_wins[r["supplier"]], "share_pct": round(city_wins[r["supplier"]] / (city_total or 1) * 100, 1)}
        for r in rows if r.get("supplier")}}
    return result


def _estimates_resource():
    package = load_json(fetch_cached("montreal-estimates-package.json", f"{CKAN}/package_show",
                                     params={"id": "bilan-des-estimations-infrastructures"}))
    return next(r["id"] for r in package["result"]["resources"] if r["format"] == "CSV" and str(r.get("datastore_active")) == "True")


def _num(value):
    try:
        return float(str(value).replace(",", ".").replace(" ", "").replace("%", "") or 0)
    except ValueError:
        return None


def planning(streets, since_year):
    """Planned and estimated work: the City's estimate-vs-award ledger (bilan des estimations) and the
    2016-2021 integrated intervention plan segments (measured PCI, planned works and costs)."""
    rows = []
    try:
        ledger = _search("estimates", _estimates_resource(), {})
    except Exception:
        ledger = []
    for r in ledger:
        description = f"{r.get('Description') or ''} {r.get('Nature de travaux') or ''}"
        street = matching_street(description, streets)
        year = _year(r.get("Date d'ouverture"))
        if not street or (year and year < since_year):
            continue
        estimate = _num(r.get("Estimé DEC ou EXT 'TOTAL (B)"))
        winning = _num(r.get("Valeur de la soumission retenue\n(C)"))
        rows.append({"street": street, "kind": "estimate", "date": str(r.get("Date d'ouverture") or "")[:10],
                     "description": " ".join(description.split())[:400], "borough": r.get("Arrondissement"),
                     "category": r.get("Catégorie de travaux"), "estimate": estimate, "supplier": canonical_supplier(r.get("Adjudicataire")),
                     "winning_bid": winning, "gap_pct": round((winning - estimate) / estimate * 100, 1) if estimate and winning else None,
                     "bidders": _num(r.get("Nbs de soum.")), "highest_bid": _num(r.get("Plus haute soumission"))})
    plan = load_json(fetch_cached("montreal-plan-intervention.geojson", PLAN_URL, ttl=30 * 86400, max_bytes=80_000_000))
    for f in plan.get("features", []):
        pr = f.get("properties") or {}
        street = matching_street(f"{pr.get('RUE') or ''}", streets)
        if not street:
            continue
        rows.append({"street": street, "kind": "plan", "date": str(pr.get("DATE_SIAD") or ""), "description": f"{pr.get('RUE')} ({pr.get('DE')} → {pr.get('A')})",
                     "borough": pr.get("NOM_ARROND"), "category": pr.get("CH_CATEGOR"), "pci": _num(pr.get("CH_PCI_MES")),
                     "pci_date": pr.get("CH_DATE_IN"), "planned_road_work": pr.get("CH_TRAV"), "planned_sewer_work": pr.get("EU_TRAV"),
                     "planned_water_work": pr.get("EP_TRAV"), "planned_cost": _num(pr.get("TOTAL_COUT")), "remark": pr.get("REMARQUE2")})
    rows.sort(key=lambda r: (r["kind"], r["date"] or ""), reverse=True)
    return dataset_result(
        key="planning", title=f"Montréal - estimates and intervention plan - {_label(streets)}",
        source_url=PAGES["planning"], since=since_year, records=rows,
        description="Two planning sources: the City's 'bilan des estimations' (internal estimate vs winning bid, number of bids, "
                    "2014 onward, central services only) and the 2016-2021 integrated intervention plan per segment "
                    "(measured PCI, planned road/sewer/water works and costs). Plans are intentions, not contracts.",
        fields=[("Street", "street"), ("Kind", "kind"), ("Date", "date"), ("Description", "description"), ("Borough", "borough"),
                ("Category", "category"), ("Estimate", "estimate"), ("Supplier", "supplier"), ("Winning bid", "winning_bid"),
                ("Gap percent", "gap_pct"), ("Number of bids", "bidders"), ("Highest bid", "highest_bid"), ("PCI", "pci"),
                ("PCI date", "pci_date"), ("Planned road work", "planned_road_work"), ("Planned sewer work", "planned_sewer_work"),
                ("Planned water work", "planned_water_work"), ("Planned cost", "planned_cost"), ("Remark", "remark")])


def integrity(suppliers, streets):
    """Bureau de l'inspecteur général publications whose title names a supplier or a street."""
    import html as html_lib
    page = fetch_cached("montreal-big-publications.html", PAGES["integrity"], ttl=7 * 86400).decode("utf-8", "replace")
    wanted = {canonical_supplier(s) for s in suppliers if s}
    rows, seen = [], set()
    for url, inner in re.findall(r'<a[^>]+href="(https://www\.bigmtl\.ca/publications/[^"]+)"[^>]*>(.*?)</a>', page, flags=re.S):
        title = html_lib.unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", inner))).strip()
        if url in seen or len(title) < 15:
            continue
        seen.add(url)
        supplier = next((s for s in wanted if len(s) > 4 and s in canonical_supplier(title)), None)
        street = matching_street(title, streets)
        if supplier or street:
            rows.append({"street": street, "supplier": supplier, "title": title[:400], "url": url,
                         "type": next((k for k in ("Rapport public", "Décision", "Bulletin", "Rapport annuel") if k.lower() in title.lower()), "Publication")})
    return dataset_result(
        key="integrity", title=f"Inspecteur général de Montréal - publications naming {_label(streets)} or its suppliers",
        source_url=PAGES["integrity"], records=rows,
        description="Public reports and decisions of the Bureau de l'inspecteur général (contract-integrity watchdog) whose "
                    "titles name one of the case's suppliers or streets. Only titles are indexed; open the link for the report.",
        fields=[("Street", "street"), ("Supplier", "supplier"), ("Type", "type"), ("Title", "title"), ("Link", "url")])


def _resource_ids(package_id, fmt="CSV", datastore=True):
    package = load_json(fetch_cached(f"montreal-{package_id}-package.json", f"{CKAN}/package_show", params={"id": package_id}, ttl=86400))
    return [(r.get("name") or "", r["id"]) for r in package["result"]["resources"]
            if r.get("format") == fmt and (not datastore or str(r.get("datastore_active")) == "True")]


def debarment(companies, roles=None):
    """Companies in Québec's RENA (barred from public contracts) matching any supplier or bidder name, with the
    linked persons and majority shareholders the registry lists - so related companies can be followed.
    `roles` maps a canonical company name to {"won": [(date, title)], "bid": [(date, title)]} from this case."""
    roles = roles or {}
    wanted = {canonical_supplier(c) for c in companies if c and len(c) > 3}
    if not wanted:
        return dataset_result(key="debarment", title="RENA - barred companies among suppliers and bidders", source_url=PAGES["debarment"],
                              records=[], description="No supplier or bidder names to check.", fields=[])
    data = load_json(fetch_cached("quebec-rena.json", RENA_URL, ttl=86400))
    entries = data.get("rena", {}).get("inscription", []) if isinstance(data, dict) else data
    rows = []
    for e in entries:
        names = [e.get("Nom_entreprise") or ""]
        extra = e.get("Informations_complementaires") or ""
        if "Autres noms" in extra and ":" in extra:
            names += [n.strip() for n in re.split(r"[\n;]", extra.split(":", 1)[1]) if n.strip()]
        hit = next((canonical_supplier(n) for n in names if canonical_supplier(n) in wanted), None)
        if not hit:
            continue
        role = roles.get(hit, {})
        won, bid = role.get("won", []), role.get("bid", [])
        during = [d for d, _ in won if (e.get("Inscription_registre") or "") <= d <= (e.get("Fin_inadmissibilite") or "")]
        rows.append({"supplier": hit, "company": e.get("Nom_entreprise"), "neq": e.get("NEQ"), "address": e.get("Adresse"),
                     "contracts_won_in_case": len(won), "won_while_barred": len(during), "tenders_bid_in_case": len(bid),
                     "example": "; ".join(f"{d} {t[:80]}" for d, t in (won or bid)[:2]),
                     "linked_person": e.get("Nom_personne_liee") or e.get("Nom_personne_physique"),
                     "majority_shareholder": e.get("Nom_de_lactionnaire_majoritaire"), "registered": e.get("Inscription_registre"),
                     "ends": e.get("Fin_inadmissibilite"), "infraction": " ".join(str(e.get("Infraction") or "").split())[:400],
                     "decision": e.get("Decision_AMP"), "category": e.get("Categorie"), "other_names": " ".join(extra.split())[:200]})
    rows.sort(key=lambda r: r["registered"] or "", reverse=True)
    return dataset_result(
        key="debarment", title="RENA - barred companies among the case's suppliers and bidders", source_url=PAGES["debarment"], records=rows,
        description="Québec's Registre des entreprises non admissibles aux contrats publics, checked against every supplier and SEAO "
                    "bidder in this case. An entry means the company was found guilty of a listed offence and barred from public "
                    "contracts for the period shown; contracts awarded during the period are a serious lead.",
        fields=[("Supplier", "supplier"), ("Company", "company"), ("NEQ", "neq"), ("Address", "address"),
                ("Contracts won in case", "contracts_won_in_case"), ("Won while barred", "won_while_barred"),
                ("Tenders bid in case", "tenders_bid_in_case"), ("Example", "example"), ("Linked person", "linked_person"),
                ("Majority shareholder", "majority_shareholder"), ("Registered", "registered"), ("Ineligible until", "ends"),
                ("Infraction", "infraction"), ("Decision", "decision"), ("Category", "category"), ("Other names", "other_names")])


def development(streets, since_year):
    """Construction permits (new construction and demolition) addressed on the case's streets."""
    rows = []
    try:
        resource = next(rid for _, rid in _resource_ids(PERMITS_PACKAGE))
    except StopIteration:
        resource = RESOURCES["permits_construction"]
    for street in streets:
        base, _ = street_key(street)
        if not base:
            continue
        for r in _search(f"permits-{base}", resource, {"q": json.dumps({"emplacement": base}),
                                                        "fields": "no_demande,date_emission,emplacement,arrondissement,description_type_demande,description_type_batiment,nature_travaux,nb_logements,latitude,longitude",
                                                        "sort": "date_emission desc"}, ttl=7 * 86400):
            if not matching_street(r.get("emplacement") or "", [street]):
                continue
            kind = (r.get("description_type_demande") or "")
            issued = str(r.get("date_emission") or "")[:10]
            if (_year(issued) or since_year) < since_year or not any(w in fold(kind) for w in ("construction", "demolition")):
                continue
            rows.append({"street": street, "kind": "building_permit", "date": issued, "reference": r.get("no_demande"),
                         "address": r.get("emplacement"), "type": kind, "building": r.get("description_type_batiment"),
                         "description": " ".join(str(r.get("nature_travaux") or "").split())[:300], "dwellings": r.get("nb_logements"),
                         "latitude": r.get("latitude"), "longitude": r.get("longitude")})
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return dataset_result(
        key="development", title=f"Montréal - construction and demolition permits - {_label(streets)}",
        source_url=PAGES["development"], since=since_year, records=rows,
        description="New-construction and demolition permits addressed on the case's streets. Heavy construction next to a road is a "
                    "frequent cause of damage unrelated to the paving contract.",
        fields=[("Street", "street"), ("Kind", "kind"), ("Date", "date"), ("Reference", "reference"), ("Address", "address"),
                ("Type", "type"), ("Building", "building"), ("Description", "description"), ("Dwellings", "dwellings")])


VEHICLE_CODES = {"camion": "trucks", "velo": "bikes", "pieton": "pedestrians", "autobus": "buses"}


def traffic(streets):
    """Turning-movement counts aggregated per intersection and count date: vehicles, trucks, pedestrians, bikes."""
    rows = []
    try:
        resources = [rid for name, rid in _resource_ids(TRAFFIC_PACKAGE) if "3 derni" in name or "2023" in name][:2]
    except Exception:
        resources = [RESOURCES["traffic_recent"]]
    for street in streets:
        base, _ = street_key(street)
        if not base:
            continue
        totals = {}
        for rid in resources:
            for r in _search(f"traffic-{rid[:8]}-{base}", rid, {"q": json.dumps({"Nom_Intersection": base}),
                                                                 "fields": "Nom_Intersection,Date,Description_Code_Banque,NBLT,NBT,NBRT,SBLT,SBT,SBRT,EBLT,EBT,EBRT,WBLT,WBT,WBRT,Latitude,Longitude"}, ttl=7 * 86400):
                if not matching_street(r.get("Nom_Intersection") or "", [street]):
                    continue
                key = (r.get("Nom_Intersection"), str(r.get("Date") or "")[:10])
                t = totals.setdefault(key, {"vehicles": 0, "trucks": 0, "bikes": 0, "pedestrians": 0, "buses": 0, "lat": r.get("Latitude"), "lon": r.get("Longitude")})
                moves = sum(_amount(r.get(c)) for c in ("NBLT", "NBT", "NBRT", "SBLT", "SBT", "SBRT", "EBLT", "EBT", "EBRT", "WBLT", "WBT", "WBRT"))
                cls = next((v for k, v in VEHICLE_CODES.items() if k in fold(r.get("Description_Code_Banque") or "")), "vehicles")
                t[cls] += moves
                if cls in ("trucks", "buses"):
                    t["vehicles"] += moves
        for (name, day), t in totals.items():
            rows.append({"street": street, "location": name, "date": day, "vehicles": round(t["vehicles"]), "trucks": round(t["trucks"]),
                         "heavy_vehicle_pct": round(t["trucks"] / t["vehicles"] * 100, 1) if t["vehicles"] else None,
                         "pedestrians": round(t["pedestrians"]), "bikes": round(t["bikes"]), "latitude": t["lat"], "longitude": t["lon"]})
    rows.sort(key=lambda r: (r["date"], r["vehicles"]), reverse=True)
    return dataset_result(
        key="traffic", title=f"Montréal - traffic counts at intersections on {_label(streets)}",
        source_url=PAGES["traffic"], records=rows[:300],
        description="Turning-movement counts summed per intersection and count day (recent survey files): motor vehicles, trucks and "
                    "truck share, pedestrians and bikes. Truck share is a key driver of pavement wear.",
        fields=[("Street", "street"), ("Location", "location"), ("Count date", "date"), ("Vehicles", "vehicles"), ("Trucks", "trucks"),
                ("Heavy vehicle percent", "heavy_vehicle_pct"), ("Pedestrians", "pedestrians"), ("Bikes", "bikes")])


CONDITION_FILES = {
    "2024 arterial": "https://donnees.montreal.ca/fr/dataset/cee47404-08bc-40e6-af71-cfcdce4617e8/resource/97489483-85f9-49cc-be33-829559439ab2/download/auscultation-chaussee-2024.gpkg",
    "2022 local": "https://donnees.montreal.ca/fr/dataset/cee47404-08bc-40e6-af71-cfcdce4617e8/resource/a4ac1425-0f4e-4dca-91da-c8666509a395/download/auscultation-chaussees-2022-local.gpkg",
}
PCI_GRADES = [(85, 1, "Excellent"), (70, 2, "Bon"), (55, 3, "Moyen"), (40, 4, "Mauvais"), (-1, 5, "Très mauvais")]
CONDITION_NOTE = ("Official pavement condition index (PCI, 0-100) measured by the City's road-survey vehicles: "
                  "arterial network surveyed in 2024, local streets in 2022. Higher is better.")


def condition_layer():
    """GeoJSON features with the City's measured PCI per segment (2024 arterial + 2022 local surveys)."""
    features = []
    for survey, url in CONDITION_FILES.items():
        raw = fetch_cached(f"montreal-pci-{survey.split()[0]}.gpkg", url, ttl=30 * 86400)
        path = cache_dir() / f"montreal-pci-{survey.split()[0]}.gpkg"
        path.write_bytes(raw) if not path.exists() else None
        for props, geometry in gpkg_features(str(path)):
            try:
                pci = float(props.get("Indice_PCI"))
            except (TypeError, ValueError):
                continue
            grade, label = next((g, lbl) for threshold, g, lbl in PCI_GRADES if pci >= threshold)
            features.append({"type": "Feature", "geometry": geometry, "properties": {
                "grade": grade, "label": f"PCI {pci:.0f} - {props.get('Etat_PCI') or label}",
                "detail": f"{props.get('Rue')} ({props.get('De')} → {props.get('A')}) · {survey} · surveyed {props.get('DateReleve')} · IRI {props.get('Indice_IRI')}",
                "pci": pci, "year": int(survey[:4]), "source": "Ville de Montréal - indicateurs de condition des chaussées"}})
    return {"type": "FeatureCollection", "features": features,
            "legend": {"title": "Pavement condition index (PCI)", "note": CONDITION_NOTE, "source_url": PAGES["condition"],
                       "classes": [(1, "Excellent (85-100)"), (2, "Bon (70-85)"), (3, "Moyen (55-70)"),
                                   (4, "Mauvais (40-55)"), (5, "Très mauvais (<40)")]}}


def profile(streets, since_year):
    streets = [streets] if isinstance(streets, str) else list(dict.fromkeys(streets))
    results, errors = [], []
    for key, step in [("awarded", lambda: awarded_contracts(streets, since_year)),
                      ("restrictions", lambda: road_restrictions(streets)),
                      ("requests_311", lambda: requests_311(streets, since_year))]:
        try:
            results.append(step())
        except Exception as error:
            errors.append(f"{key}: {type(error).__name__}")
    suppliers = {r["supplier"] for d in results if d["key"] == "awarded" for r in d["records"] if r.get("road_work")}
    for key, step in [("bids", lambda: seao_bids(streets, suppliers, since_year)),
                      ("planning", lambda: planning(streets, since_year)),
                      ("integrity", lambda: integrity(suppliers, streets)),
                      ("development", lambda: development(streets, since_year)),
                      ("traffic", lambda: traffic(streets)),
                      ("climate", lambda: climate.seasons(*CLIMATE_STATION, since_year, PAGES["climate"]))]:
        try:
            results.append(step())
        except Exception as error:
            errors.append(f"{key}: {type(error).__name__}")
    roles = {}
    for d in results:
        if d["key"] == "awarded":
            for r in d["records"]:
                roles.setdefault(r["supplier"], {"won": [], "bid": []})["won"].append((r["award_date"], r["description"]))
        if d["key"] == "bids":
            for r in d["records"]:
                for name in (r.get("bidder_names") or "").split("; "):
                    if name:
                        roles.setdefault(canonical_supplier(name), {"won": [], "bid": []})["bid"].append((r["date"], r["title"]))
                if r.get("supplier") and r.get("award_amount"):
                    roles.setdefault(r["supplier"], {"won": [], "bid": []})["won"].append((r["date"], r["title"]))
    try:
        results.append(debarment(set(roles) | suppliers, roles))
    except Exception as error:
        errors.append(f"debarment: {type(error).__name__}")
    return results, errors


def supplier_contracts(names, since_year):
    """Every Ville de Montréal contract awarded to the supplier or one of its other names (a supplier case, not a street case)."""
    from .common import company_base, company_matcher
    matches = company_matcher(names)
    bases = [company_base(n) for n in names if n and company_base(n)]

    def where(column):
        return "(" + " OR ".join(f'"{column}" ILIKE {_q("%" + b.replace(" ", "%") + "%")}' for b in bases) + ")" if bases else "FALSE"
    rows = []
    sources = [("central", "NOM DU FOURNISSEUR", "SELECT \"NOM DU FOURNISSEUR\" supplier, \"NUMERO\" number, \"DATE D'APPROBATION\" date, \"DESCRIPTION\" description, "
                           "\"SERVICE\" service, \"MONTANT\" amount FROM \"{rid}\" WHERE {where} AND \"DATE D'APPROBATION\" >= {since} LIMIT 400", "Fonctionnaires (pouvoirs délégués)"),
               ("executive", "FOURNISSEUR", 'SELECT "FOURNISSEUR" supplier, "NO DE DOSSIER" number, "DATE SIGNATURE" date, "OBJET" description, '
                             '"SERVICE" service, "MONTANT" amount, "NO DECISION" decision FROM "{rid}" WHERE {where} AND "DATE SIGNATURE" >= {since} LIMIT 400', "Comité exécutif"),
               ("boroughs", "FOURNISSEUR", 'SELECT "FOURNISSEUR" supplier, "NO DE DOSSIER" number, "DATE SIGNATURE" date, "OBJET" description, '
                            '"SERVICE" service, "MONTANT" amount, "NO DECISION" decision FROM "{rid}" WHERE {where} AND "DATE SIGNATURE" >= {since} LIMIT 400', "Conseils d'arrondissement")]
    for name, column, template, body in sources:
        query = template.replace("{rid}", RESOURCES[name]).replace("{where}", where(column)).replace("{since}", _q(f"{since_year}-01-01"))
        for r in _sql(f"supplier-{name}", query):
            if not matches(r.get("supplier")):
                continue
            description = " ".join(str(r.get("description") or "").split())
            date = _date(r.get("date"))
            rows.append({"city": NAME, "vendor": canonical_supplier(r.get("supplier")), "vendor_name": r.get("supplier"),
                         "document_number": r.get("number"), "type": body, "amount": _amount(r.get("amount")), "award_date": date,
                         "year": _year(date), "division": r.get("service"), "description": description[:600], "decision": r.get("decision"),
                         "road_work": any(w in description.lower() for w in ROAD_WORK_WORDS)})
    rows.sort(key=lambda r: r["award_date"] or "", reverse=True)
    result = dataset_result(
        key="city_awards", title=f"Montréal - contrats attribués à {names[0] if names else 'the supplier'}",
        source_url=PAGES["awarded"], since=since_year, records=rows,
        description="Contracts the Ville de Montréal (delegated authority, executive committee, borough councils) awarded to this "
                    "supplier or a name variant. Amounts are contract values, not payments.",
        fields=[("City", "city"), ("Vendor name", "vendor_name"), ("Document number", "document_number"), ("Awarding body", "type"),
                ("Award amount", "amount"), ("Award date", "award_date"), ("Division", "division"), ("Decision", "decision"),
                ("Description", "description")])
    result["total"] = len(rows)
    return result
