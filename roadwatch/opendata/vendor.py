"""One supplier followed across governments.

A supplier case names a company (plus other names it trades under) and, optionally, the province whose records and
largest city's records to add. Tiers:
  federal      Government of Canada - contracts over $10k, grants (opt-in), Registry of Lobbyists, PSPC ineligibility list
  provincial   Ontario Public Accounts payments; Québec: RENA (barred companies) and SEAO bids for Montréal buyers
  municipal    Toronto Bids awards, non-competitive contracts and lobbyist registry; Montréal contracts
Every dataset becomes a citable document; the vendor_indicators compute cross-tier leads from the rows.
"""
from . import canada, montreal, ontario, toronto

PROVINCES = {
    "on": {"key": "on", "name": "Ontario", "city": "toronto", "city_name": toronto.NAME,
           "tiers": "Ontario Public Accounts payments; Toronto awards, non-competitive contracts and lobbyist registry"},
    "qc": {"key": "qc", "name": "Québec", "city": "montreal", "city_name": montreal.NAME,
           "tiers": "RENA (companies barred from public contracts); Montréal contracts and SEAO tenders"},
}
TIERS = {"fed_contracts": "federal", "fed_grants": "federal", "fed_lobbying": "federal", "fed_ineligible": "federal",
         "on_payments": "provincial", "qc_debarment": "provincial", "qc_bids": "provincial",
         "city_awards": "municipal", "city_non_competitive": "municipal", "city_lobbying": "municipal"}


def names_of(vendor):
    """The company's name followed by its other names, de-duplicated and non-blank."""
    return [n for n in dict.fromkeys([vendor.get("name", "")] + list(vendor.get("aliases") or [])) if n and n.strip()]


def _rekey(result, key, title=None):
    """Reuse a street-case connector under a supplier-case key (and title: the first line of page 1 is the title)."""
    result["key"] = key
    if title:
        first = result["pages"][0]
        _, _, rest = first["text"].partition("\n")
        first["text"] = title + ("\n" + rest if rest else "")
        result["title"] = title
    return result


def profile(names, province, since_year):
    """Run every connector of every applicable tier; a failing dataset is reported in `errors`, never fatal."""
    results, errors = canada.profile(names, since_year)
    steps = []
    if province == "on":
        steps = [("on_payments", lambda: ontario.payments(names, since_year)),
                 ("city_awards", lambda: toronto.supplier_awards(names, since_year)),
                 ("city_non_competitive", lambda: _rekey(toronto.non_competitive(names, since_year), "city_non_competitive",
                                                        f"Toronto Bids - non-competitive contracts to {names[0]}")),
                 ("city_lobbying", lambda: _rekey(toronto.lobbying(names, [], since_year), "city_lobbying",
                                                  f"Toronto Lobbyist Registry - {names[0]} as client"))]
    elif province == "qc":
        steps = [("city_awards", lambda: montreal.supplier_contracts(names, since_year)),
                 ("qc_bids", lambda: _rekey(montreal.seao_bids([], names, since_year), "qc_bids",
                                            f"SEAO tenders - bids and awards involving {names[0]}")),
                 ("qc_debarment", lambda: _rekey(montreal.debarment(names, _roles(results)), "qc_debarment"))]
    for key, step in steps:
        try:
            results.append(step())
        except Exception as error:  # portal errors should not lose the other datasets
            errors.append(f"{key}: {type(error).__name__}")
    return results, errors


def _roles(results):
    """Contracts the supplier won in the datasets pulled so far, for RENA's 'won while barred' count."""
    roles = {}
    for d in results:
        for r in d["records"]:
            if d["key"] in ("city_awards", "fed_contracts") and r.get("vendor") and not r.get("is_amendment"):
                roles.setdefault(r["vendor"], {"won": [], "bid": []})["won"].append(
                    (r.get("award_date") or r.get("date") or "", r.get("description") or ""))
    return roles
