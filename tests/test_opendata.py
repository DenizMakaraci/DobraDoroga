"""Open data, map location and indicators - all offline (connectors are monkeypatched)."""
import json
import os
import pytest
from fastapi.testclient import TestClient

from roadwatch import ai, geocode
from roadwatch.analysis import citation_valid
from roadwatch.opendata import common, mentions_street, street_key, toronto
from roadwatch.server import app


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
def case(client):
    return client.post("/api/projects", json={"name": "Davenport 2024", "road": "Davenport Road",
                                               "contractor": "CRCE Construction Ltd", "completed_on": "2025-10-31"}).json()


PLACE = {"road": "Davenport Road", "display_name": "Davenport Road, Toronto, Ontario, Canada", "city_key": "toronto",
         "latitude": 43.675, "longitude": -79.40, "osm_type": "way", "osm_id": 1,
         "geometry": {"type": "LineString", "coordinates": [[-79.41, 43.674], [-79.39, 43.676]]}}

AWARDED = [
    {"Document Number": "1", "RFx (Solicitation) Type": "RFT", "Successful Supplier": "CRCE CONSTRUCTION LTD",
     "Award": "1,563,741.85", "Award Authority Obtained Date": "2024-08-16", "Division": "ECS", "Wards": "11",
     "Solicitation Document Description": "Major Road Resurfacing on Davenport Road from Bedford Road to Dupont Street"},
    {"Document Number": "2", "RFx (Solicitation) Type": "RFT", "Successful Supplier": "CRCE CONSTRUCTION LTD",
     "Award": "900000", "Award Authority Obtained Date": "2021-05-01", "Division": "ECS", "Wards": "9",
     "Solicitation Document Description": "Road reconstruction, Davenport Rd from Ossington to Dufferin"},
    {"Document Number": "3", "RFx (Solicitation) Type": "RFQ", "Successful Supplier": "Midome Construction Services Ltd",
     "Award": "400000", "Award Authority Obtained Date": "2019-03-03", "Division": "ECS", "Wards": "9",
     "Solicitation Document Description": "Sidewalk repairs on Davenport Road"},
    *[{"Document Number": str(10 + i), "RFx (Solicitation) Type": "RFT", "Successful Supplier": f"CITY PAVER {i} INC", "Award": "500000",
       "Award Authority Obtained Date": "2022-06-01", "Division": "ECS", "Wards": "", "Solicitation Document Description": f"Road resurfacing on Elsewhere Avenue section {i}"}
      for i in range(12)],  # citywide baseline: many other winners elsewhere, so CRCE's street share is unusual
    {"Document Number": "4", "RFx (Solicitation) Type": "RFP", "Successful Supplier": "CONSULTCO",
     "Award": "50000", "Award Authority Obtained Date": "2023-01-01", "Division": "Parks", "Wards": "",
     "Solicitation Document Description": "Davenport Avenue park design"},  # different street type: excluded
]
NONCOMP = [{"Workspace Number": "77", "Reason": "Emergency", "Contract Date": "2025-02-02",
            "Supplier Name": "CRCE Construction Ltd", "Contract Amount": "120000", "Division": "Transportation"}]
PROGRAM = [{"PROJECT": "Local Road Rehabilitation", "LOCATION": "DAVENPORT RD&lt;br&gt; From: BEDFORD RD&lt;br&gt; To: DUPONT ST",
            "DETAILS": "Resurfacing", "DURATION/ Construction Timeline": "Q2 2026 - Q4 2026", "STATUS": "Planned",
            "START_YEAR": "2026", "OWNER": "Transportation Services", "CONTR_NUMBER": "EXTERNAL", "LAST_UPDATED_DATE": "2025-10-01"}]
RESTRICTIONS = {"Closure": [
    {"id": "R1", "road": "Davenport Rd", "name": "Davenport Rd at Bay St", "type": "CONSTRUCTION", "workEventType": "Utility",
     "permitType": "Utility", "contractor": "Entera Utility Contractors", "source": "City", "startTime": "1760000000000",
     "endTime": "1770000000000", "maxImpact": "Low", "description": "Gas main", "latitude": "43.67", "longitude": "-79.39"},
    {"id": "R2", "road": "Queen St W", "name": "Queen St W at Bathurst", "type": "CONSTRUCTION", "contractor": "X",
     "startTime": "1760000000000", "endTime": "1770000000000"},
]}


def fake_311(street, since_year):
    rows = [{"created": f"{year}-05-0{i + 1}", "type": "Road - Pot hole", "status": "Completed",
             "intersection": "Davenport Rd & Bay St", "ward": "11", "division": "Transportation"}
            for year, n in (("2024", 5), ("2025", 12)) for i in range(n)]
    result = common.dataset_result(key="requests_311", title=f"311 - {street}", source_url=toronto.PAGES["requests_311"],
                                   description="test", records=rows, since=since_year,
                                   fields=[("Created", "created"), ("Type", "type"), ("Intersection", "intersection")])
    result["summary"] = [{"year": "2024", "type": "Road - Pot hole", "count": 5},
                         {"year": "2025", "type": "Road - Pot hole", "count": 12}]
    result["total"] = len(rows)
    result["pages"][0]["text"] = result["pages"][0]["text"].replace(
        "\n\nRecord 1", "\n\nCounts by year and type\n2024 | Road - Pot hole: 5\n2025 | Road - Pot hole: 12\n\nRecord 1", 1)
    return result


LOBBY_XML = """<?xml version="1.0"?><ROWSET><ROW><SMXML><SM><SMNumber>SM28346</SMNumber><Status>Active</Status><Type>In-house</Type>
<SubjectMatter>Procurement</SubjectMatter><Particulars>Services offered by Midome Construction Services Ltd</Particulars>
<InitialApprovalDate>2024-01-10</InitialApprovalDate><EffectiveDate>2024-01-10</EffectiveDate>
<Registrant><FirstName>Jane</FirstName><LastName>Doe</LastName></Registrant>
<Firms><Firm><Name>Midome Construction Services Ltd.</Name></Firm></Firms>
<Communications><Communication><POH_Name>Some Councillor</POH_Name><POH_Office>Ward 9</POH_Office><CommunicationDate>2024-05-02</CommunicationDate></Communication></Communications>
</SM></SMXML></ROW><ROW><SMXML><SM><SMNumber>SM1</SMNumber><Status>Closed</Status><Type>Consultant</Type><SubjectMatter>Planning</SubjectMatter>
<Particulars>Condo at 100 Davenport Road</Particulars><Firms><Firm><Name>Dev Co</Name></Firm></Firms><Communications/></SM></SMXML></ROW></ROWSET>"""


def fake_lobby_zip():
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lobbyactivity-active.xml", LOBBY_XML)
    return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))


def fake_daily_rows(station_id, year):
    rows = []
    for month, day_count in ((1, 31), (2, 28), (3, 31), (11, 30), (12, 31)):
        for d in range(1, day_count + 1):
            cold = (d % 2 == 0) if year == 2025 else (d % 4 == 0)
            rows.append({"Date/Time": f"{year}-{month:02d}-{d:02d}", "Max Temp (°C)": "2.0" if cold else "-3.0", "Min Temp (°C)": "-4.0",
                         "Total Precip (mm)": "1.0", "Total Snow (cm)": "0", "Total Rain (mm)": "0"})
    return rows


@pytest.fixture
def offline_climate(monkeypatch):
    from roadwatch.opendata import climate
    monkeypatch.setattr(climate, "daily_rows", fake_daily_rows)


@pytest.fixture
def offline_toronto(monkeypatch, offline_climate):
    tables = {"awarded": AWARDED, "non_competitive": NONCOMP, "road_program": PROGRAM}
    monkeypatch.setattr(toronto, "_csv", lambda name, resource: tables[name])
    monkeypatch.setattr(toronto, "fetch_cached", lambda *a, **k: b'{"Closure": []}')
    monkeypatch.setattr(toronto, "load_json", lambda raw: RESTRICTIONS)
    monkeypatch.setattr(toronto, "requests_311", fake_311)
    monkeypatch.setattr(toronto, "_lobby_zip", fake_lobby_zip)
    monkeypatch.setattr(toronto, "_sidewalk_rows", lambda: [])

    def fake_datastore(name, resource, params, ttl=0):
        if name.startswith("permits-"):
            return [{"PERMIT_NUM": "24 100", "PERMIT_TYPE": "New Building", "WORK": "New", "STREET_NUM": "245", "STREET_NAME": "DAVENPORT", "STREET_TYPE": "RD",
                     "ISSUED_DATE": "2025-03-01", "STATUS": "Issued", "EST_CONST_COST": "12000000", "DESCRIPTION": "10 storey condo", "BUILDER_NAME": "Dev Co"},
                    {"PERMIT_NUM": "24 101", "PERMIT_TYPE": "Plumbing", "STREET_NUM": "1", "STREET_NAME": "DAVENPORT", "STREET_TYPE": "RD", "ISSUED_DATE": "2025-03-01", "EST_CONST_COST": "500"}]
        if name.startswith("devapps-"):
            return [{"APPLICATION#": "25 1 SA", "APPLICATION_TYPE": "SA", "STREET_NUM": "245", "STREET_NAME": "DAVENPORT", "STREET_TYPE": "RD",
                     "DATE_SUBMITTED": "2024-06-01T00:00:00", "STATUS": "Under Review", "DESCRIPTION": "Site plan", "APPLICATION_URL": "http://app.toronto.ca/AIC/x"}]
        if name.startswith("traffic-"):
            return [{"location_name": "Davenport Rd / Bedford Rd", "latest_count_date": "2025-05-05", "count_duration": "8", "total_vehicle": "12000",
                     "total_heavy_pct": "0.041", "total_pedestrian": "800", "total_bike": "90", "pm_peak_vehicle": "1100", "latitude": "43.67", "longitude": "-79.40"}]
        return []
    monkeypatch.setattr(toronto, "_datastore", fake_datastore)
    tables["capital_pipeline"] = [{"Name and Construction Contract Number": "Davenport Road Resurfacing 25ECS-RD-01", "Scope of Work: Detailed Description": "Resurfacing",
                                   "Type of Work": "Roads", "Delivery Division": "ECS", "Target Award Year": "2027", "Sourcing Type": "RFT",
                                   "Estimated Range": "$5M - $10M", "Estimated Contract Term (Months)": "12"}]
    tables["solicitations"] = [{"Document Number": "9", "RFx (Solicitation) Type": "RFT", "Issue Date": "2026-10-01", "Submission Deadline": "2026-11-01",
                                "Division": "ECS", "High Level Category": "Construction", "Solicitation Document Description": "Watermain on Davenport Road"}]


def test_street_matching_is_type_aware_and_word_bounded():
    assert street_key("Davenport Road") == street_key("DAVENPORT RD") == ("davenport", "rd")
    assert street_key("Lake Shore Blvd E") == ("lake shore", "blvd")
    assert street_key("Queen St W") == ("queen", "st")
    assert mentions_street("Resurfacing on Davenport Rd from X", "Davenport Road")
    assert mentions_street("DAVENPORT RD & OSSINGTON AVE", "Davenport Road")
    assert mentions_street("works on Davenport between A and B", "Davenport Road")
    assert not mentions_street("Davenport Avenue park", "Davenport Road")
    assert not mentions_street("Mount Davenporthill Rd", "Davenport Road")
    assert not mentions_street("", "Davenport Road") and not mentions_street("Davenport", "")


def test_toronto_profile_filters_renders_and_matches_suppliers(offline_toronto):
    results, errors = toronto.profile(["Davenport Road"], 2019)
    assert errors == []
    by_key = {r["key"]: r for r in results}
    assert [r["document_number"] for r in by_key["awarded"]["records"]] == ["1", "2", "3"]
    assert by_key["awarded"]["baselines"]["city_road_awards"] == 15 and by_key["awarded"]["baselines"]["city_supplier_shares"]["CRCE CONSTRUCTION LTD"]["share_pct"] == 13.3
    assert by_key["awarded"]["records"][0]["amount"] == 1563741.85 and by_key["awarded"]["records"][0]["road_work"]
    assert by_key["awarded"]["records"][0]["street"] == "Davenport Road"
    assert "Successful supplier: CRCE CONSTRUCTION LTD" in by_key["awarded"]["pages"][0]["text"]
    assert by_key["road_program"]["records"][0]["location"] == "DAVENPORT RD From: BEDFORD RD To: DUPONT ST"
    assert [r["id"] for r in by_key["restrictions"]["records"]] == ["R1"]
    assert by_key["restrictions"]["records"][0]["start"] == "2025-10-09"
    assert by_key["non_competitive"]["records"][0]["supplier"] == "CRCE CONSTRUCTION LTD"  # normalised name
    assert toronto.canonical_supplier("Midome Construction Services Ltd.") == toronto.canonical_supplier("MIDOME  CONSTRUCTION SERVICES LTD")
    assert toronto.canonical_supplier("Sanscon Construction Limited") == "SANSCON CONSTRUCTION LTD"
    assert toronto.canonical_supplier("IBI Group Inc.") == toronto.canonical_supplier("IBI GROUP INCORPORATED")


def test_streets_can_be_added_and_removed_with_city_and_duplicate_rules(client, case):
    bad = client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "city_key": "ottawa"})
    assert bad.status_code == 422 and "Toronto" in bad.json()["detail"] and "Montréal" in bad.json()["detail"]
    assert client.post(f"/api/projects/{case['id']}/open-data", json={}).status_code == 422
    project = client.post(f"/api/projects/{case['id']}/locations", json=PLACE).json()
    assert [l["road"] for l in project["locations"]] == ["Davenport Road"]
    assert client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "road": "davenport road"}).status_code == 409
    project = client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "road": "Dupont Street"}).json()
    assert [l["road"] for l in project["locations"]] == ["Davenport Road", "Dupont Street"]
    assert client.delete(f"/api/projects/{case['id']}/locations/5").status_code == 404
    project = client.delete(f"/api/projects/{case['id']}/locations/0").json()
    assert [l["road"] for l in project["locations"]] == ["Dupont Street"]


def test_pull_open_data_creates_citable_documents_and_indicators(client, case, offline_toronto):
    client.post(f"/api/projects/{case['id']}/locations", json=PLACE)
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    assert {d["kind"] for d in project["evidence"]} == {"open_data"}
    assert {d["key"] for d in project["open_data"]} == {"awarded", "road_program", "restrictions", "requests_311", "non_competitive", "lobbying",
                                                          "planning", "solicitations", "development", "traffic", "integrity", "climate"}
    development = next(d for d in project["open_data"] if d["key"] == "development")
    assert [r["kind"] for r in development["records"]] == ["building_permit", "development_application"]  # plumbing permit filtered out
    assert next(d for d in project["open_data"] if d["key"] == "traffic")["records"][0]["heavy_vehicle_pct"] == 4.1
    climate = next(d for d in project["open_data"] if d["key"] == "climate")
    assert climate["records"] and all(r["freeze_thaw_days"] >= 0 for r in climate["records"])
    assert next(d for d in project["open_data"] if d["key"] == "planning")["records"][0]["estimate_range"] == "$5M - $10M"
    assert next(d for d in project["open_data"] if d["key"] == "solicitations")["count"] == 1
    lobby = next(d for d in project["open_data"] if d["key"] == "lobbying")
    assert [r["sm_number"] for r in lobby["records"]] == ["SM28346"]  # the condo planning entry is not road-related
    assert lobby["records"][0]["supplier"] == "MIDOME CONSTRUCTION SERVICES LTD" and "Some Councillor" in lobby["records"][0]["officials_lobbied"]
    assert all((d["source_url"] or "").startswith("https://") for d in project["evidence"])
    assert {d["source_url"].split("/")[2] for d in project["evidence"]} >= {"open.toronto.ca", "www.torontoauditor.ca", "climate.weather.gc.ca"}

    report = client.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    titles = {f["title"] for f in report["findings"]}
    assert "One supplier won a large share of road work on the case's street(s)" in titles
    assert "Road work scheduled within three years of earlier work on the same street" in titles
    assert "A contractor on the case's street(s) also received non-competitive contracts" in titles
    assert "Road-damage complaints roughly doubled year over year" in titles
    spike = next(f for f in report["findings"] if f["title"].startswith("Road-damage complaints"))
    assert "freeze-thaw days" in spike["alternative_explanation"] and "permit(s)" in spike["alternative_explanation"]
    assert "Construction permits name contractors other than the award winner" in titles
    assert "A supplier on the case's street(s) is a registered lobbyist of the City" in titles
    lobbying = next(f for f in report["findings"] if "lobbyist" in f["title"])
    assert "within 12 months before" in lobbying["explanation"]  # 2024-05-02 comms, 2024-08-16 award
    assert all(f["origin"] == "open_data" for f in report["findings"])
    assert all(citation_valid(c, project) for f in report["findings"] for c in f["citations"])
    assert all(f["alternative_explanation"] and f["next_step"] for f in report["findings"])
    assert "corruption" not in " ".join(f["explanation"] for f in report["findings"]).lower()

    # A second pull replaces the open-data documents instead of piling up copies.
    again = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    assert len(again["evidence"]) == 12 and again["revision"] == project["revision"] + 1
    # Deleting a dataset document removes its structured records too.
    doc = again["evidence"][0]
    after = client.delete(f"/api/projects/{case['id']}/evidence/{doc['id']}").json()
    assert all(d["evidence_id"] != doc["id"] for d in after["open_data"])


def test_ai_tool_queries_stored_open_data_only(client, case, offline_toronto):
    client.post(f"/api/projects/{case['id']}/locations", json=PLACE)
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    result = ai.run_tool("query_open_data", {"dataset": "awarded", "text": "crce"}, project)
    assert result["total_records"] == 3 and [h["document_number"] for h in result["hits"]] == ["1", "2"]
    assert result["hits"][0]["page"] == 1
    assert citation_valid({"evidence_id": result["evidence_id"], "page": 1,
                           "quote": "Successful supplier: CRCE CONSTRUCTION LTD"}, project)
    assert "error" in ai.run_tool("query_open_data", {"dataset": "awarded"}, {"open_data": []})
    assert ai.run_tool("query_open_data", {"dataset": "awarded", "year": 2019}, project)["hits"][0]["document_number"] == "3"


def test_geocode_proxy_filters_to_streets_and_tags_supported_city(client, monkeypatch):
    items = [
        {"category": "highway", "type": "secondary", "lat": "43.67", "lon": "-79.40", "osm_type": "way", "osm_id": 5,
         "display_name": "Davenport Road, Toronto, Ontario, Canada", "address": {"road": "Davenport Road", "city": "Toronto"},
         "geojson": {"type": "LineString", "coordinates": [[-79.4, 43.67]]}},
        {"category": "place", "type": "neighbourhood", "lat": "43.6", "lon": "-79.4", "osm_type": "node", "osm_id": 6,
         "display_name": "Davenport, Toronto", "address": {"city": "Toronto"}},
        {"category": "highway", "type": "residential", "lat": "45.5", "lon": "-73.5", "osm_type": "way", "osm_id": 7,
         "display_name": "Rue Davenport, Montréal", "address": {"road": "Rue Davenport", "city": "Montréal"}},
    ]

    async def fake_get(path, params):
        return items if path == "/search" else items[0]
    monkeypatch.setattr(geocode, "_get", fake_get)
    found = client.get("/api/geocode", params={"q": "Davenport"}).json()
    assert [p["road"] for p in found] == ["Davenport Road", "Rue Davenport"]
    assert found[0]["city_key"] == "toronto" and found[1]["city_key"] == "montreal"
    assert found[0]["geometry"]["type"] == "LineString"
    assert client.get("/api/geocode", params={"q": "x"}).status_code == 422
    assert client.get("/api/geocode/reverse", params={"lat": 43.67, "lon": -79.4}).json()["road"] == "Davenport Road"


def test_csp_allows_osm_tiles_only(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "https://tile.openstreetmap.org" in csp and "connect-src 'self'" in csp


def test_new_case_can_carry_several_streets_from_the_map(client):
    two = [PLACE, {**PLACE, "road": "Dupont Street"}]
    created = client.post("/api/projects", json={"name": "Davenport", "road": "Davenport Road & Dupont Street, Toronto", "locations": two})
    assert created.status_code == 201 and [l["road"] for l in created.json()["locations"]] == ["Davenport Road", "Dupont Street"]
    rejected = client.post("/api/projects", json={"name": "Rue X", "road": "Rue X", "locations": [{**PLACE, "city_key": "ottawa"}]})
    assert rejected.status_code == 422
    plain = client.post("/api/projects", json={"name": "No map", "road": "Somewhere"})
    assert plain.status_code == 201 and plain.json()["locations"] == []


def test_merged_pull_tags_records_by_street_and_flags_stale_streets(client, case, offline_toronto):
    client.post(f"/api/projects/{case['id']}/locations", json=PLACE)
    client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "road": "Dupont Street"})
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    awarded = next(d for d in project["open_data"] if d["key"] == "awarded")
    assert awarded["streets"] == ["Davenport Road", "Dupont Street"]
    assert {r["street"] for r in awarded["records"]} == {"Davenport Road"}  # fixture data only names Davenport
    assert "Davenport Road, Dupont Street" in awarded["title"]
    client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "road": "Bloor Street"})
    report = client.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    assert any("added after the last open-data pull" in g for g in report["missing_records"])
    assert any(f["title"].startswith("One supplier") for f in report["findings"])


def test_multiple_providers_from_env_and_safe_failure_messages(client, monkeypatch):
    monkeypatch.setenv("ROADWATCH_API_KEY", "k1"); monkeypatch.setenv("ROADWATCH_MODEL", "gemini-x")
    monkeypatch.setenv("ROADWATCH_NV_MODEL", "nvidia/ultra"); monkeypatch.setenv("ROADWATCH_NV_LABEL", "NVIDIA Ultra")
    monkeypatch.setenv("ROADWATCH_NV_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("ROADWATCH_NOKEY_MODEL", "orphan"); monkeypatch.delenv("ROADWATCH_NOKEY_API_KEY", raising=False)
    found = ai.providers()
    assert set(found) == {"default", "nv", "nokey"} and found["nokey"]["api_key"] == "k1"
    monkeypatch.delenv("ROADWATCH_API_KEY")
    assert set(ai.providers()) == set()  # without any key nothing is offered
    monkeypatch.setenv("ROADWATCH_API_KEY", "k1")
    assert found["nv"]["api_key"] == "k1" and found["nv"]["label"] == "NVIDIA Ultra"
    cfg = client.get("/api/config").json()
    assert cfg["enabled"] and {p["id"] for p in cfg["providers"]} >= {"default", "nv"}
    assert "api_key" not in json.dumps(cfg)
    assert ai.provider("nv")["model"] == "nvidia/ultra" and ai.provider("missing")["id"] == "default"

    class RateLimitError(Exception):
        pass
    assert "quota" in ai.failure_reason(RateLimitError())
    assert "key" in ai.failure_reason(type("AuthenticationError", (Exception,), {})())
    assert "did not complete" in ai.failure_reason(RuntimeError("secret sk-123"))
    assert "secret" not in ai.failure_reason(RuntimeError("secret sk-123"))


def test_parse_report_tolerates_fences_and_rejects_schema_echo():
    good = {"findings": [], "missing_records": ["x"]}
    assert ai.parse_report(json.dumps(good)) == good
    assert ai.parse_report("```json\n" + json.dumps(good) + "\n```") == good
    assert ai.parse_report("Here is the report:\n" + json.dumps(good) + "\nThanks.") == good
    with pytest.raises(ValueError, match="schema"):
        ai.parse_report(json.dumps({"$defs": {}, "properties": {}}))
    with pytest.raises(ValueError, match="invalid JSON"):
        ai.parse_report('{"findings": [{"title": "x", ')
    with pytest.raises(ValueError, match="no JSON"):
        ai.parse_report("I could not find anything.")


def test_cities_endpoint_and_city_bounded_geocode(client, monkeypatch):
    found = client.get("/api/cities").json()
    assert [c["key"] for c in found] == ["toronto", "montreal"] and found[1]["language"] == "fr"
    seen = {}

    async def fake_get(path, params):
        seen.update(params)
        return [{"category": "highway", "type": "residential", "lat": "45.52", "lon": "-73.58", "osm_type": "way", "osm_id": 9,
                 "display_name": "Rue Saint-Denis, Le Plateau-Mont-Royal, Montréal, Québec, Canada",
                 "address": {"road": "Rue Saint-Denis", "city": "Montréal"}, "geojson": {"type": "LineString", "coordinates": []}}]
    monkeypatch.setattr(geocode, "_get", fake_get)
    out = client.get("/api/geocode", params={"q": "Saint-Denis", "city": "montreal"}).json()
    assert seen["bounded"] == 1 and seen["q"] == "Saint-Denis, Montréal" and "viewbox" in seen
    assert out[0]["city_key"] == "montreal"


MTL_PLACE = {"road": "Rue Saint-Denis", "display_name": "Rue Saint-Denis, Montréal, Québec, Canada", "city_key": "montreal",
             "latitude": 45.52, "longitude": -73.58, "osm_type": "way", "osm_id": 9, "geometry": None}


def test_montreal_connector_offline_and_city_isolation(client, monkeypatch, offline_climate):
    from roadwatch.opendata import montreal
    queries = []

    def fake_sql(name, query, ttl=0):
        queries.append(name)
        if name.startswith("contracts-central"):
            return [{"supplier": "Groupe ABF Inc.", "number": "26-002-VG", "date": "2026-06-15", "amount": "108270",
                     "description": "Contrat pour la Réfection de trottoirs - Rue Beaubien entre Saint-Denis et Boyer", "service": "Voirie"},
                    {"supplier": "Béton Brunet Ltée", "number": "1", "date": "2016-01-01", "amount": "5",
                     "description": "Intervention rue Saint-Denis", "service": "Voirie"}]  # before since_year: dropped
        if name.startswith("contracts-executive"):
            return [{"supplier": "7574479 CANADA INC.", "number": "2", "date": "9/12/2025", "amount": "311150",
                     "description": "Travaux de plantation d'arbres rue Saint-Denis", "service": "Parcs", "decision": "CE25 1"}]
        if name.startswith("contracts-boroughs"):
            return []
        if name == "impacts":
            return [{"id_request": "r1", "name": "rue Saint-Denis", "fromname": "rue Roy", "toname": "avenue Duluth", "streetimpacttype": "Voie barrée"}]
        if name == "permits":
            return [{"id": "r1", "permit_permit_id": "OCC-1", "contractnumber": "1652355", "boroughid": "Le Plateau", "permitcategory": "Travaux",
                     "currentstatus": "Permis émis", "duration_start_date": "2026-10-04T20:00:00Z", "duration_end_date": "2026-11-13T18:59:59Z",
                     "reason_category": "Aqueduc", "occupancy_name": "Saint-Denis entre Roy et Duluth",
                     "submittercategory": "Entrepreneur sous contrat avec la Ville", "organizationname": "Énergère Inc."}]
        raise AssertionError(name)

    def fake_search(name, resource, params, ttl=0):
        if name.startswith("permits-"):
            return [{"no_demande": "1", "date_emission": "2025-04-04", "emplacement": "1720 rue Saint-Denis", "arrondissement": "Plateau",
                     "description_type_demande": "Démolition", "description_type_batiment": "Commercial", "nature_travaux": "Démolition", "nb_logements": "0"}]
        if name.startswith("traffic-"):
            return [{"Nom_Intersection": "Rachel / Saint-Denis", "Date": "2026-06-11", "Description_Code_Banque": "Camions porteurs", "NBT": "50", "SBT": "50"},
                    {"Nom_Intersection": "Rachel / Saint-Denis", "Date": "2026-06-11", "Description_Code_Banque": "Autos", "NBT": "1000", "SBT": "900"}]
        if "-RUE-" not in name or "311_2022" not in name:
            return []
        return ([{"DDS_DATE_CREATION": f"2024-0{i % 9 + 1}-01T10:00:00", "ACTI_NOM": "Nid-de-poule", "DERNIER_STATUT": "Terminée",
                  "RUE": "rue Saint-Denis", "RUE_INTERSECTION1": None, "RUE_INTERSECTION2": None, "ARRONDISSEMENT": "Le Plateau"} for i in range(12)]
                + [{"DDS_DATE_CREATION": f"2025-{i % 12 + 1:02d}-{i % 28 + 1:02d}T10:00:00", "ACTI_NOM": "Nid-de-poule", "DERNIER_STATUT": "Terminée",
                    "RUE": "rue Saint-Denis", "RUE_INTERSECTION1": None, "RUE_INTERSECTION2": None, "ARRONDISSEMENT": "Le Plateau"} for i in range(30)]
                + [{"DDS_DATE_CREATION": "2025-01-01T10:00:00", "ACTI_NOM": "Nid-de-poule", "DERNIER_STATUT": "Terminée",
                    "RUE": "rue Saint-Denis", "RUE_INTERSECTION1": None, "RUE_INTERSECTION2": None, "ARRONDISSEMENT": "Le Plateau"}])  # duplicate row: deduped
    monkeypatch.setattr(montreal, "_sql", fake_sql)
    monkeypatch.setattr(montreal, "_search", fake_search)
    seao = [{"ocid": "ocds-1", "date": "2025-08-28", "buyer": "Ville de Montréal", "title": "Réfection de trottoirs rue Saint-Denis",
             "category": "works", "method": "open", "bidders": 1, "tenderers": ["Groupe ABF Inc."], "bids": [108270.0],
             "awards": [{"amount": 108270.0, "suppliers": ["Groupe ABF Inc."], "date": "2025-08-28"}], "url": "https://seao.gouv.qc.ca/x"},
            {"ocid": "ocds-2", "date": "2025-09-10", "buyer": "Ville de Montréal", "title": "Pavage rue Saint-Denis", "category": "works",
             "method": "open", "bidders": 3, "tenderers": ["A", "B", "C"], "bids": [100000.0, 101000.0, 150000.0],
             "awards": [{"amount": 101000.0, "suppliers": ["B"], "date": "2025-09-10"}], "url": None}]
    monkeypatch.setattr(montreal, "_seao_months", lambda: [("202509", "u"), ("202508", "u")])
    monkeypatch.setattr(montreal, "_seao_month", lambda yyyymm, url: seao if yyyymm == "202509" else [])
    monkeypatch.setattr(montreal, "_estimates_resource", lambda: "est")
    estimates = [{"Description": "Réfection rue Saint-Denis", "Nature de travaux": "Pavage", "Date d'ouverture": "2024-05-01T00:00:00",
                  "Arrondissement": "Plateau", "Catégorie de travaux": "Pavage", "Estimé DEC ou EXT 'TOTAL (B)": "1000000",
                  "Adjudicataire": "Groupe ABF Inc.", "Valeur de la soumission retenue\n(C)": "1300000", "Nbs de soum.": "2", "Plus haute soumission": "1400000"}]
    original_search = montreal._search
    monkeypatch.setattr(montreal, "_search", lambda name, resource, params, ttl=0: estimates if name == "estimates" else fake_search(name, resource, params, ttl))
    plan_json = json.dumps({"features": [{"properties": {"RUE": "Saint-Denis", "DE": "Roy", "A": "Duluth", "NOM_ARROND": "Plateau",
                                                          "CH_PCI_MES": "41", "CH_TRAV": "R\u00e9hab. majeure", "TOTAL_COUT": "200000", "DATE_SIAD": "2019"}}]}).encode()
    big_html = '<html><a href="https://www.bigmtl.ca/publications/x/">Rapport public - enqu\u00eate sur Groupe ABF inc. et la rue Saint-Denis</a></html>'.encode()
    rena_json = json.dumps({"rena": {"inscription": [{"NEQ": "111", "Nom_entreprise": "Groupe ABF inc.", "Adresse": "x", "Nom_personne_liee": "J. Doe",
                                                      "Nom_de_lactionnaire_majoritaire": "", "Inscription_registre": "2025-01-01", "Fin_inadmissibilite": "2030-01-01",
                                                      "Infraction": "Loi : Competition Act", "Decision_AMP": "Déclaration", "Informations_complementaires": "", "Categorie": "Autres"}]}}).encode()
    monkeypatch.setattr(montreal, "fetch_cached", lambda name, url, **k: plan_json if "plan" in name else (rena_json if "rena" in name else big_html))
    monkeypatch.setattr(montreal, "_resource_ids", lambda package_id, fmt="CSV", datastore=True: [("3 dernières", "rid-recent")])

    results, errors = montreal.profile(["Rue Saint-Denis"], 2018)
    assert errors == [] and {r["key"] for r in results} == {"awarded", "restrictions", "requests_311", "bids", "planning", "integrity",
                                                              "development", "traffic", "climate", "debarment"}
    assert next(r for r in results if r["key"] == "development")["records"][0]["type"] == "Démolition"
    traffic = next(r for r in results if r["key"] == "traffic")["records"][0]
    assert traffic["vehicles"] == 2000 and traffic["trucks"] == 100 and traffic["heavy_vehicle_pct"] == 5.0
    assert next(r for r in results if r["key"] == "debarment")["records"][0]["supplier"] == "GROUPE ABF INC"
    planning = next(r for r in results if r["key"] == "planning")
    assert {r["kind"] for r in planning["records"]} == {"estimate", "plan"}
    assert next(r for r in planning["records"] if r["kind"] == "estimate")["gap_pct"] == 30.0
    integrity = next(r for r in results if r["key"] == "integrity")
    assert integrity["records"][0]["supplier"] == "GROUPE ABF INC"
    bids = next(r for r in results if r["key"] == "bids")
    assert bids["records"][0]["spread_pct"] == 1.0 and bids["records"][0]["winner_is_lowest"] is False
    assert bids["records"][1]["bidders"] == 1 and "OCID: ocds-1" in bids["pages"][0]["text"]
    awarded = next(r for r in results if r["key"] == "awarded")
    assert [r["supplier"] for r in awarded["records"]] == ["GROUPE ABF INC", "7574479 CANADA INC"]
    assert awarded["records"][1]["award_date"] == "2025-09-12"  # month/day/year normalised
    assert awarded["records"][0]["road_work"] and awarded["records"][0]["street"] == "Rue Saint-Denis"
    permits = next(r for r in results if r["key"] == "restrictions")
    assert permits["records"][0]["contractor"] == "Énergère Inc." and permits["records"][0]["type"] == "CONSTRUCTION"
    r311 = next(r for r in results if r["key"] == "requests_311")
    assert r311["total"] == 39 and "2025 | Nid-de-poule: 30" in r311["pages"][0]["text"]  # 2024 fixture rows collapse to 9 unique

    case = client.post("/api/projects", json={"name": "St-Denis", "road": "Rue Saint-Denis, Montréal", "locations": [MTL_PLACE]}).json()
    mixed = client.post(f"/api/projects/{case['id']}/locations", json=PLACE)  # a Toronto street
    assert mixed.status_code == 422 and "same city" in mixed.json()["detail"]
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2018}).json()
    assert all(d["city_key"] == "montreal" for d in project["open_data"])
    assert all((d["source_url"] or "").startswith("https://") for d in project["evidence"])
    assert {d["source_url"].split("/")[2] for d in project["evidence"]} >= {"donnees.montreal.ca", "www.donneesquebec.ca", "www.bigmtl.ca", "climate.weather.gc.ca"}
    report = client.post(f"/api/projects/{case['id']}/investigate", json={"use_ai": False}).json()["reports"][-1]
    titles = {f["title"] for f in report["findings"]}
    assert any(t.startswith("Road-damage complaints") for t in titles)
    assert "Road tenders awarded with a single bidder" in titles
    assert any(t.startswith("Bid patterns worth checking") for t in titles)
    assert "Winning bid far from the City's own estimate" in titles
    rena = next(f for f in report["findings"] if "RENA" in f["title"])
    assert "awarded while barred" in rena["explanation"]  # ABF's 2026 award falls inside 2025-2030
    assert all(citation_valid(c, project) for f in report["findings"] for c in f["citations"])


def test_condition_layers_and_viewport_endpoint(client, monkeypatch, offline_toronto):
    from roadwatch import server
    from roadwatch.opendata import geo
    # Transverse Mercator inverse: a known Montréal MTM-8 point lands where it should
    lon, lat = geo.tm_to_lonlat(300000, 5040000, **geo.PROJECTIONS[32188])
    assert abs(lon + 73.56) < 0.02 and abs(lat - 45.50) < 0.02
    # Toronto proxy layer from the (fixture) road program
    monkeypatch.setattr(toronto, "_csv", lambda name, resource: [{**PROGRAM[0], "geometry": json.dumps({"type": "LineString", "coordinates": [[-79.40, 43.675], [-79.39, 43.676]]})}] if name == "road_program" else [])
    layer = toronto.condition_layer()
    assert layer["features"][0]["properties"]["grade"] == 5 and "Reconstruction" in layer["features"][0]["properties"]["label"]
    assert layer["legend"]["classes"][-1][0] == 5
    server._condition_cache.clear()
    inside = client.get("/api/condition", params={"city": "toronto", "bbox": "-79.41,43.67,-79.38,43.68"}).json()
    assert len(inside["features"]) == 1 and inside["legend"]["title"]
    outside = client.get("/api/condition", params={"city": "toronto", "bbox": "-79.30,43.70,-79.28,43.71"}).json()
    assert outside["features"] == []
    assert client.get("/api/condition", params={"city": "toronto", "bbox": "-80,43,-79,44"}).status_code == 422  # too wide
    assert client.get("/api/condition", params={"city": "ottawa", "bbox": "0,0,0.1,0.1"}).status_code == 404
    # per-case toggle
    case = client.post("/api/projects", json={"name": "x", "road": "y", "show_condition": False}).json()
    assert case["show_condition"] is False
    assert client.put(f"/api/projects/{case['id']}/settings", json={"show_condition": True}).json()["show_condition"] is True


def test_contract_limits_flag_records(client, case, offline_toronto):
    client.post(f"/api/projects/{case['id']}/locations", json={**PLACE, "from_street": "Bedford Road", "to_street": "Dupont Street"})
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    awarded = next(d for d in project["open_data"] if d["key"] == "awarded")
    flags = {r["document_number"]: r["within_limits"] for r in awarded["records"]}
    assert flags == {"1": True, "2": False, "3": False}  # only #1 names Bedford/Dupont
    permits = next(d for d in project["open_data"] if d["key"] == "restrictions")
    assert permits["records"][0]["within_limits"] is False  # "at Bay St"
    updated = client.put(f"/api/projects/{case['id']}/locations/0", json={"from_street": "", "to_street": ""}).json()
    assert updated["locations"][0]["from_street"] == ""
    project = client.post(f"/api/projects/{case['id']}/open-data", json={"since_year": 2019}).json()
    assert all(r["within_limits"] is None for d in project["open_data"] for r in d["records"])
