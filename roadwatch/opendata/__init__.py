"""Open-data connectors. Each city module exposes `profile(street, since_year)` returning dataset
results that become evidence documents (one per dataset) plus structured rows for the checks.
`vendor.profile(names, province, since_year)` does the same for one supplier across federal,
provincial and municipal sources."""
from . import canada, montreal, ontario, toronto, vendor
from .common import company_matcher, flag_limits, mentions_street, street_key

CITIES = {"toronto": toronto, "montreal": montreal}

__all__ = ["CITIES", "canada", "company_matcher", "flag_limits", "mentions_street", "ontario", "street_key", "vendor"]
