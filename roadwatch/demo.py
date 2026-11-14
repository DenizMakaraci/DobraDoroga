from . import store


def seed():
    project = store.create_project({
        "name": "Cedar Avenue resurfacing", "road": "Fictional Cedar Avenue, Toronto",
        "contractor": "Example Paving Ltd. (fictional)", "contract_number": "DEMO-2025-001",
        "completed_on": "2025-08-31",
    }, demo=True)
    documents = [
        ("Contract and scope", "contract",
         "FICTIONAL DEMO — not a real Toronto contract.\n"
         "Contract DEMO-2025-001. Original award: CAD 1,000,000 before tax. "
         "Scope: roadway resurfacing. Required asphalt thickness: 50 mm. "
         "Target completion: August 31, 2025."),
        ("Approved change order", "amendment",
         "FICTIONAL DEMO — not a real Toronto record.\n"
         "Change order CO-01 authorizes an additional CAD 100,000 before tax "
         "for roadway resurfacing. No thickness change is specified."),
        ("Payment ledger excerpt", "payment",
         "FICTIONAL DEMO — not a real Toronto record.\n"
         "Individual payments, not cumulative totals. All amounts before tax.\n"
         "PAY-001: CAD 600,000 paid for roadway resurfacing.\n"
         "PAY-002: CAD 550,000 paid for roadway resurfacing.\n"
         "This excerpt may not include all adjustments."),
        ("Inspection note", "inspection",
         "FICTIONAL DEMO — not a real Toronto record.\n"
         "August 20, 2025. One asphalt core measured 38 mm at test location C-4. "
         "The contract specifies 50 mm. The inspector requested a repeat test. "
         "The repeat test result and acceptance decision are not included. "
         "A single sample does not establish the condition of the full road."),
    ]
    for title, kind, text in documents:
        project["evidence"].append({
            "id": store.identifier("evidence"), "title": title, "kind": kind,
            "source_url": None, "filename": "", "sha256": None,
            "created_at": store.now(), "pages": [{"number": 1, "text": text}],
        })
    for kind, amount, reference, index, quote in [
        ("award", "1000000.00", "DEMO-2025-001", 0, "Original award: CAD 1,000,000 before tax."),
        ("amendment", "100000.00", "CO-01", 1, "an additional CAD 100,000 before tax"),
        ("payment", "600000.00", "PAY-001", 2, "PAY-001: CAD 600,000 paid for roadway resurfacing."),
        ("payment", "550000.00", "PAY-002", 2, "PAY-002: CAD 550,000 paid for roadway resurfacing."),
    ]:
        project["finances"].append({
            "id": store.identifier("entry"), "kind": kind, "amount": amount,
            "reference": reference, "scope": "Roadway resurfacing", "basis": "before_tax",
            "evidence_id": project["evidence"][index]["id"], "page": 1, "quote": quote,
        })
    project["observations"].append({
        "id": store.identifier("observation"), "observed_on": "2026-03-15",
        "latitude": 43.6532, "longitude": -79.3832, "severity": "severe",
        "description": "Fictional report: several potholes after winter. Location is illustrative, "
                       "not evidence about a real street.",
    })
    project["revision"] = 1
    return store.mutate(project["id"], lambda saved: saved.update(project))
