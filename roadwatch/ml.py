"""Two honest uses of machine learning for an investigation that has no ground truth about corruption.

1. Anomaly scores (unsupervised): robust z-scores of each tender's bidder count, bid spread and estimate gap, and of
   each supplier's win rate, against the baseline of every record pulled for the same city. 'Unusual' is a fact about
   the distribution, not an accusation.
2. Calibration (supervised): a logistic regression trained on the team's own verdicts - findings marked escalated (1)
   versus explained/dismissed (0) across all cases - over the indicator pattern of each finding. It reports how often
   findings with this pattern turned out to matter for this team, and abstains until there are enough labels.
Pure Python; no dependencies."""
import json
import math
import statistics
from collections import Counter, defaultdict

from . import store

MIN_LABELS = 12
FEATURES = ("single_bidder", "tight_spread", "winner_not_lowest", "estimate_gap", "repeat_winner", "rework", "non_competitive",
            "complaint_spike", "other_contractors", "lobbying", "rena", "within_limits", "supported", "plausible", "speculative", "ai")


def _robust_z(value, values, minimum=8):
    clean = [v for v in values if v is not None]
    if value is None or len(clean) < minimum:
        return None
    median = statistics.median(clean)
    mad = statistics.median(abs(v - median) for v in clean) or (statistics.pstdev(clean) / 1.4826 if len(set(clean)) > 1 else 0)
    if not mad:
        return 0.0
    return round((value - median) / (1.4826 * mad), 2)


def anomaly_scores(project):
    """Per-tender and per-supplier anomaly scores from the case's open data (baseline = all pulled rows)."""
    bids = next((d for d in project.get("open_data", []) if d["key"] == "bids"), None)
    planning = next((d for d in project.get("open_data", []) if d["key"] == "planning"), None)
    rows = [r for r in (bids or {"records": []})["records"] if r.get("road_work")]
    bidders = [r.get("bidders") for r in rows]
    spreads = [r.get("spread_pct") for r in rows]
    gaps = [r.get("gap_pct") for r in (planning or {"records": []})["records"] if r.get("kind") == "estimate"]
    tenders = []
    for r in rows:
        z_bidders, z_spread = _robust_z(r.get("bidders"), bidders), _robust_z(r.get("spread_pct"), spreads)
        flags = []
        if z_bidders is not None and z_bidders <= -1.5:
            flags.append("far fewer bidders than typical")
        if z_spread is not None and z_spread <= -1.5 and (r.get("spread_pct") or 0) <= 2:
            flags.append("unusually tight low bids")
        if r.get("winner_is_lowest") is False:
            flags.append("award not to lowest bid")
        if flags:
            tenders.append({"ocid": r.get("ocid"), "date": r.get("date"), "title": (r.get("title") or "")[:120], "supplier": r.get("supplier"),
                            "bidders": r.get("bidders"), "spread_pct": r.get("spread_pct"), "z_bidders": z_bidders, "z_spread": z_spread, "flags": flags})
    wins, entries = Counter(), Counter()
    for r in rows:
        for name in (r.get("bidder_names") or "").split("; "):
            if name:
                entries[name.strip().upper()] += 1
        if r.get("supplier") and r.get("award_amount"):
            wins[r["supplier"]] += 1
    rates = {s: wins.get(s, 0) / max(entries[s], wins.get(s, 0)) for s in entries if entries[s] >= 3}
    rate_values = list(rates.values())
    suppliers = []
    for s, rate in rates.items():
        z = _robust_z(rate, rate_values, minimum=4)
        if z is not None and z >= 1.5:
            suppliers.append({"supplier": s, "tenders_entered": entries[s], "won": wins.get(s, 0), "win_rate": round(rate, 2), "z_win_rate": z,
                              "flag": "wins far more often than peers on these streets"})
    gap_outliers = [{"date": r.get("date"), "supplier": r.get("supplier"), "gap_pct": r.get("gap_pct"), "z": _robust_z(r.get("gap_pct"), gaps)}
                    for r in (planning or {"records": []})["records"] if r.get("kind") == "estimate" and _robust_z(r.get("gap_pct"), gaps) not in (None,) and abs(_robust_z(r.get("gap_pct"), gaps)) >= 1.5]
    return {"baseline": {"tenders": len(rows), "median_bidders": statistics.median([b for b in bidders if b]) if any(bidders) else None,
                         "median_spread_pct": statistics.median([s for s in spreads if s is not None]) if any(s is not None for s in spreads) else None},
            "unusual_tenders": sorted(tenders, key=lambda t: (t["z_bidders"] or 0) + (t["z_spread"] or 0))[:12],
            "unusual_suppliers": sorted(suppliers, key=lambda s: -s["z_win_rate"])[:8], "estimate_gap_outliers": gap_outliers[:8],
            "note": "Robust z-scores against this case's own pulled records; |z| >= 1.5 is flagged. Unusual is a statistical fact, not misconduct."}


def features(finding):
    title, text = (finding.get("title") or "").lower(), json.dumps(finding, default=str).lower()
    x = {
        "single_bidder": "single bidder" in title, "tight_spread": "near-identical" in title or "bid patterns" in title,
        "winner_not_lowest": "not to the lowest" in text, "estimate_gap": "estimate" in title, "repeat_winner": "one supplier won" in title,
        "rework": "within three years" in title, "non_competitive": "non-competitive" in title, "complaint_spike": "complaints" in title,
        "other_contractors": "other than the award winner" in title, "lobbying": "lobbyist" in title, "rena": "rena" in title,
        "within_limits": "within the stated contract limits" in text and " 0 of them" not in text,
        "supported": finding.get("confidence") == "supported", "plausible": finding.get("confidence") == "plausible",
        "speculative": finding.get("confidence") == "speculative", "ai": finding.get("origin") == "ai",
    }
    return [1.0 if x[f] else 0.0 for f in FEATURES]


def training_set():
    """(features, label) from every finding the team has judged, across all cases."""
    data = []
    for project in store.list_projects():
        statuses = store.finding_statuses(project["id"])
        findings = {f.get("key"): f for report in project.get("reports", []) for f in report.get("findings", []) if f.get("key")}
        for key, status in statuses.items():
            finding = findings.get(key)
            if not finding or status.get("status") not in ("escalated", "explained", "dismissed"):
                continue
            data.append((features(finding), 1.0 if status["status"] == "escalated" else 0.0))
    return data


def train(data, epochs=400, lr=0.1, l2=0.01):
    """Logistic regression by gradient descent (tiny data, so this is instant)."""
    if not data:
        return None
    n = len(FEATURES)
    w, b = [0.0] * n, 0.0
    for _ in range(epochs):
        gw, gb = [0.0] * n, 0.0
        for x, y in data:
            p = 1 / (1 + math.exp(-min(30, max(-30, sum(wi * xi for wi, xi in zip(w, x)) + b))))
            err = p - y
            gb += err
            for i in range(n):
                gw[i] += err * x[i]
        m = len(data)
        w = [wi - lr * (gwi / m + l2 * wi) for wi, gwi in zip(w, gw)]
        b -= lr * gb / m
    return {"w": w, "b": b, "n": len(data), "positives": int(sum(y for _, y in data))}


def predict(model, finding):
    z = sum(wi * xi for wi, xi in zip(model["w"], features(finding))) + model["b"]
    return 1 / (1 + math.exp(-min(30, max(-30, z))))


def calibration(findings):
    """Attach the team-calibrated probability to each finding when enough verdicts exist; otherwise explain why not."""
    data = training_set()
    if len(data) < MIN_LABELS or len({y for _, y in data}) < 2:
        return {"available": False, "labels": len(data), "needed": MIN_LABELS,
                "note": "Calibration starts once the team has marked at least 12 findings as escalated or explained/dismissed, with both outcomes present."}
    model = train(data)
    for f in findings:
        f["team_calibration"] = round(predict(model, f), 2)
    strongest = sorted(zip(FEATURES, model["w"]), key=lambda kv: -abs(kv[1]))[:5]
    return {"available": True, "labels": model["n"], "escalated": model["positives"],
            "influential_patterns": [{"pattern": k, "weight": round(v, 2)} for k, v in strongest],
            "note": "Share of findings with a similar indicator pattern that this team escalated in the past; it reflects the team's judgement, not truth."}
