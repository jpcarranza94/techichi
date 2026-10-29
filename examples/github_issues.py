#!/usr/bin/env python3
"""A command Source: open GitHub issues with a label, assigned to you, start work. No Jira needed.

  [factory]
  sources = ["github", "issues"]

  [sources.issues]
  command = ["python3", "/path/to/techichi/examples/github_issues.py", "--project", "APP", "--repo", "acme/app"]
  every_seconds = 120

Each issue becomes an admitted ticket APP-<number>. The branch is APP-<number>-<slug>. Remove the label, or close
the issue, to stop new admissions. It reads with `gh` and never writes to GitHub.
"""
import argparse
import json
import re
import subprocess

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--project", required=True, help="the policy project key, for example APP")
ap.add_argument("--repo", required=True, help="owner/name")
ap.add_argument("--label", default="techichi", help="the label that admits an issue (default: techichi)")
a = ap.parse_args()

issues = json.loads(subprocess.run(
    ["gh", "issue", "list", "-R", a.repo, "--state", "open", "--label", a.label, "--assignee", "@me",
     "--json", "number,title,url,createdAt", "--limit", "100"],
    capture_output=True, text=True, check=True).stdout)
for i in issues:
    slug = "-".join(re.findall(r"[a-z0-9]+", i["title"].lower())[:5])
    print(json.dumps({"id": f"{a.repo}#{i['number']}", "kind": "ticket", "ref": f"{a.project}-{i['number']}",
                      "payload": {"what": "ready", "actor": "github", "at": i["createdAt"], "url": i["url"],
                                  "self": False, "admitted": True, "project": a.project, "slug": slug}}))
