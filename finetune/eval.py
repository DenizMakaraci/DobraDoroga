"""Compare providers on the roadwatch investigation task - the honest 'is it better?' check.

Replays a quick investigation on every project that has evidence, once per provider,
and scores what the task actually needs:

  parsed    - the model returned a valid report object
  accepted  - findings whose citations all passed citation_valid
  rejected  - findings thrown away for citing text that does not exist (fabrication rate)
  seconds   - wall time per investigation

Usage:
    .venv/bin/python finetune/eval.py local default          # fine-tuned vs teacher
    .venv/bin/python finetune/eval.py local --project <id>
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roadwatch import ai, store  # noqa: E402
from roadwatch.analysis import analyze  # noqa: E402


async def run(project, provider_id):
    start = time.monotonic()
    try:
        report = await ai.investigate(project, analyze(project), provider_id=provider_id, depth="quick")
        return {"ok": True, "accepted": len(report["findings"]), "rejected": report["rejected_findings"],
                "missing": len(report["missing_records"]), "seconds": round(time.monotonic() - start, 1)}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__, "seconds": round(time.monotonic() - start, 1)}


async def main(providers, project_ids):
    ids = project_ids or [p["id"] for p in store.list_projects()]
    projects = [p for pid in ids if (p := store.get_project(pid)) and p.get("evidence")]
    results = {pid: [] for pid in providers}
    for project in projects:
        print(f"{project['name']}")
        for pid in providers:
            outcome = await run(project, pid)
            results[pid].append(outcome)
            print(f"  {pid}: {json.dumps(outcome)}")
    print("\n== summary ==")
    for pid in providers:
        rows = results[pid]
        ok = [r for r in rows if r["ok"]]
        print(f"{pid}: {len(ok)}/{len(rows)} parsed, "
              f"{sum(r['accepted'] for r in ok)} accepted findings, "
              f"{sum(r['rejected'] for r in ok)} rejected (fabricated citations), "
              f"avg {round(sum(r['seconds'] for r in rows) / len(rows), 1) if rows else 0}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("providers", nargs="+", help="provider ids, e.g. local default")
    parser.add_argument("--project", action="append", dest="project_ids")
    args = parser.parse_args()
    asyncio.run(main(args.providers, args.project_ids))
