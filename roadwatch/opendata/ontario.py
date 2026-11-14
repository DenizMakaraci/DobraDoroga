"""Province of Ontario open data for one supplier.

  on_payments   Public Accounts of Ontario - Detailed Schedule of Payments (formerly Volume 3): every ministry payment of
                $50,000 or more to a vendor or transfer-payment recipient, one CSV per fiscal year (April-March) on the
                Ontario Data Catalogue (CKAN). Queried with the datastore's full-text search, then matched locally by
                canonical company name. Amounts include sales tax; smaller payments are lumped as "Accounts under $50,000".
Published under the Ontario.ca terms of use (the Public Accounts are not under the Open Government Licence - Ontario).
"""
import hashlib
import re

from .common import canonical_company, company_base, company_matcher, dataset_result, fetch_cached, load_json

NAME = "Ontario"
KEY = "on"
CKAN = "https://data.ontario.ca/api/3/action"
PACKAGE = "public-accounts-detailed-schedule-of-payments"
PAGES = {"on_payments": "https://data.ontario.ca/dataset/public-accounts-detailed-schedule-of-payments"}


def _amount(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def resources():
    """English CSV resources of the Public Accounts package as [(fiscal year start, label, resource id)], newest first."""
    package = load_json(fetch_cached("ontario-public-accounts-package.json", f"{CKAN}/package_show", params={"id": PACKAGE}, ttl=86400))
    found = {}
    for r in package["result"]["resources"]:
        match = re.search(r"(20\d{2})\s*[-–]\s*(\d{2,4})", r.get("name") or "")
        if r.get("format") != "CSV" or not match or (r.get("language") or "english").lower().startswith("fr"):
            continue
        start = int(match.group(1))
        found.setdefault(start, (f"{start}-{str(start + 1)[-2:]}", r["id"]))
    return [(start, label, rid) for start, (label, rid) in sorted(found.items(), reverse=True)]


def _search(resource_id, query):
    """Full-text datastore search for `query` in one fiscal year, cached for a day."""
    key = hashlib.sha1(query.encode()).hexdigest()[:10]
    data = load_json(fetch_cached(f"ontario-payments-{resource_id[:8]}-{key}.json", f"{CKAN}/datastore_search",
                                  params={"resource_id": resource_id, "q": query, "limit": 500}, ttl=86400))
    return data["result"]["records"]


def _field(record, *names):
    lowered = {k.lower(): v for k, v in record.items()}
    return next((lowered[n.lower()] for n in names if n.lower() in lowered), None)


def _queries(names):
    """Distinct full-text queries: the suffix-free base of each name (at most its first three words)."""
    return list(dict.fromkeys(" ".join(company_base(n).split(" ")[:3]) for n in names if n and company_base(n)))


def payments(names, since_year):
    matches = company_matcher(names)
    rows, years = [], []
    for start, label, rid in resources():
        if start < since_year:
            continue
        years.append(label)
        seen = set()
        for query in _queries(names):
            for r in _search(rid, query):
                recipient = " ".join(str(_field(r, "Recipient") or "").split())
                key = (r.get("_id"), recipient)
                if key in seen or not matches(recipient):
                    continue
                seen.add(key)
                detail = _field(r, "Payment Detail", "Payment detail") or ""
                extra = _field(r, "Additional Detail") or ""
                rows.append({
                    "vendor": canonical_company(recipient), "recipient": recipient, "fiscal_year": label,
                    "ministry": " ".join(str(_field(r, "Ministry") or "").split()), "category": _field(r, "Category"),
                    "detail": None if detail in ("", "No Value", "No value") else detail,
                    "additional_detail": None if extra in ("", "No Value", "No value") else extra,
                    "amount": _amount(_field(r, "Amount $", "Amount")),
                })
    rows.sort(key=lambda r: (r["fiscal_year"], r["amount"]), reverse=True)
    result = dataset_result(
        key="on_payments", title=f"Public Accounts of Ontario - payments to {names[0] if names else 'the supplier'}",
        source_url=PAGES["on_payments"], since=since_year, records=rows,
        description="Payments of $50,000 or more by Ontario ministries to this company or a name variant, by fiscal year "
                    f"(April to March; years searched: {', '.join(reversed(years)) or 'none published for the period'}). "
                    "These are actual payments including sales tax, as published in the Public Accounts; smaller amounts are not itemised.",
        fields=[("Recipient", "recipient"), ("Fiscal year", "fiscal_year"), ("Ministry", "ministry"), ("Category", "category"),
                ("Payment detail", "detail"), ("Additional detail", "additional_detail"), ("Amount", "amount")])
    result["total"] = len(rows)
    result["summary"] = {"by_year": [{"year": y, "amount": round(sum(r["amount"] for r in rows if r["fiscal_year"] == y), 2)} for y in sorted({r["fiscal_year"] for r in rows})],
                         "paid": round(sum(r["amount"] for r in rows), 2)}
    return result
