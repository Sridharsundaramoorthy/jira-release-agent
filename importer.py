#!/usr/bin/env python3
"""Idempotent importer: loads inputs/dataset.json into a Jira Cloud project.

What it does
  * one Jira Task per dataset record, summary "[SOURCE-ID] original summary"
  * labels on EVERY issue: the dataset label (tyn-release-assessment-v2),
    a kind label (rr-requirement / rr-evidence / rr-work / rr-manifest)
    and a release label (release-R2.4)
  * description keeps source id, recorded release, recorded status and the
    original description text
  * every comment is posted with its original text unchanged. The text already
    starts with "Recorded event <timestamp> | actor <name> | role <role>", so the
    scenario actor and time are preserved IN THE TEXT. Jira's native author and
    created time belong to this importer and are not the scenario actors.
  * dataset links become Jira issue links
  * Jira status is moved to the recorded status (context only, never proof)

Safe to re-run: existing issues, comments and links are detected and skipped.

Usage
  python importer.py --check      # test credentials, project, issue/link types
  python importer.py --dry-run    # show what would be created
  python importer.py              # import (resumes if interrupted)
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing environment variable {name}. See .env.example")
    return value


# Keep only scheme + host, so a pasted URL with ?continue=... still works.
_u = urlparse(env("JIRA_BASE_URL", required=True).strip())
BASE = f"{_u.scheme}://{_u.netloc}"
EMAIL = env("JIRA_EMAIL", required=True)
TOKEN = env("JIRA_API_TOKEN", required=True)
PROJECT = env("JIRA_PROJECT_KEY", required=True)
ISSUE_TYPE = env("JIRA_ISSUE_TYPE", "Task")
LINK_TYPE = env("JIRA_LINK_TYPE", "Relates")
DATASET_PATH = env("DATASET_PATH", "inputs/dataset.json")
WORKERS = int(env("IMPORT_WORKERS", "4"))

SESSION = requests.Session()
SESSION.auth = (EMAIL, TOKEN)
SESSION.headers.update({"Accept": "application/json"})


class JiraError(RuntimeError):
    pass


def call(method, path, **kwargs):
    """HTTP call with retry/backoff on 429 and 5xx (honours Retry-After)."""
    url = BASE + path
    for attempt in range(7):
        try:
            resp = SESSION.request(method, url, timeout=60, **kwargs)
        except requests.RequestException as exc:
            wait = 2 ** attempt
            print(f"  network error ({exc}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  HTTP {resp.status_code}; retry in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise JiraError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp
    raise JiraError(f"{method} {path} failed after retries")


def adf(text):
    """Plain text -> Atlassian Document Format (one paragraph per line)."""
    lines = [ln for ln in text.split("\n") if ln.strip()] or ["(empty)"]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": ln}]}
            for ln in lines
        ],
    }


def build_description(rec):
    head = (
        f"Source ID: {rec['source_id']}\n"
        f"Recorded release: {rec['release']}\n"
        f"Recorded status (as in source data): {rec['recorded_status']}\n"
        f"Imported fictional record. Original text follows.\n"
    )
    return adf(head + rec["description"])


def labels_for(rec, dataset_label):
    return [dataset_label, f"rr-{rec['kind']}", f"release-{rec['release']}"]


# ---------------------------------------------------------------- check
def check():
    me = call("GET", "/rest/api/3/myself").json()
    print(f"Auth OK as: {me.get('displayName')} ({me.get('emailAddress', 'email hidden')})")
    proj = call("GET", f"/rest/api/3/project/{PROJECT}").json()
    types = [t["name"] for t in proj.get("issueTypes", [])]
    print(f"Project OK: {proj['key']} - {proj['name']}")
    print(f"Issue types in project: {types}")
    if ISSUE_TYPE not in types:
        print(f"  WARNING: '{ISSUE_TYPE}' not found. Set JIRA_ISSUE_TYPE in .env")
    links = [t["name"] for t in call("GET", "/rest/api/3/issueLinkType").json()["issueLinkTypes"]]
    print(f"Issue link types: {links}")
    if LINK_TYPE not in links:
        print(f"  WARNING: link type '{LINK_TYPE}' not found. Set JIRA_LINK_TYPE in .env")


# ------------------------------------------------------- existing state
SID_RE = re.compile(r"^\[([^\]]+)\]")


def fetch_existing(dataset_label):
    """Map source_id -> {key, status, links:set(frozenset({keyA,keyB}))}."""
    existing, token = {}, None
    jql = f'project = "{PROJECT}" AND labels = "{dataset_label}" ORDER BY key ASC'
    while True:
        body = {"jql": jql, "fields": ["summary", "status", "issuelinks"], "maxResults": 100}
        if token:
            body["nextPageToken"] = token
        data = call("POST", "/rest/api/3/search/jql", json=body).json()
        for issue in data.get("issues", []):
            f = issue["fields"]
            m = SID_RE.match(f.get("summary", ""))
            if not m or m.group(1) in existing:
                continue
            pairs = set()
            for lk in f.get("issuelinks", []):
                other = lk.get("outwardIssue") or lk.get("inwardIssue")
                if other:
                    pairs.add(frozenset((issue["key"], other["key"])))
            existing[m.group(1)] = {
                "key": issue["key"],
                "status": f["status"]["name"],
                "links": pairs,
            }
        token = data.get("nextPageToken")
        if not token:
            break
    return existing


# ------------------------------------------------------------- workers
def create_issue(rec, dataset_label):
    fields = {
        "project": {"key": PROJECT},
        "issuetype": {"name": ISSUE_TYPE},
        "summary": f"[{rec['source_id']}] {rec['summary']}"[:255],
        "labels": labels_for(rec, dataset_label),
        "description": build_description(rec),
    }
    resp = call("POST", "/rest/api/3/issue", json={"fields": fields})
    return rec["source_id"], resp.json()["key"]


def comment_count(key):
    r = call("GET", f"/rest/api/3/issue/{key}/comment", params={"startAt": 0, "maxResults": 1})
    return r.json().get("total", 0)


def sync_issue(rec, key, current_status):
    """Post missing comments (in order) and move status. Returns final comment count."""
    have = comment_count(key)
    for text in rec["comments"][have:]:
        call("POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf(text)})
        have += 1
    target = rec["recorded_status"]
    if current_status.lower() != target.lower():
        trs = call("GET", f"/rest/api/3/issue/{key}/transitions").json()["transitions"]
        match = [t for t in trs if t["to"]["name"].lower() == target.lower()]
        if match:
            call("POST", f"/rest/api/3/issue/{key}/transitions",
                 json={"transition": {"id": match[0]["id"]}})
        else:
            print(f"  WARNING {key}: no transition to '{target}' (status stays '{current_status}')",
                  file=sys.stderr)
    return have


def create_link(source_key, target_key):
    call("POST", "/rest/api/3/issueLink", json={
        "type": {"name": LINK_TYPE},
        "outwardIssue": {"key": source_key},
        "inwardIssue": {"key": target_key},
    })


def run_parallel(label, jobs, fn):
    """jobs: list of (id, args). Returns (results dict, failures list)."""
    results, failures = {}, []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(fn, *args): jid for jid, args in jobs}
        done = 0
        for fut in as_completed(futs):
            jid = futs[fut]
            done += 1
            try:
                results[jid] = fut.result()
            except Exception as exc:  # keep going; a re-run resumes
                failures.append((jid, str(exc)))
            if done % 25 == 0 or done == len(jobs):
                print(f"  {label}: {done}/{len(jobs)}")
    return results, failures


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="test connection and exit")
    ap.add_argument("--dry-run", action="store_true", help="show plan, change nothing")
    args = ap.parse_args()

    if args.check:
        check()
        return 0

    with open(DATASET_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    dataset_label = data["dataset_label"]
    records = data["records"]
    by_id = {r["source_id"]: r for r in records}
    exp_comments = sum(len(r["comments"]) for r in records)
    exp_links = {frozenset((r["source_id"], t)) for r in records for t in r["links"]}
    print(f"Dataset '{dataset_label}': {len(records)} records, "
          f"{exp_comments} comments, {len(exp_links)} links")

    existing = fetch_existing(dataset_label)
    to_create = [r for r in records if r["source_id"] not in existing]
    print(f"Already in Jira: {len(existing)}; to create: {len(to_create)}")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0

    t0 = time.time()
    failures = []

    # 1. issues
    if to_create:
        res, fails = run_parallel("issues", [(r["source_id"], (r, dataset_label)) for r in to_create],
                                  create_issue)
        failures += fails
        for sid, (_, key) in res.items():
            existing[sid] = {"key": key, "status": "To Do", "links": set()}

    # 2. comments + status
    jobs = [(r["source_id"], (r, existing[r["source_id"]]["key"], existing[r["source_id"]]["status"]))
            for r in records if r["source_id"] in existing]
    counts, fails = run_parallel("comments/status", jobs, sync_issue)
    failures += fails

    # 3. links
    link_jobs, seen = [], set()
    for r in records:
        for t in r["links"]:
            if r["source_id"] not in existing or t not in existing:
                failures.append((f"link {r['source_id']}->{t}", "endpoint issue missing"))
                continue
            a, b = existing[r["source_id"]]["key"], existing[t]["key"]
            pair = frozenset((a, b))
            already = any(pair in e["links"] for e in existing.values())
            if pair in seen or already:
                continue
            seen.add(pair)
            link_jobs.append((f"{r['source_id']}->{t}", (a, b)))
    link_res, fails = run_parallel("links", link_jobs, create_link)
    failures += fails

    print("\n=== Import summary ===")
    print(f"Issues in Jira: {len(existing)} / {len(records)}")
    print(f"Comments in Jira: {sum(counts.values())} / {exp_comments}")
    print(f"Links created this run: {len(link_res)} of {len(link_jobs)} attempted "
          f"(dataset has {len(exp_links)} distinct links; existing ones are skipped)")
    print(f"Elapsed: {time.time() - t0:.0f}s")
    if failures:
        print(f"\n{len(failures)} failure(s). Re-run the importer to resume:")
        for jid, msg in failures[:20]:
            print(f"  - {jid}: {msg}")
        return 1
    print("No failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
