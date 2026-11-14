"""Supplier cases: federal/provincial/municipal connectors and cross-government indicators - all offline."""
import csv
import io
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from roadwatch.analysis import citation_valid
from roadwatch.opendata import canada, ontario, toronto, vendor
from roadwatch.opendata.common import company_base, company_matcher
from roadwatch.server import app

VENDOR = "Northbridge Advisory Inc."  # fictional
CONTRACT_HEADER = ["reference_number", "procurement_id", "vendor_name", "vendor_postal_code", "buyer_name", "contract_date",
                   "economic_object_code", "description_en", "description_fr", "contract_period_start", "delivery_date",
                   "contract_value", "original_value", "amendment_value", "comments_en", "comments_fr", "commodity_type",
                   "commodity_code", "country_of_vendor", "solicitation_procedure", "limited_tendering_reason", "trade_agreement",
                   "former_public_servant", "standing_offer_number", "instrument_type", "ministers_office", "number_of_bids",
                   "award_criteria", "reporting_period", "owner_org", "owner_org_title"]
CBSA, SSC = "Canada Border Services Agency", "Shared Services Canada"


def contract(ref, number, date, original, *, department=CBSA, procedure="TN", reason="", bids="", instrument="C",
             value=None, amendment="0", vendor=VENDOR, fps="N", description="Informatics professional services"):
    return {"reference_number": ref, "procurement_id": number, "vendor_name": vendor, "contract_date": date,
            "description_en": description, "contract_value": str(value if value is not None else original), "original_value": str(original),
            "amendment_value": amendment, "commodity_type": "S", "solicitation_procedure": procedure, "limited_tendering_reason": reason,
            "former_public_servant": fps, "instrument_type": instrument, "number_of_bids": bids,
            "reporting_period": f"{date[:4]}-{int(date[:4]) + 1}-Q1", "owner_org": department.lower()[:6], "owner_org_title": department}


CONTRACTS = [
    contract("A-1", "NB-100", "2023-03-29", 2_000_000, reason="21", bids="1", description="Line one\nline two of the description"),
    contract("A-2", "NB-100", "2023-10-02", 2_000_000, instrument="A", value=3_500_000, amendment="1500000", vendor="NORTHBRIDGE ADVISORY INC"),
    contract("A-3", "NB-101", "2024-03-15", 38_000, reason="05"),
    contract("A-4", "NB-102", "2024-03-20", 36_500, reason="05"),
    contract("A-5", "NB-103", "2024-04-10", 39_000, reason="05"),
    contract("A-6", "NB-104", "2024-03-28", 120_000, procedure="OB", bids="1"),
    contract("A-7", "NB-105", "2024-09-01", 50_000, reason="22"),
    contract("B-1", "SSC-1", "2022-03-30", 500_000, department=SSC, procedure="OB", bids="1", fps="Y"),
    contract("B-2", "SSC-2", "2022-06-15", 250_000, department=SSC, procedure="TC", bids="1"),
    contract("B-3", "SSC-3", "2021-11-02", 80_000, department=SSC, procedure="OB", bids="4", vendor="1234567 Canada Inc"),
    contract("O-1", "OLD-1", "2012-05-02", 30_000, department="Old Department"),          # before the index's since-year
    contract("X-1", "X-1", "2024-05-02", 30_000, vendor="Northbridge Roofing Ltd"),       # similar name, different company
]


def csv_bytes(header, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def communications_zip():
    primary = csv_bytes(["COMLOG_ID", "CLIENT_ORG_CORP_NUM", "CLIENT_ORG_CORP_NM", "CLIENT_ORG_CORP_NM_AN", "RGSTRNT_LAST_NM", "RGSTRNT_1ST_NM",
                         "REG_TYPE_CD", "REG_TYPE_EN", "COMM_DATE", "SUBMISSION_DATE"], [
        {"COMLOG_ID": "9001", "CLIENT_ORG_CORP_NM": "Northbridge Advisory Inc.", "RGSTRNT_LAST_NM": "Doe", "RGSTRNT_1ST_NM": "Jane",
         "REG_TYPE_EN": "Consultant", "COMM_DATE": "2023-01-18"},
        {"COMLOG_ID": "9002", "CLIENT_ORG_CORP_NM": "Some Other Client", "RGSTRNT_LAST_NM": "Roe", "RGSTRNT_1ST_NM": "Rick",
         "REG_TYPE_EN": "In-house Corporation", "COMM_DATE": "2023-02-01"},
        {"COMLOG_ID": "9003", "CLIENT_ORG_CORP_NM": "Northbridge Advisory Inc.", "RGSTRNT_LAST_NM": "Doe", "RGSTRNT_1ST_NM": "Jane",
         "REG_TYPE_EN": "Consultant", "COMM_DATE": "2012-01-18"},
    ])
    dpoh = csv_bytes(["COMLOG_ID", "DPOH_LAST_NM", "DPOH_FIRST_NM", "DPOH_TITLE_EN", "DPOH_TITLE_FR", "BRANCH_UNIT_EN", "INSTITUTION_EN", "INSTITUTION_FR"], [
        {"COMLOG_ID": "9001", "DPOH_LAST_NM": "Roy", "DPOH_FIRST_NM": "Alex", "DPOH_TITLE_EN": "Director General", "INSTITUTION_EN": "Canada Border Services Agency (CBSA)"},
        {"COMLOG_ID": "9002", "DPOH_LAST_NM": "X", "DPOH_FIRST_NM": "Y", "DPOH_TITLE_EN": "Minister", "INSTITUTION_EN": "Finance Canada"},
    ])
    subjects = csv_bytes(["COMLOG_ID", "SUBJECT_MATTER_EN", "SUBJECT_MATTER_FR"], [{"COMLOG_ID": "9001", "SUBJECT_MATTER_EN": "Government Procurement"}])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Communication_PrimaryExport.csv", primary)
        archive.writestr("Communication_DpohExport.csv", dpoh)
        archive.writestr("Communication_SubjectMattersExport.csv", subjects)
    return buffer.getvalue()


INELIGIBLE_HTML = """<html><body><h1>Ineligible and suspended suppliers</h1>
<table class="table"><thead><tr><th>Supplier name</th><th>Address</th><th>Status</th><th>Start date of ineligibility or suspension</th><th>End date of ineligibility or suspension</th></tr></thead>
<tbody><tr><td>Example Industries Limited</td><td>Montreal (Quebec)</td><td>Ineligible</td><td>June 17, 2020</td><td>June 17, 2030</td></tr>
<tr><td>Northbridge Advisory Inc.</td><td>Ottawa (Ontario)</td><td>Ineligible</td><td>June 6, 2024</td><td>June 6, 2031 (subject to possible extension)</td></tr>
<tr><td>7654321 Canada Inc. (also doing business as Sports Max)</td><td>Pointe-&agrave;-la-Croix (Quebec)</td><td>Suspended</td><td>July 24, 2023</td><td>July 24, 2033</td></tr>
</tbody></table></body></html>"""

ONTARIO_ROWS = {
    "rid2024": [{"_id": 1, "Amount $": "257,640", "Ministry": "Ministry of Health", "Category": "Other Payments", "Payment Detail": "No Value",
                 "Recipient": "Northbridge Advisory Inc", "Statutory": "No Value", "Additional Detail": "No Value"},
                {"_id": 2, "Amount $": "99,000", "Ministry": "Ministry of Health", "Category": "Other Payments", "Payment Detail": "No Value",
                 "Recipient": "Northbridge Roofing Ltd", "Statutory": "No Value"}],
    "rid2023": [{"_id": 5, "Amount $": "60000", "Ministry": "Treasury Board Secretariat", "Category": "Other Payments", "Payment detail": "Advisory",
                 "Recipient": "NORTHBRIDGE ADVISORY INC.", "Statutory": "No Value"}],
}
TORONTO_AWARDED = [
    {"Document Number": "T-1", "RFx (Solicitation) Type": "RFP", "Successful Supplier": "Northbridge Advisory Inc.", "Award": "410,000",
     "Award Authority Obtained Date": "2024-02-01", "Division": "Technology Services", "Wards": "", "Solicitation Document Description": "IT advisory services"},
    {"Document Number": "T-2", "RFx (Solicitation) Type": "RFT", "Successful Supplier": "Someone Else Ltd", "Award": "900,000",
     "Award Authority Obtained Date": "2024-02-01", "Division": "ECS", "Wards": "", "Solicitation Document Description": "Road resurfacing"},
]
TORONTO_NONCOMP = [{"Workspace Number": "77", "Reason": "Proprietary", "Contract Date": "2024-06-02", "Supplier Name": "Northbridge Advisory Inc",
                    "Contract Amount": "70000", "Division": "Technology Services"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADWATCH_DB", str(tmp_path / "case.sqlite3"))
    monkeypatch.setenv("ROADWATCH_CACHE", str(tmp_path / "cache"))
    for name in [n for n in os.environ if n.startswith("ROADWATCH_") and n.endswith(("_MODEL", "_API_KEY", "_LABEL"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ROADWATCH_ACCESS_KEY", raising=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def offline_federal(client, monkeypatch):
    data = csv_bytes(CONTRACT_HEADER, CONTRACTS)
    monkeypatch.setattr(canada, "_download", lambda url: iter([data[:97], data[97:]]))   # split mid-row on purpose
    canada.build_contracts_index()
    fixtures = {"federal-communications.zip": communications_zip(), "federal-ineligible.html": INELIGIBLE_HTML.encode()}
    monkeypatch.setattr(canada, "fetch_cached", lambda name, url, **kw: fixtures[name])


@pytest.fixture
def offline_provincial(monkeypatch):
    monkeypatch.setattr(ontario, "resources", lambda: [(2024, "2024-25", "rid2024"), (2023, "2023-24", "rid2023"), (2014, "2014-15", "rid2014")])
    monkeypatch.setattr(ontario, "_search", lambda rid, q: ONTARIO_ROWS.get(rid, []))
    tables = {"awarded": TORONTO_AWARDED, "non_competitive": TORONTO_NONCOMP}
    monkeypatch.setattr(toronto, "_csv", lambda name, resource: tables[name])
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("lobbyactivity-active.xml", "<?xml version='1.0'?><ROWSET></ROWSET>")
    monkeypatch.setattr(toronto, "_lobby_zip", lambda: zipfile.ZipFile(io.BytesIO(empty.getvalue())))


def vendor_case(client, **extra):
    payload = {"name": "Northbridge across governments", "contractor": VENDOR, "anchor": "vendor",
               "vendor_aliases": ["1234567 Canada Inc"], "province": "on", **extra}
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---- matching ------------------------------------------------------------------------------------------------

def test_company_matcher_prefix_and_suffix_variants():
    matches = company_matcher([VENDOR])
    assert matches("NORTHBRIDGE ADVISORY INC") and matches("Northbridge Advisory Incorporated") and matches("northbridge advisory")
    assert matches("Northbridge Advisory Consulting Ltd")          # name-prefix variant is surfaced, with its own vendor_name
    assert not matches("Northbridge Roofing Ltd") and not matches("North Bridge Advisory Inc")
    assert company_base("Canada Bread Company Limited") == "CANADA BREAD"
    short = company_matcher(["ABC Ltd"])
    assert short("ABC Limited") and not short("ABC Roofing Ltd")   # short bases match exactly only


# ---- federal connectors ---------------------------------------------------------------------------------------

def test_contract_index_streams_and_filters(client, offline_federal):
    result = canada.contracts([VENDOR, "1234567 Canada Inc"], 2015)
    refs = [r["reference"] for r in result["records"]]
    assert "O-1" not in refs and "X-1" not in refs and "B-3" in refs           # since-year cut; similar-name firm excluded; alias included
    assert result["total"] == 10 and result["records"][0]["reference"] == "A-7"  # newest first
    amendment = next(r for r in result["records"] if r["reference"] == "A-2")
    assert amendment["is_amendment"] and amendment["amendment_value"] == 1_500_000 and amendment["value"] == 3_500_000
    first = next(r for r in result["records"] if r["reference"] == "A-1")
    assert first["procedure"] == "Non-competitive" and first["tendering_reason"] == "exclusive rights" and first["bids"] == 1
    assert first["description"] == "Line one line two of the description"      # multi-line quoted field parsed as one record
    summary = result["summary"]
    assert summary["contracts"] == 9 and summary["amendments"] == 1
    assert {y["year"]: y["committed"] for y in summary["by_year"]}[2023] == 3_500_000
    assert "2023 | 2 rows | CAD 3,500,000 committed" in result["pages"][0]["text"]
    assert "Procedure | Non-competitive |" in result["pages"][0]["text"]


def test_index_build_runs_in_background_and_reports_progress(client, monkeypatch):
    data = csv_bytes(CONTRACT_HEADER, CONTRACTS)
    monkeypatch.setattr(canada, "_download", lambda url: iter([data]))
    with pytest.raises(canada.IndexBuilding) as pending:
        canada._ensure_index("contracts", canada.build_contracts_index)
    assert "being built in the background" in str(pending.value)
    for _ in range(100):
        if canada._index_path("contracts").exists() and "contracts" not in canada._building:
            break
        time.sleep(0.05)
    assert canada._ensure_index("contracts", canada.build_contracts_index) == canada._index_path("contracts")
    assert canada.contracts([VENDOR], 2015)["total"] == 9


def test_profile_reports_pending_index_without_losing_other_datasets(client, monkeypatch):
    monkeypatch.setattr(canada, "_download", lambda url: (_ for _ in ()).throw(RuntimeError("offline")))
    fixtures = {"federal-communications.zip": communications_zip(), "federal-ineligible.html": INELIGIBLE_HTML.encode()}
    monkeypatch.setattr(canada, "fetch_cached", lambda name, url, **kw: fixtures[name])
    results, errors = canada.profile([VENDOR], 2015)
    assert [r["key"] for r in results] == ["fed_lobbying", "fed_ineligible"]
    assert errors and errors[0].startswith("fed_contracts: the federal contracts index is being built")
    for _ in range(100):
        if "contracts" not in canada._building:
            break
        time.sleep(0.05)
    assert canada._failures.get("contracts") == "RuntimeError"


def test_lobbying_joins_officials_and_subjects(client, offline_federal):
    result = canada.lobbying([VENDOR], 2015)
    assert result["total"] == 1                                               # 2012 report cut by since-year, other client excluded
    row = result["records"][0]
    assert row["date"] == "2023-01-18" and row["registrant"] == "Jane Doe" and row["type"] == "Consultant"
    assert row["officials"] == "Alex Roy, Director General" and row["institutions"] == "Canada Border Services Agency (CBSA)"
    assert row["subjects"] == "Government Procurement" and "Report: 9001" in result["pages"][0]["text"]


def test_ineligible_list_parsing(client, offline_federal):
    entries = canada.ineligible_entries()
    assert len(entries) == 3 and entries[1]["from"] == "2024-06-06" and entries[1]["until"] == "2031-06-06"
    assert "subject to possible extension" in entries[1]["until_note"]
    assert canada._name_variants(entries[2]["company"])[1] == "Sports Max"
    result = canada.ineligible([VENDOR])
    assert result["total"] == 1 and result["checked"] == 3 and result["records"][0]["status"] == "Ineligible"
    assert canada.ineligible(["Sports Max"])["total"] == 1                     # trade name inside parentheses


# ---- provincial / municipal -----------------------------------------------------------------------------------

def test_ontario_payments_match_locally(client, offline_provincial):
    result = ontario.payments([VENDOR], 2020)
    assert result["total"] == 2 and {r["fiscal_year"] for r in result["records"]} == {"2024-25", "2023-24"}
    assert all(r["recipient"].upper().startswith("NORTHBRIDGE ADVISORY") for r in result["records"])
    assert result["records"][0]["amount"] == 257_640 and result["summary"]["paid"] == 317_640
    detail = next(r for r in result["records"] if r["fiscal_year"] == "2023-24")
    assert detail["detail"] == "Advisory"                                      # case-insensitive column names across years


def test_ontario_resources_parse_package(monkeypatch):
    package = {"result": {"resources": [
        {"name": "2024-2025", "format": "CSV", "language": "english", "id": "a"}, {"name": "2024-2025", "format": "CSV", "language": "french", "id": "b"},
        {"name": "Detailed Scheduled Payments: 2017-18", "format": "CSV", "language": "english", "id": "c"}, {"name": "", "format": "", "id": "d"}]}}
    monkeypatch.setattr(ontario, "fetch_cached", lambda *a, **k: json.dumps(package).encode())
    assert ontario.resources() == [(2024, "2024-25", "a"), (2017, "2017-18", "c")]


def test_toronto_supplier_awards(client, offline_provincial):
    result = toronto.supplier_awards([VENDOR], 2015)
    assert result["key"] == "city_awards" and result["total"] == 1 and result["records"][0]["document_number"] == "T-1"
    assert result["records"][0]["city"] == "Toronto" and result["records"][0]["year"] == 2024


# ---- the case end to end ----------------------------------------------------------------------------------------

def test_vendor_case_validation(client):
    assert client.post("/api/projects", json={"name": "No company", "anchor": "vendor"}).status_code == 422
    assert client.post("/api/projects", json={"name": "No road", "road": "  "}).status_code == 422
    project = vendor_case(client)
    assert project["anchor"] == "vendor" and project["vendor"] == {"name": VENDOR, "aliases": ["1234567 Canada Inc"], "province": "on"}
    assert project["road"] and project["locations"] == []
    assert client.get("/api/projects").json()[0]["anchor"] == "vendor"
    assert client.get("/api/provinces").json()[0]["key"] == "on"


def test_pull_runs_every_tier_and_indicators_cite_the_records(client, offline_federal, offline_provincial):
    project = vendor_case(client)
    pulled = client.post(f"/api/projects/{project['id']}/open-data", json={"since_year": 2015})
    assert pulled.status_code == 200, pulled.text
    project = pulled.json()
    keys = [d["key"] for d in project["open_data"]]
    assert keys == ["fed_contracts", "fed_lobbying", "fed_ineligible", "on_payments", "city_awards", "city_non_competitive", "city_lobbying"]
    assert project["open_data"][0]["errors"] == [] and project["open_data"][0]["vendor_names"] == [VENDOR, "1234567 Canada Inc"]
    assert project["open_data"][0]["city"] == "Government of Canada, Ontario and Toronto"
    assert len([d for d in project["evidence"] if d["kind"] == "open_data"]) == 7
    assert "Toronto Lobbyist Registry - Northbridge Advisory Inc. as client" in [d["title"] for d in project["evidence"]]

    report = client.post(f"/api/projects/{project['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    titles = {f["title"] for f in report["findings"]}
    expected = {
        "A company in this case is on a register of suppliers barred from public contracts",
        "Most of the supplier's federal contracts were awarded without competition",
        "Contracts that grew well beyond their original value through amendments",
        "Several small contracts from one department within a short period",
        "Competitive tenders that drew a single bid",
        "Lobbying communications with a department in the year before it awarded the supplier a contract",
        "Contracts cluster in March, the last month of the federal fiscal year",
        "The supplier is paid by more than one level of government in the same years",
        "Contracts flagged as awarded to a former public servant receiving a pension",
        "The contracting party is a numbered company",
    }
    assert expected <= titles, expected - titles
    for finding in report["findings"]:
        assert finding["citations"] and all(citation_valid(c, project) for c in finding["citations"]), finding["title"]
        assert finding["alternative_explanation"] and finding["next_step"]
    barred = next(f for f in report["findings"] if f["title"].startswith("A company in this case is on a register"))
    assert "A-7" in barred["explanation"] and "Ontario's Public Accounts record CAD 257,640" in barred["explanation"]
    splitting = next(f for f in report["findings"] if f["title"].startswith("Several small"))
    assert "3 separate contracts" in splitting["explanation"] and "2024-03-15 and 2024-04-10" in splitting["explanation"]
    lobbying = next(f for f in report["findings"] if f["title"].startswith("Lobbying"))
    assert "70 days before contract A-1" in lobbying["explanation"]
    assert any("Sole-source justifications" in g for g in report["missing_records"])
    assert not any("street" in g.lower() for g in report["missing_records"])
    assert any("company name" in l for l in report["limitations"])


def test_renaming_the_supplier_flags_a_stale_pull(client, offline_federal, offline_provincial):
    project = vendor_case(client, province="")
    project = client.post(f"/api/projects/{project['id']}/open-data", json={"since_year": 2015}).json()
    assert [d["key"] for d in project["open_data"]] == ["fed_contracts", "fed_lobbying", "fed_ineligible"]
    report = client.post(f"/api/projects/{project['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    assert any("No province chosen" in g for g in report["missing_records"])
    revision = project["revision"]
    updated = client.put(f"/api/projects/{project['id']}/vendor", json={"name": VENDOR, "aliases": ["Northbridge Advisory Group"], "province": "on"})
    assert updated.status_code == 200 and updated.json()["revision"] == revision + 1
    assert updated.json()["vendor"]["aliases"] == ["Northbridge Advisory Group"] and updated.json()["contractor"] == VENDOR
    report = client.post(f"/api/projects/{project['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    assert any("Names were added after the last open-data pull" in g for g in report["missing_records"])
    street_case = client.post("/api/projects", json={"name": "Street", "road": "Davenport Road"}).json()
    assert client.put(f"/api/projects/{street_case['id']}/vendor", json={"name": "X"}).status_code == 422
    assert client.post(f"/api/projects/{street_case['id']}/open-data", json={"since_year": 2015}).status_code == 422


def test_ai_context_frames_the_supplier_case(client, offline_federal):
    from roadwatch import ai
    project = vendor_case(client, province="")
    project = client.post(f"/api/projects/{project['id']}/open-data", json={"since_year": 2015}).json()
    context = ai.build_context(project, {"findings": [], "missing_records": []})
    assert context["case_frame"].startswith("This case follows one supplier") and context["vendor"]["name"] == VENDOR
    street = client.post("/api/projects", json={"name": "Street", "road": "Davenport Road"}).json()
    assert "case_frame" not in ai.build_context(street, {"findings": [], "missing_records": []})
    assert "fed_contracts" in ai.TOOLS[2]["function"]["parameters"]["properties"]["dataset"]["enum"]
