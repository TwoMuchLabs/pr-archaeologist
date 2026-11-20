#!/usr/bin/env python3
"""Summarise what a `dig --prs` run learned from pull requests. Used by the field-test workflow.

    python scripts/field_report.py /path/to/analysed/repo
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pr_archaeologist import cache, model  # noqa: E402


def main(repo: str) -> int:
    state = cache.load(repo)
    prs = state.get("prs", {})
    real = [p for p in prs.values() if p.get("state") != "missing"]
    with_reason = [p for p in real if p.get("why")]
    with_warning = [p for p in real if p.get("warn")]
    with_review = [p for p in real if p.get("discussion")]
    print(f"commits analysed:          {len(state['commits'])}")
    print(f"pull requests read:        {len(real)}  (numbers that were not pull requests: {len(prs) - len(real)})")
    print(f"  with a stated reason:    {len(with_reason)}")
    print(f"  with a warning:          {len(with_warning)}")
    print(f"  with reviewer remarks:   {len(with_review)}  ({sum(len(p['discussion']) for p in with_review)} remarks kept)")

    issues = [i for i in state.get("issues", {}).values() if i.get("state") != "missing"]
    print(f"issues read:               {len(issues)}  (closed by a commit or pull request)")
    for issue in sorted(issues, key=lambda i: -i["number"])[:12]:
        print(f"  #{issue['number']} {issue['title'][:110]}")
        if issue.get("lead"):
            print(f"      {issue['lead'][:200]}")

    print("\nReviewer remarks kept (first 25):")
    shown = 0
    for p in sorted(with_review, key=lambda p: -p["number"]):
        for d in p["discussion"]:
            if shown >= 25:
                break
            print(f"  PR #{p['number']} {d['author']}: {d['text'][:220]}")
            shown += 1

    report = model.build(state, os.path.basename(os.path.abspath(repo)), "", state.get("config", {}).get("ignore", []))
    print("\nTop ranked files and what pull requests added to them:")
    for f in model.ranked(report)[:8]:
        extra = [p for p in f["pitfalls"] if p["ref"].startswith(("PR #", "#"))]
        print(f"  #{f['rank']} {f['path']}  {f['level']} {f['score']}  ({len(extra)} from pull requests and issues)")
        for p in extra[:3]:
            print(f"      [{p['kind']}] {p['ref']}: {p['text'][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
