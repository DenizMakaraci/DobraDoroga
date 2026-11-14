"""Government of Canada open data for one supplier (a company and its other names).

Datasets:
  fed_contracts   Proactive Publication - Contracts over $10,000 (Treasury Board; every department; ~1.3M rows, 600 MB CSV).
                  Too large to download per case, so it is streamed once into a compact SQLite index keyed by canonical
                  vendor name (cache/federal-contracts.sqlite3) and refreshed in the background when older than a week.
  fed_grants      Proactive Disclosure - Grants and Contributions (2 GB CSV): same treatment, opt-in with ROADWATCH_FEDERAL_GRANTS=on.
  fed_lobbying    Registry of Lobbyists - monthly communication reports (Office of the Commissioner of Lobbying, its own open
                  data licence): client, registrant, officials (DPOH), institution and date of each communication.
  fed_ineligible  PSPC Ineligibility and Suspension List (HTML table on canada.ca).

Field ids follow the TBS data dictionaries (vendor_name, original_value, amendment_value, solicitation_procedure...).
Rows are matched to the supplier by canonical company name or by its suffix-free name as a prefix; the matched
'Vendor name' is kept on every row so a reviewer can spot an unrelated firm with a similar name.
"""
import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

from .common import USER_AGENT, cache_dir, canonical_company, company_base, company_matcher, dataset_result, fetch_cached

NAME = "Government of Canada"
KEY = "canada"
PAGES = {
    "fed_contracts": "https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b",
    "fed_grants": "https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013",
    "fed_lobbying": "https://lobbycanada.gc.ca/en/open-data/",
    "fed_ineligible": "https://www.canada.ca/en/public-services-procurement/services/standards-oversight/"
                      "supplier-integrity-compliance/ineligible-suspended-suppliers.html",
}
CONTRACTS_CSV = ("https://open.canada.ca/data/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/resource/"
                 "fac950c0-00d5-4ec1-a4d3-9cbebf98a305/download/contracts.csv")
GRANTS_CSV = ("https://open.canada.ca/data/dataset/432527ab-7aac-45b5-81d6-7597107a7013/resource/"
              "1d15a62f-5656-49ad-8c88-f40ce689d831/download/grants.csv")
COMMUNICATIONS_ZIP = "https://lobbycanada.gc.ca/media/mqbbmaqk/communications_ocl_cal.zip"
INDEX_TTL = 7 * 86400
INDEX_SINCE = int(os.environ.get("ROADWATCH_FEDERAL_SINCE", "2015"))
GRANTS_ENABLED = os.environ.get("ROADWATCH_FEDERAL_GRANTS", "").lower() in ("1", "on", "true", "yes")

PROCEDURES = {"AC": "Advance Contract Award Notice", "OB": "Competitive - open bidding", "ST": "Competitive - selective tendering",
              "TC": "Competitive - traditional", "TN": "Non-competitive"}
COMPETITIVE = {"OB", "ST", "TC"}
TENDERING_REASONS = {"00": "none stated", "05": "low dollar value", "21": "exclusive rights", "22": "extreme urgency",
                     "23": "additional deliveries", "24": "prototype purchase", "25": "subcontracting"}
COMMODITIES = {"G": "Goods", "S": "Services", "C": "Construction"}
INSTRUMENTS = {"C": "Contract", "A": "Amendment", "SOSA": "Standing offer / supply arrangement"}
AGREEMENTS = {"G": "Grant", "C": "Contribution", "O": "Other transfer payment"}
CONTRACT_FIELDS = ("reference_number", "procurement_id", "vendor_name", "vendor_postal_code", "buyer_name", "contract_date",
                   "economic_object_code", "description_en", "contract_period_start", "delivery_date", "contract_value",
                   "original_value", "amendment_value", "comments_en", "commodity_type", "commodity_code", "country_of_vendor",
                   "solicitation_procedure", "limited_tendering_reason", "trade_agreement", "former_public_servant",
                   "standing_offer_number", "instrument_type", "ministers_office", "number_of_bids", "award_criteria",
                   "reporting_period", "owner_org", "owner_org_title")
GRANT_FIELDS = ("ref_number", "amendment_number", "amendment_date", "agreement_type", "recipient_type", "recipient_legal_name",
                "recipient_operating_name", "recipient_city", "recipient_province", "recipient_postal_code", "prog_name_en",
                "prog_purpose_en", "agreement_title_en", "agreement_number", "agreement_value", "agreement_start_date",
                "agreement_end_date", "description_en", "owner_org", "owner_org_title")

csv.field_size_limit(10_000_000)


def _amount(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def _int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _year(value):
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _date(value):
    """'2024-03-28', '2024-03-28T..' or '3/28/2024' -> 'YYYY-MM-DD'; anything else is returned trimmed."""
    text = str(value or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}" if match else text[:10]


def _clean(value):
    return " ".join(str(value or "").split())


def _label(names):
    return names[0] if names else "the supplier"


# ---- federal CSV index ----------------------------------------------------------------------------------------

class IndexBuilding(Exception):
    """The index for a federal dataset is being built in the background; pull again later."""


_building, _failures, _lock = {}, {}, threading.Lock()


class _Stream(io.RawIOBase):
    """File-like view over an iterator of byte chunks, so csv can parse a 600 MB download without holding it in memory."""

    def __init__(self, chunks, progress):
        self.chunks, self.progress, self.buffer = iter(chunks), progress, b""

    def readable(self):
        return True

    def readinto(self, target):
        while not self.buffer:
            chunk = next(self.chunks, None)
            if chunk is None:
                return 0
            self.buffer = chunk
            self.progress["bytes"] += len(chunk)
        n = min(len(target), len(self.buffer))
        target[:n], self.buffer = self.buffer[:n], self.buffer[n:]
        return n


def _download(url):
    """The body of `url` in 1 MB chunks (replaced in tests)."""
    with httpx.Client(timeout=httpx.Timeout(60, read=600), follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            yield from response.iter_bytes(1 << 20)


def _index_path(name):
    return cache_dir() / f"federal-{name}.sqlite3"


def _build_index(name, url, key_columns, keep, year_of, since_year=None, progress=None):
    """Stream the CSV at `url` into cache/federal-<name>.sqlite3: one row per record dated `since_year` or later, keyed by
    the canonical company in the first non-empty of `key_columns`, keeping only the `keep` columns."""
    since_year = INDEX_SINCE if since_year is None else since_year
    progress = progress if progress is not None else {"bytes": 0, "rows": 0}
    path, tmp = _index_path(name), _index_path(name).with_suffix(".building")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(str(tmp))
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE rows (company TEXT NOT NULL, year INTEGER, body TEXT NOT NULL)")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    text = io.TextIOWrapper(io.BufferedReader(_Stream(_download(url), progress), 1 << 20), encoding="utf-8-sig", newline="", errors="replace")
    batch, total = [], 0
    for row in csv.DictReader(text):
        total += 1
        year = year_of(row)
        company = canonical_company(next((row.get(c) for c in key_columns if row.get(c)), ""))
        if not company or (year is not None and year < since_year):
            continue
        batch.append((company, year, json.dumps({k: row[k] for k in keep if row.get(k)}, ensure_ascii=False)))
        if len(batch) >= 5000:
            db.executemany("INSERT INTO rows VALUES (?, ?, ?)", batch)
            batch.clear()
            progress["rows"] = total
    db.executemany("INSERT INTO rows VALUES (?, ?, ?)", batch)
    db.execute("CREATE INDEX rows_company ON rows(company)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("built_at", datetime.now(timezone.utc).isoformat()), ("source_rows", str(total)),
                                                     ("since_year", str(since_year)), ("url", url)])
    db.commit()
    db.close()
    os.replace(tmp, path)
    return path


def build_contracts_index(since_year=None, progress=None):
    return _build_index("contracts", CONTRACTS_CSV, ("vendor_name",), CONTRACT_FIELDS,
                        lambda r: _year(r.get("contract_date")) or _year(r.get("reporting_period")), since_year, progress)


def build_grants_index(since_year=None, progress=None):
    return _build_index("grants", GRANTS_CSV, ("recipient_legal_name", "recipient_operating_name"), GRANT_FIELDS,
                        lambda r: _year(r.get("agreement_start_date")) or _year(r.get("amendment_date")), since_year, progress)


def _background(name, build):
    try:
        build(progress=_building[name])
        _failures.pop(name, None)
    except Exception as error:  # remembered so the next pull can say why the dataset is still missing
        _failures[name] = type(error).__name__
    finally:
        _building.pop(name, None)


def _ensure_index(name, build):
    """The index path when it exists. A missing or week-old index is (re)built in a background thread; while the first
    build runs, IndexBuilding is raised so the other datasets of the pull still come through."""
    path = _index_path(name)
    fresh = path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL
    if not fresh:
        with _lock:
            if name not in _building:
                _building[name] = {"bytes": 0, "rows": 0}
                threading.Thread(target=_background, args=(name, build), daemon=True).start()
    if path.exists():
        return path
    progress = _building.get(name) or {"bytes": 0, "rows": 0}
    failed = f"; the previous attempt failed ({_failures[name]}), retrying" if name in _failures else ""
    raise IndexBuilding(f"the federal {name} index is being built in the background ({progress['bytes'] // 1_000_000} MB read, "
                        f"{progress['rows']:,} rows scanned){failed} - pull again in a few minutes")


def _query(name, names):
    """(canonical company, record) for every indexed row naming the supplier or an alias."""
    matches = company_matcher(names)
    wanted = sorted({canonical_company(n) for n in names if n and str(n).strip()})
    bases = sorted({company_base(n) for n in names if n and len(company_base(n)) >= 6})
    if not wanted:
        return []
    clauses = ["company IN (%s)" % ",".join("?" * len(wanted))] + ["company LIKE ? ESCAPE '\\'"] * len(bases)
    params = wanted + [re.sub(r"([%_\\])", r"\\\1", b) + " %" for b in bases]
    db = sqlite3.connect(f"file:{_index_path(name)}?mode=ro", uri=True)
    try:
        rows = db.execute(f"SELECT company, body FROM rows WHERE {' OR '.join(clauses)}", params).fetchall()
    finally:
        db.close()
    return [(company, json.loads(body)) for company, body in rows if matches(company)]


def _insert_summary(result, title, lines):
    """Put a quotable totals block on page 1, between the header and the first record."""
    block = title + "\n" + "\n".join(lines)
    text = result["pages"][0]["text"]
    marker = "\n\nRecord 1\n"
    result["pages"][0]["text"] = (text.replace(marker, "\n\n" + block + marker, 1) if marker in text else text + "\n\n" + block)


# ---- fed_contracts ----------------------------------------------------------------------------------------------

def committed(record):
    """Money added by a disclosed row: the original value of a contract, the change of an amendment."""
    return record["amendment_value"] if record.get("is_amendment") else (record.get("original_value") or record.get("value") or 0)


def contracts(names, since_year):
    _ensure_index("contracts", build_contracts_index)
    rows = []
    for company, r in _query("contracts", names):
        date = _date(r.get("contract_date"))
        year = _year(date) or _year(r.get("reporting_period"))
        if year is not None and year < since_year:
            continue
        procedure = (r.get("solicitation_procedure") or "").strip().upper()
        reason = (r.get("limited_tendering_reason") or "").strip()
        instrument = (r.get("instrument_type") or "").strip().upper()
        amendment = _amount(r.get("amendment_value"))
        is_amendment = instrument == "A" or (not instrument and amendment != 0)
        rows.append({
            "vendor": company, "vendor_name": _clean(r.get("vendor_name")), "reference": r.get("reference_number"),
            "contract_number": r.get("procurement_id"), "department": r.get("owner_org_title") or r.get("owner_org"),
            "buyer": _clean(r.get("buyer_name")) or None, "date": date, "year": year,
            "period_start": _date(r.get("contract_period_start")) or None, "delivery": _date(r.get("delivery_date")) or None,
            "description": _clean(r.get("description_en"))[:300], "value": _amount(r.get("contract_value")),
            "original_value": _amount(r.get("original_value")), "amendment_value": amendment,
            "instrument": INSTRUMENTS.get(instrument) or ("Amendment" if is_amendment else "Contract"), "is_amendment": is_amendment,
            "procedure": PROCEDURES.get(procedure, procedure or None), "procedure_code": procedure or None,
            "competitive": (procedure in COMPETITIVE) if procedure else None,
            "tendering_reason": TENDERING_REASONS.get(reason, reason or None), "bids": _int(r.get("number_of_bids")),
            "commodity": COMMODITIES.get((r.get("commodity_type") or "").strip().upper(), r.get("commodity_type")),
            "commodity_code": r.get("commodity_code"), "postal_code": r.get("vendor_postal_code"), "country": r.get("country_of_vendor"),
            "former_public_servant": r.get("former_public_servant"), "ministers_office": r.get("ministers_office"),
            "award_criteria": r.get("award_criteria"), "reporting_period": r.get("reporting_period"),
            "comments": _clean(r.get("comments_en"))[:200],
        })
    rows.sort(key=lambda r: (r["date"] or "", r["reference"] or ""), reverse=True)
    result = dataset_result(
        key="fed_contracts", title=f"Government of Canada - contracts disclosed for {_label(names)}",
        source_url=PAGES["fed_contracts"], since=since_year, records=rows,
        description="Every contract and amendment over $10,000 that federal departments and agencies proactively published "
                    "with this supplier (or a name variant) as vendor. Values are commitments including taxes, not payments; "
                    "an amendment row's 'Amendment value' is the change and its 'Contract value' the new total.",
        fields=[("Vendor name", "vendor_name"), ("Reference", "reference"), ("Contract number", "contract_number"),
                ("Department", "department"), ("Buyer", "buyer"), ("Contract date", "date"), ("Period start", "period_start"),
                ("Delivery date", "delivery"), ("Description", "description"), ("Contract value", "value"),
                ("Original value", "original_value"), ("Amendment value", "amendment_value"), ("Instrument", "instrument"),
                ("Solicitation procedure", "procedure"), ("Limited tendering reason", "tendering_reason"),
                ("Number of bids", "bids"), ("Commodity", "commodity"), ("Commodity code", "commodity_code"),
                ("Vendor postal code", "postal_code"), ("Country", "country"), ("Former public servant", "former_public_servant"),
                ("Minister's office", "ministers_office"), ("Award criteria", "award_criteria"), ("Reporting period", "reporting_period"),
                ("Comments", "comments")])
    result["summary"] = contract_summary(rows)
    result["total"] = len(rows)
    lines = [f"{y['year']} | {y['rows']} rows | CAD {y['committed']:,.0f} committed" for y in result["summary"]["by_year"]]
    lines += [f"Department | {d['department']} | {d['rows']} rows | CAD {d['committed']:,.0f} committed" for d in result["summary"]["by_department"][:12]]
    lines += [f"Procedure | {p['procedure']} | {p['rows']} rows | CAD {p['committed']:,.0f} committed" for p in result["summary"]["by_procedure"]]
    _insert_summary(result, "Totals (all rows, not only the first page)", lines)
    return result


def contract_summary(rows):
    """Committed value by year, department and solicitation procedure over all rows."""
    by_year, by_department, by_procedure = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0])
    for r in rows:
        money = committed(r)
        for table, key in ((by_year, r.get("year")), (by_department, r.get("department") or "Unknown department"),
                           (by_procedure, r.get("procedure") or "Not stated")):
            if key is not None:
                table[key][0] += 1
                table[key][1] += money
    return {
        "by_year": [{"year": y, "rows": n, "committed": round(v, 2)} for y, (n, v) in sorted(by_year.items())],
        "by_department": [{"department": d, "rows": n, "committed": round(v, 2)}
                          for d, (n, v) in sorted(by_department.items(), key=lambda item: -item[1][1])],
        "by_procedure": [{"procedure": p, "rows": n, "committed": round(v, 2)}
                         for p, (n, v) in sorted(by_procedure.items(), key=lambda item: -item[1][1])],
        "committed": round(sum(committed(r) for r in rows), 2),
        "contracts": sum(1 for r in rows if not r.get("is_amendment")), "amendments": sum(1 for r in rows if r.get("is_amendment")),
    }


# ---- fed_grants -------------------------------------------------------------------------------------------------

def grants(names, since_year):
    _ensure_index("grants", build_grants_index)
    rows = []
    for company, r in _query("grants", names):
        start = _date(r.get("agreement_start_date"))
        year = _year(start) or _year(r.get("amendment_date"))
        if year is not None and year < since_year:
            continue
        rows.append({
            "vendor": company, "recipient": _clean(r.get("recipient_legal_name")), "operating_name": _clean(r.get("recipient_operating_name")) or None,
            "reference": r.get("ref_number"), "amendment": r.get("amendment_number"), "department": r.get("owner_org_title") or r.get("owner_org"),
            "program": _clean(r.get("prog_name_en")), "title": _clean(r.get("agreement_title_en"))[:200],
            "type": AGREEMENTS.get((r.get("agreement_type") or "").strip().upper(), r.get("agreement_type")),
            "value": _amount(r.get("agreement_value")), "start": start or None, "end": _date(r.get("agreement_end_date")) or None, "year": year,
            "city": r.get("recipient_city"), "province": r.get("recipient_province"), "description": _clean(r.get("description_en"))[:300],
        })
    rows.sort(key=lambda r: (r["start"] or "", r["reference"] or ""), reverse=True)
    result = dataset_result(
        key="fed_grants", title=f"Government of Canada - grants and contributions to {_label(names)}",
        source_url=PAGES["fed_grants"], since=since_year, records=rows,
        description="Transfer payments (grants, contributions) that federal institutions proactively disclosed with this "
                    "company or a name variant as recipient. Agreement values are maximum amounts, not money paid.",
        fields=[("Recipient", "recipient"), ("Operating name", "operating_name"), ("Reference", "reference"), ("Amendment", "amendment"),
                ("Department", "department"), ("Program", "program"), ("Agreement title", "title"), ("Type", "type"),
                ("Agreement value", "value"), ("Start", "start"), ("End", "end"), ("City", "city"), ("Province", "province"),
                ("Description", "description")])
    result["total"] = len(rows)
    return result


# ---- fed_lobbying -----------------------------------------------------------------------------------------------

def _communications():
    """The Commissioner of Lobbying's communication-report export (one zip, all reports since 2008), cached for a day."""
    raw = fetch_cached("federal-communications.zip", COMMUNICATIONS_ZIP, ttl=86400, max_bytes=400_000_000)
    return zipfile.ZipFile(io.BytesIO(raw))


def _pick(headers, *needles):
    """The shortest header containing every needle (case-insensitive), or None."""
    hits = [h for h in headers if all(n in h.upper() for n in needles)]
    return min(hits, key=len) if hits else None


def _csv_member(archive, pattern):
    member = next((m for m in archive.namelist() if m.lower().endswith(".csv") and re.search(pattern, m, re.I)), None)
    if not member:
        return []
    return list(csv.DictReader(io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="", errors="replace")))


def lobbying(names, since_year):
    matches = company_matcher(names)
    archive = _communications()
    primary = _csv_member(archive, r"primary")
    if not primary:
        raise ValueError("communication export without a primary file")
    h = list(primary[0].keys())
    col_id, col_client, col_date = _pick(h, "COMLOG") or h[0], _pick(h, "CLIENT", "NM") or _pick(h, "CLIENT"), _pick(h, "COMM", "DATE")
    col_type = _pick(h, "REG_TYPE", "EN") or _pick(h, "TYPE", "EN")
    col_last, col_first = _pick(h, "RGSTRNT", "LAST") or _pick(h, "REGISTRANT", "LAST"), _pick(h, "RGSTRNT", "1ST") or _pick(h, "REGISTRANT", "FIRST")
    hits = {}
    for r in primary:
        client = _clean(r.get(col_client))
        date = _date(r.get(col_date))
        if not matches(client) or (_year(date) or since_year) < since_year:
            continue
        hits[r.get(col_id)] = {
            "vendor": canonical_company(client), "client": client, "date": date, "year": _year(date),
            "registrant": " ".join(x for x in (_clean(r.get(col_first)), _clean(r.get(col_last))) if x) or None,
            "type": _clean(r.get(col_type)) or None, "institutions": "", "officials": "", "subjects": "", "report": r.get(col_id),
        }
    if hits:
        officials, institutions, subjects = defaultdict(list), defaultdict(list), defaultdict(list)
        dpoh = _csv_member(archive, r"dpoh")
        if dpoh:
            d = list(dpoh[0].keys())
            c_id, c_last, c_first = _pick(d, "COMLOG") or d[0], _pick(d, "DPOH", "LAST"), _pick(d, "DPOH", "FIRST") or _pick(d, "DPOH", "1ST")
            c_title, c_inst = _pick(d, "TITLE", "EN"), _pick(d, "INSTITUTION", "EN") or _pick(d, "INSTITUTION")
            for r in dpoh:
                if r.get(c_id) in hits:
                    name = " ".join(x for x in (_clean(r.get(c_first)), _clean(r.get(c_last))) if x)
                    title = _clean(r.get(c_title))
                    officials[r[c_id]].append(f"{name}, {title}" if title else name)
                    if _clean(r.get(c_inst)):
                        institutions[r[c_id]].append(_clean(r.get(c_inst)))
        for r in _csv_member(archive, r"subject"):
            keys = list(r.keys())
            c_id, c_subject = _pick(keys, "COMLOG") or keys[0], _pick(keys, "SUBJECT", "EN") or _pick(keys, "SUBJECT")
            if r.get(c_id) in hits and _clean(r.get(c_subject)):
                subjects[r[c_id]].append(_clean(r.get(c_subject)))
        for key, hit in hits.items():
            hit["officials"] = "; ".join(dict.fromkeys(officials[key]))[:600]
            hit["institutions"] = "; ".join(dict.fromkeys(institutions[key]))[:400]
            hit["subjects"] = "; ".join(dict.fromkeys(subjects[key]))[:300]
    rows = sorted(hits.values(), key=lambda r: r["date"] or "", reverse=True)
    result = dataset_result(
        key="fed_lobbying", title=f"Registry of Lobbyists - communications reported for {_label(names)}",
        source_url=PAGES["fed_lobbying"], since=since_year, records=rows,
        description="Monthly communication reports in which this company (or a name variant) is the client, corporation or "
                    "organization: who met which designated public office holders, at which institution, on what subject and when. "
                    "Lobbying is lawful and registered; timing relative to awards is what a reviewer checks.",
        fields=[("Client", "client"), ("Communication date", "date"), ("Registrant", "registrant"), ("Registration type", "type"),
                ("Institutions", "institutions"), ("Officials", "officials"), ("Subjects", "subjects"), ("Report", "report")])
    result["total"] = len(rows)
    return result


# ---- fed_ineligible ---------------------------------------------------------------------------------------------

class _Tables(HTMLParser):
    """Every <table> on a page as rows of cell texts."""

    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.tables:
            self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def _long_date(text):
    """'June 17, 2020 (subject to possible extension)' -> '2020-06-17'."""
    match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text or "")
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat() if match else None
    except ValueError:
        return None


def _name_variants(company):
    """'3682021 Canada Inc. (Sports Max)' / 'X Ltd (also doing business as Y)' -> the legal name and each trade name."""
    variants = [company] + [re.sub(r"^(also )?(doing business as|dba|o/a)\s+", "", inner, flags=re.I)
                            for inner in re.findall(r"\(([^)]+)\)", company)]
    variants.append(company.split("(")[0])
    return [v.strip() for v in variants if v and v.strip()]


def ineligible_entries():
    page = fetch_cached("federal-ineligible.html", PAGES["fed_ineligible"], ttl=86400).decode("utf-8", "replace")
    parser = _Tables()
    parser.feed(page)
    entries = []
    for table in parser.tables:
        if not table or "supplier" not in " ".join(table[0]).lower():
            continue
        for cells in table[1:]:
            if len(cells) < 3 or not cells[0]:
                continue
            cells += [""] * (5 - len(cells))
            entries.append({"company": cells[0], "address": cells[1], "status": cells[2], "from": _long_date(cells[3]),
                            "until": _long_date(cells[4]), "until_note": cells[4]})
    return entries


def ineligible(names):
    matches = company_matcher(names)
    entries = ineligible_entries()
    rows = [{"vendor": next(canonical_company(v) for v in _name_variants(e["company"]) if matches(v)), **e}
            for e in entries if any(matches(v) for v in _name_variants(e["company"]))]
    result = dataset_result(
        key="fed_ineligible", title="PSPC Ineligibility and Suspension List - entries matching the case's company names",
        source_url=PAGES["fed_ineligible"], records=rows,
        description=f"Suppliers declared ineligible or suspended from federal contracts under the Ineligibility and Suspension "
                    f"Policy, checked against the case's company names ({len(entries)} entries on the list). Ineligibility applies "
                    "from the start date; contracts awarded earlier, or to a different company with a similar name, are lawful.",
        fields=[("Company", "company"), ("Address", "address"), ("Status", "status"), ("From", "from"), ("Until", "until"),
                ("Until (as published)", "until_note")])
    result["total"] = len(rows)
    result["checked"] = len(entries)
    return result


# ---- profile ------------------------------------------------------------------------------------------------------

def profile(names, since_year):
    """Run every federal connector for a supplier; a failing dataset is reported, not fatal."""
    results, errors = [], []
    steps = [("fed_contracts", lambda: contracts(names, since_year)), ("fed_lobbying", lambda: lobbying(names, since_year)),
             ("fed_ineligible", lambda: ineligible(names))]
    if GRANTS_ENABLED:
        steps.insert(1, ("fed_grants", lambda: grants(names, since_year)))
    for key, step in steps:
        try:
            results.append(step())
        except IndexBuilding as pending:
            errors.append(f"{key}: {pending}")
        except Exception as error:  # network/format problems should not lose the other datasets
            errors.append(f"{key}: {type(error).__name__}")
    return results, errors
