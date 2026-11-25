#!/usr/bin/env python3
"""Jira Release Evidence Agent.

Assesses whether the requirements of a target release are satisfied by evidence
recorded in Jira. The model never sees dataset.json: it investigates through
tools that query Jira live (scoped to the dataset label), then records one
decision per requirement. The overall decision is computed in code.

Usage
  python agent.py --release R2.4
  python agent.py --project SCRUM --label tyn-release-assessment-v2 --release R2.4

Outputs (runs/<timestamp>-<release>/): report.md, report.json, trace.jsonl
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import errors, types

# Reuses importer.py's env loading and retrying Jira HTTP helper (unchanged).
from importer import BASE, JiraError, call

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "120"))
MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "300"))
MAX_SECONDS = int(os.environ.get("AGENT_MAX_SECONDS", "1800"))
KEEP_FULL_RESULTS = int(os.environ.get("AGENT_KEEP_FULL_RESULTS", "14"))

DECISIONS = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
REF_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)(?:#c(\d+))?$")
SID_RE = re.compile(r"^\[([^\]]+)\]\s*")


class ToolError(Exception):
    pass


def clip(s, n):
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars]"


def esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def adf_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = [adf_text(c) for c in node.get("content", [])]
    if t in ("doc", "bulletList", "orderedList", "listItem"):
        return "\n".join(x for x in inner if x)
    return "".join(inner)


def kind_of(labels):
    return next((l[3:] for l in labels if l.startswith("rr-")), None)


def release_of(labels):
    return next((l[len("release-"):] for l in labels if l.startswith("release-")), None)


# ------------------------------------------------------------------ state
class Ctx:
    def __init__(self, project, label, release):
        self.project, self.label, self.release = project, label, release
        self.sid2key, self.key2sid, self.meta = {}, {}, {}
        self.comments = {}          # sid -> [{id, text}]
        self.viewed_issues = set()  # sids opened with get_issue
        self.viewed_comments = set()  # (sid, n) returned to the model
        self.decisions = {}
        self.target_reqs = []
        self.finished = False
        self.finish_warned = False
        self.notes = ""
        self.failures = []


def jql_search(jql, fields, max_results=100, token=None):
    body = {"jql": jql, "fields": fields, "maxResults": max_results}
    if token:
        body["nextPageToken"] = token
    return call("POST", "/rest/api/3/search/jql", json=body).json()


def build_index(ctx):
    """Metadata only (source id <-> key, labels, status). Used to resolve ids and
    enforce dataset scope; the model never receives this index wholesale."""
    jql = f'project = "{esc(ctx.project)}" AND labels = "{esc(ctx.label)}" ORDER BY key ASC'
    token = None
    while True:
        data = jql_search(jql, ["summary", "status", "labels"], token=token)
        for it in data.get("issues", []):
            f = it["fields"]
            m = SID_RE.match(f.get("summary", ""))
            if not m or m.group(1) in ctx.sid2key:
                continue
            sid, key = m.group(1), it["key"]
            ctx.sid2key[sid], ctx.key2sid[key] = key, sid
            labels = f.get("labels", [])
            ctx.meta[sid] = {
                "key": key,
                "title": SID_RE.sub("", f["summary"]),
                "status": f["status"]["name"],
                "kind": kind_of(labels),
                "release": release_of(labels),
            }
        token = data.get("nextPageToken")
        if not token:
            break
    ctx.target_reqs = sorted(
        s for s, m in ctx.meta.items() if m["kind"] == "requirement" and m["release"] == ctx.release
    )


def resolve(ctx, ident):
    ident = str(ident).strip()
    for s in (ident, ident.upper()):
        if s in ctx.sid2key:
            return s, ctx.sid2key[s]
        if s in ctx.key2sid:
            return ctx.key2sid[s], s
    raise ToolError(
        f"'{ident}' is not in the dataset scope (label {ctx.label}). Use a source id like REQ-02 or a Jira key."
    )


def load_comments(ctx, sid):
    if sid in ctx.comments:
        return ctx.comments[sid]
    key, out, start = ctx.sid2key[sid], [], 0
    while True:
        d = call("GET", f"/rest/api/3/issue/{key}/comment",
                 params={"startAt": start, "maxResults": 100, "orderBy": "created"}).json()
        batch = d.get("comments", [])
        out += [{"id": c["id"], "text": adf_text(c.get("body"))} for c in batch]
        start += len(batch)
        if not batch or start >= d.get("total", 0):
            break
    ctx.comments[sid] = out
    return out


# ------------------------------------------------------------------ tools
def t_search_issues(ctx, kind=None, release=None, text=None, status=None,
                    max_results=25, page_token=None):
    parts = [f'project = "{esc(ctx.project)}"', f'labels = "{esc(ctx.label)}"']
    if kind:
        parts.append(f'labels = "rr-{esc(kind)}"')
    if release:
        parts.append(f'labels = "release-{esc(str(release).removeprefix("release-"))}"')
    if status:
        parts.append(f'status = "{esc(status)}"')
    if text:
        parts.append(f'text ~ "{esc(text)}"')
    jql = " AND ".join(parts) + " ORDER BY key ASC"
    data = jql_search(jql, ["summary", "status", "labels"],
                      max_results=max(1, min(int(max_results), 50)), token=page_token)
    items = []
    for it in data.get("issues", []):
        f = it["fields"]
        labels = f.get("labels", [])
        sid = ctx.key2sid.get(it["key"]) or (SID_RE.match(f["summary"]) or [None, None])[1]
        items.append({
            "source_id": sid, "key": it["key"], "title": SID_RE.sub("", f["summary"]),
            "kind": kind_of(labels), "release": release_of(labels), "status": f["status"]["name"],
        })
    return {"count": len(items), "issues": items, "next_page_token": data.get("nextPageToken")}


def t_get_issue(ctx, id):
    sid, key = resolve(ctx, id)
    d = call("GET", f"/rest/api/3/issue/{key}",
             params={"fields": "summary,status,labels,description,issuelinks"}).json()["fields"]
    ctx.viewed_issues.add(sid)
    labels = d.get("labels", [])
    links = []
    for lk in d.get("issuelinks", []):
        if lk.get("outwardIssue"):
            other, rel = lk["outwardIssue"], lk["type"].get("outward", lk["type"]["name"])
        else:
            other, rel = lk["inwardIssue"], lk["type"].get("inward", lk["type"]["name"])
        osid = ctx.key2sid.get(other["key"])
        m = ctx.meta.get(osid, {})
        links.append({
            "relation": rel, "source_id": osid, "key": other["key"], "in_scope": osid is not None,
            "title": m.get("title"), "kind": m.get("kind"), "release": m.get("release"),
            "status": m.get("status"),
        })
    desc = adf_text(d.get("description"))
    return {
        "source_id": sid, "key": key, "url": f"{BASE}/browse/{key}",
        "kind": kind_of(labels), "release": release_of(labels), "labels": labels,
        "jira_status_context_only": d["status"]["name"],
        "title": SID_RE.sub("", d["summary"]),
        "description": clip(desc, 4000), "links": links,
        "comment_count_hint": "use get_comments / grep_comments for comments",
    }


def t_get_comments(ctx, id, start=1, count=10, max_chars=1500):
    sid, _ = resolve(ctx, id)
    allc = load_comments(ctx, sid)
    start, count = max(1, int(start)), max(1, min(int(count), 25))
    max_chars = max(200, min(int(max_chars), 6000))
    out = []
    for n in range(start, min(start + count, len(allc) + 1)):
        txt = allc[n - 1]["text"]
        ctx.viewed_comments.add((sid, n))
        out.append({"ref": f"{sid}#c{n}", "text": clip(txt, max_chars), "chars": len(txt)})
    nxt = start + count if start + count <= len(allc) else None
    return {"source_id": sid, "total_comments": len(allc), "comments": out, "next_start": nxt}


def t_grep_comments(ctx, id, pattern, ignore_case=True):
    sid, _ = resolve(ctx, id)
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    allc, hits = load_comments(ctx, sid), []
    for n, c in enumerate(allc, 1):
        m = rx.search(c["text"])
        if m:
            a, b = max(0, m.start() - 150), min(len(c["text"]), m.end() + 150)
            ctx.viewed_comments.add((sid, n))
            hits.append({"ref": f"{sid}#c{n}", "snippet": c["text"][a:b]})
        if len(hits) >= 20:
            break
    return {"source_id": sid, "total_comments": len(allc), "matches": hits}


def _check_refs(ctx, evidence):
    clean, bad = [], []
    for e in evidence or []:
        ref, note = str(e.get("ref", "")).strip(), str(e.get("note", "")).strip()
        m = REF_RE.match(ref)
        if not m:
            bad.append(f"{ref!r}: use SOURCEID or SOURCEID#cN")
            continue
        try:
            sid, _ = resolve(ctx, m.group(1))
        except ToolError:
            bad.append(f"{ref!r}: not in dataset scope")
            continue
        if sid not in ctx.viewed_issues and not any(s == sid for s, _ in ctx.viewed_comments):
            bad.append(f"{ref!r}: you have not opened this issue in this run")
            continue
        if m.group(2):
            n = int(m.group(2))
            if (sid, n) not in ctx.viewed_comments:
                bad.append(f"{ref!r}: comment {n} was never returned by a tool in this run")
                continue
            clean.append({"ref": f"{sid}#c{n}", "note": note})
        else:
            clean.append({"ref": sid, "note": note})
    return clean, bad


def t_submit_decision(ctx, requirement_id, decision, mandatory, explanation,
                      evidence=None, outstanding=""):
    if decision not in DECISIONS:
        raise ToolError(f"decision must be one of {DECISIONS}")
    sid, key = resolve(ctx, requirement_id)
    if ctx.meta[sid]["kind"] != "requirement":
        raise ToolError(f"{sid} is a '{ctx.meta[sid]['kind']}' record, not a requirement")
    if sid not in ctx.viewed_issues:
        raise ToolError(f"open {sid} with get_issue before deciding it")
    clean, bad = _check_refs(ctx, evidence)
    if bad:
        raise ToolError("evidence refs rejected: " + "; ".join(bad))
    if decision in ("PASS", "FAIL") and not clean:
        raise ToolError(f"{decision} needs at least one evidence ref you actually read")
    ctx.decisions[sid] = {
        "requirement": sid, "key": key, "title": ctx.meta[sid]["title"],
        "decision": decision, "mandatory": bool(mandatory),
        "explanation": str(explanation).strip(), "evidence": clean,
        "outstanding": str(outstanding or "").strip(),
    }
    remaining = [s for s in ctx.target_reqs if s not in ctx.decisions]
    return {"recorded": sid, "decided": len(ctx.decisions), "remaining_target_requirements": remaining}


def t_finish_assessment(ctx, notes=""):
    missing = [s for s in ctx.target_reqs if s not in ctx.decisions]
    if missing and not ctx.finish_warned:
        ctx.finish_warned = True
        return {"accepted": False, "undecided_requirements": missing,
                "message": "These requirements for the target release have no decision. Decide them, "
                           "or call finish_assessment again to leave them as not assessed."}
    for s in missing:
        fill_unassessed(ctx, s, "Not assessed: the agent finished without deciding this requirement.")
    ctx.notes, ctx.finished = str(notes or "").strip(), True
    return {"accepted": True, "decisions": len(ctx.decisions)}


def fill_unassessed(ctx, sid, why):
    m = ctx.meta[sid]
    ctx.decisions[sid] = {
        "requirement": sid, "key": m["key"], "title": m["title"],
        "decision": "INSUFFICIENT_EVIDENCE", "mandatory": True, "explanation": why,
        "evidence": [], "outstanding": "Investigation incomplete; re-run or review manually.",
        "not_assessed": True,
    }


TOOLS = {
    "search_issues": t_search_issues, "get_issue": t_get_issue, "get_comments": t_get_comments,
    "grep_comments": t_grep_comments, "submit_decision": t_submit_decision,
    "finish_assessment": t_finish_assessment,
}


def run_tool(ctx, name, args):
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(ctx, **(args or {}))
    except ToolError as e:
        return {"error": str(e)}
    except JiraError as e:
        return {"error": f"Jira error: {clip(str(e), 300)}"}
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except Exception as e:  # report honestly, let the model adapt
        return {"error": f"{type(e).__name__}: {clip(str(e), 300)}"}


# ------------------------------------------------------- tool declarations
T = types.Type


def S(t, desc=None, **kw):
    return types.Schema(type=t, description=desc, **kw)


def decl(name, desc, props=None, required=()):
    if not props:
        return types.FunctionDeclaration(name=name, description=desc)
    return types.FunctionDeclaration(
        name=name, description=desc,
        parameters=types.Schema(type=T.OBJECT, properties=props, required=list(required)))


def declarations():
    ev = S(T.OBJECT, properties={
        "ref": S(T.STRING, "SOURCEID (issue/description) or SOURCEID#cN (comment N)"),
        "note": S(T.STRING, "what this evidence shows and why it applies (release/commit/service/env/prompt version)"),
    }, required=["ref", "note"])
    return [types.Tool(function_declarations=[
        decl("search_issues",
             "Search Jira issues in the dataset scope. Returns compact rows (source_id, key, title, kind, release, status). "
             "kind is one of requirement, evidence, work, manifest. Use text for keyword search over issues and comments.",
             {"kind": S(T.STRING, "requirement | evidence | work | manifest"),
              "release": S(T.STRING, "release label value, e.g. R2.4"),
              "text": S(T.STRING, "keyword(s)"), "status": S(T.STRING, "Jira status name (context only)"),
              "max_results": S(T.INTEGER, "default 25, max 50"),
              "page_token": S(T.STRING, "next_page_token from a previous call")}),
        decl("get_issue",
             "Open one issue: description and links (relation, linked source id, kind, release, status). "
             "Linked records do not necessarily apply to the target build.",
             {"id": S(T.STRING, "source id (REQ-02) or Jira key")}, ["id"]),
        decl("get_comments",
             "Read comments of an issue in order, 1-based. Each starts with 'Recorded event <time> | actor | role'. "
             "Returns refs like REQ-02#c3.",
             {"id": S(T.STRING), "start": S(T.INTEGER, "first comment number, default 1"),
              "count": S(T.INTEGER, "default 10, max 25"),
              "max_chars": S(T.INTEGER, "per-comment truncation, default 1500, max 6000")}, ["id"]),
        decl("grep_comments",
             "Regex search inside one issue's comments (e.g. a commit hash, environment, prompt version, 'superseded'). "
             "Cheaper than reading every comment.",
             {"id": S(T.STRING), "pattern": S(T.STRING, "regular expression"),
              "ignore_case": S(T.BOOLEAN)}, ["id", "pattern"]),
        decl("submit_decision",
             "Record the final decision for one requirement. Evidence refs must be ones you read this run. "
             "PASS/FAIL need at least one evidence ref. Can be called again to revise.",
             {"requirement_id": S(T.STRING), "decision": S(T.STRING, "PASS | FAIL | INSUFFICIENT_EVIDENCE",
                                                          enum=list(DECISIONS)),
              "mandatory": S(T.BOOLEAN, "is this requirement mandatory per the manifest/requirement text"),
              "explanation": S(T.STRING, "why, including which evidence applies or is inapplicable and why"),
              "evidence": S(T.ARRAY, "supporting references", items=ev),
              "outstanding": S(T.STRING, "verification or clarification still needed, if any")},
             ["requirement_id", "decision", "mandatory", "explanation"]),
        decl("finish_assessment",
             "Call once every applicable requirement has a decision. Optional overall notes.",
             {"notes": S(T.STRING, "optional overall notes")}),
    ])]


SYSTEM = """You are a release-readiness investigator. Your only evidence source is Jira, reached through tools. \
Never use outside knowledge as evidence and never invent results.

Workflow
1. Find the release manifest (search_issues kind=manifest), read its description and comments: target build \
identifiers (release, commit, service, environment, prompt version) and the assessment rules.
2. List the target requirements (kind=requirement, release=<target>; page through all results).
3. For each requirement: get_issue (acceptance criterion, links), get_comments, then open linked evidence/work/approval \
records. Decide whether each piece of evidence APPLIES: same release, commit, service, environment and any required \
prompt version. Records of other releases/builds are not proof. Watch for later comments that supersede, retract, \
rerun or contradict earlier ones (order by the recorded event timestamp), and for approvals from the wrong role. \
Use grep_comments to check specific identifiers cheaply.
4. submit_decision for every requirement, then finish_assessment.

Decision rules
- PASS: applicable evidence establishes the acceptance criterion is satisfied.
- FAIL: applicable evidence establishes a failure or violation.
- INSUFFICIENT_EVIDENCE: evidence is missing, inapplicable or genuinely inconclusive. Missing proof is NOT a failure.
- Jira status (Done, etc.) is context only, never proof. Resolve what the evidence supports; do not default to unknown.

Notes
- Comments begin with 'Recorded event <timestamp> | actor <name> | role <role>'. That is the scenario actor and time. \
Jira's native author and created time belong to the importer; never cite them as the original actor.
- Cite only refs you have read (SOURCEID or SOURCEID#cN). Explain applicability explicitly in each decision.
- Be efficient. Older tool results are trimmed to save context; re-fetch only if you truly need them. Do not re-read \
what you have already judged. Stay within budget."""


# ----------------------------------------------------------- model plumbing
def compact(contents, keep):
    idx = [i for i, c in enumerate(contents)
           if c.role == "user" and c.parts and any(p.function_response for p in c.parts)]
    for i in idx[:-keep]:
        new = []
        for p in contents[i].parts:
            fr = p.function_response
            if fr and fr.name not in ("submit_decision", "finish_assessment"):
                s = json.dumps(fr.response, default=str)
                if len(s) > 400:
                    p = types.Part.from_function_response(
                        name=fr.name,
                        response={"result": "[older result trimmed to save context; re-fetch if needed] " + s[:300]})
            new.append(p)
        contents[i] = types.Content(role="user", parts=new)


class Run:
    def __init__(self, outdir):
        self.model = MODEL
        self.models_used = [MODEL]
        self.tokens = {"prompt": 0, "output": 0, "thoughts": 0, "total": 0}
        self.turns = self.tool_calls = self.tool_errors = self.retries = 0
        self.by_tool = {}
        self.trace = open(outdir / "trace.jsonl", "w", encoding="utf-8")
        self.t0 = time.time()

    def log(self, event, **kw):
        rec = {"t": round(time.time() - self.t0, 2), "event": event, **kw}
        self.trace.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        self.trace.flush()

    def add_usage(self, resp):
        u = getattr(resp, "usage_metadata", None)
        if not u:
            return
        self.tokens["prompt"] += u.prompt_token_count or 0
        self.tokens["output"] += u.candidates_token_count or 0
        self.tokens["thoughts"] += getattr(u, "thoughts_token_count", 0) or 0
        self.tokens["total"] += u.total_token_count or 0


def generate(client, run, contents, config):
    switched = False
    while True:
        for attempt in range(6):
            try:
                return client.models.generate_content(model=run.model, contents=contents, config=config)
            except Exception as e:
                code = getattr(e, "code", None) if isinstance(e, errors.APIError) else None
                retryable = code in (429, 500, 502, 503, 504) or not isinstance(e, errors.APIError)
                run.log("model_error", model=run.model, code=code, error=clip(str(e), 400), attempt=attempt)
                if not retryable or attempt == 5:
                    last = e
                    break
                run.retries += 1
                wait = min(60, 3 * 2 ** attempt)
                print(f"  model error ({code or type(e).__name__}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
        if switched or run.model == FALLBACK_MODEL:
            raise last
        print(f"  switching model {run.model} -> {FALLBACK_MODEL}", file=sys.stderr)
        run.log("model_fallback", from_model=run.model, to_model=FALLBACK_MODEL)
        run.model = FALLBACK_MODEL
        run.models_used.append(FALLBACK_MODEL)
        switched = True


# ------------------------------------------------------------------ report
def evidence_line(ctx, ev):
    ref, note = ev["ref"], ev["note"]
    sid, _, n = ref.partition("#c")
    key = ctx.sid2key.get(sid, "?")
    url = f"{BASE}/browse/{key}"
    header = ""
    if n:
        c = ctx.comments[sid][int(n) - 1]
        url += f"?focusedCommentId={c['id']}"
        first = c["text"].split("\n", 1)[0]
        header = clip(first, 200)
    line = f"[{ref}]({url}) ({key})"
    if header:
        line += f" - {header}"
    return f"{line}\n    - {note}" if note else line


def overall_decision(decisions, incomplete):
    mand = [d for d in decisions.values() if d["mandatory"]]
    if any(d["decision"] == "FAIL" for d in mand):
        return "BLOCKED"
    if not mand or incomplete or any(d["decision"] == "INSUFFICIENT_EVIDENCE" for d in mand):
        return "NEEDS_REVIEW"
    return "READY"


def write_report(ctx, run, outdir, args, elapsed):
    incomplete = (not ctx.finished) or bool(ctx.failures) or any(
        d.get("not_assessed") for d in ctx.decisions.values())
    overall = overall_decision(ctx.decisions, incomplete)
    ds = sorted(ctx.decisions.values(), key=lambda d: d["requirement"])
    counts = {k: sum(1 for d in ds if d["decision"] == k) for k in DECISIONS}
    stats = {
        "elapsed_seconds": round(elapsed, 1), "model_turns": run.turns, "tool_calls": run.tool_calls,
        "tool_errors": run.tool_errors, "model_retries": run.retries, "tool_calls_by_name": run.by_tool,
        "models_used": run.models_used, "tokens": run.tokens,
    }
    L = [f"# Release readiness report: {args.release}", "",
         f"- Jira project: `{ctx.project}`  |  dataset scope: `labels = {ctx.label}`",
         f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
         f"- **Overall decision: {overall}**" + ("  (run incomplete, see Failures)" if incomplete else ""), "",
         "Overall rule: BLOCKED if any mandatory requirement fails; NEEDS_REVIEW if none fails but at least one "
         "mandatory requirement has insufficient evidence; READY only if every mandatory requirement passes.",
         "Actors and timestamps shown next to comment references come from the recorded event text, "
         "not from Jira's native author/created fields (those belong to the importer).", "",
         f"PASS {counts['PASS']} | FAIL {counts['FAIL']} | INSUFFICIENT_EVIDENCE {counts['INSUFFICIENT_EVIDENCE']}", "",
         "| Requirement | Jira | Title | Mandatory | Decision |", "|---|---|---|---|---|"]
    for d in ds:
        L.append(f"| {d['requirement']} | [{d['key']}]({BASE}/browse/{d['key']}) | "
                 f"{d['title'].replace('|', '/')} | {'yes' if d['mandatory'] else 'no'} | {d['decision']} |")
    L += ["", "## Decisions", ""]
    for d in ds:
        L += [f"### {d['requirement']} - {d['title']}", "",
              f"- Decision: **{d['decision']}** ({'mandatory' if d['mandatory'] else 'not mandatory'})",
              f"- Explanation: {d['explanation']}"]
        if d["evidence"]:
            L.append("- Evidence:")
            L += [f"  - {evidence_line(ctx, e)}" for e in d["evidence"]]
        else:
            L.append("- Evidence: none cited")
        L += [f"- Outstanding verification / clarification: {d['outstanding'] or 'none'}", ""]
    if ctx.notes:
        L += ["## Agent notes", "", ctx.notes, ""]
    L += ["## Run statistics", "", "```json", json.dumps(stats, indent=2), "```", ""]
    L += ["## Failures", ""]
    L += [f"- {f}" for f in ctx.failures] or ["- none"]
    L += ["", f"Investigation trace: `{outdir / 'trace.jsonl'}`", ""]
    (outdir / "report.md").write_text("\n".join(L), encoding="utf-8")
    (outdir / "report.json").write_text(json.dumps(
        {"release": args.release, "project": ctx.project, "dataset_label": ctx.label, "overall": overall,
         "incomplete": incomplete, "decisions": ds, "stats": stats, "failures": ctx.failures,
         "notes": ctx.notes}, indent=2, ensure_ascii=False), encoding="utf-8")
    return overall, stats


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Jira release evidence agent")
    ap.add_argument("--project", default=os.environ.get("JIRA_PROJECT_KEY"))
    ap.add_argument("--label", default=os.environ.get("DATASET_LABEL", "tyn-release-assessment-v2"),
                    help="dataset scope: the Jira label applied by the importer")
    ap.add_argument("--release", required=True, help="target release, e.g. R2.4")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()
    if not args.project:
        sys.exit("Provide --project or set JIRA_PROJECT_KEY")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Missing GEMINI_API_KEY")

    outdir = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}-{args.release}"
    outdir.mkdir(parents=True, exist_ok=True)
    run, ctx = Run(outdir), Ctx(args.project, args.label, args.release)

    print(f"Indexing Jira scope ({args.project}, label {args.label}) ...")
    try:
        build_index(ctx)
    except Exception as e:
        sys.exit(f"Cannot read Jira: {e}")
    if not ctx.meta:
        sys.exit("No issues found in that project/label. Check --project and --label.")
    print(f"  {len(ctx.meta)} issues in scope; {len(ctx.target_reqs)} requirements labelled for {args.release}")
    run.log("start", project=args.project, label=args.label, release=args.release, model=run.model,
            issues_in_scope=len(ctx.meta), target_requirements=len(ctx.target_reqs))

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM, tools=declarations(), temperature=0.0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    task = (f"Assess release {args.release} in Jira project {args.project} (dataset scope label "
            f"{args.label}). Jira has {len(ctx.target_reqs)} requirement issues labelled for this release; "
            "make sure every applicable requirement gets a decision. Start with the manifest.")
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]

    nudges = 0
    try:
        for turn in range(1, MAX_TURNS + 1):
            if time.time() - run.t0 > MAX_SECONDS:
                ctx.failures.append(f"time budget of {MAX_SECONDS}s exhausted")
                break
            compact(contents, KEEP_FULL_RESULTS)
            run.turns = turn
            resp = generate(client, run, contents, config)
            run.add_usage(resp)
            cand = resp.candidates[0] if resp.candidates else None
            parts = list(cand.content.parts) if cand and cand.content and cand.content.parts else []
            run.log("model_turn", turn=turn, model=run.model, tokens=dict(run.tokens),
                    text=clip(" ".join(p.text for p in parts if p.text and not getattr(p, "thought", False)), 600))
            calls = [p.function_call for p in parts if p.function_call]
            if cand and cand.content and parts:
                contents.append(cand.content)
            if not calls:
                nudges += 1
                if nudges > 3:
                    ctx.failures.append("model stopped without calling tools or finish_assessment")
                    break
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Continue using tools. When every requirement has a decision, call finish_assessment.")]))
                continue
            out_parts = []
            for fc in calls:
                name, a = fc.name, dict(fc.args or {})
                if run.tool_calls >= MAX_TOOL_CALLS and name not in ("submit_decision", "finish_assessment"):
                    result = {"error": "tool-call budget exhausted: submit decisions and call finish_assessment now"}
                else:
                    run.tool_calls += 1
                    run.by_tool[name] = run.by_tool.get(name, 0) + 1
                    result = run_tool(ctx, name, a)
                if "error" in result:
                    run.tool_errors += 1
                print(f"  [{turn}] {name} {clip(json.dumps(a, default=str), 110)}"
                      + (f"  !! {clip(result['error'], 80)}" if "error" in result else ""))
                run.log("tool_call", turn=turn, tool=name, args=a,
                        result_chars=len(json.dumps(result, default=str)),
                        result_preview=clip(json.dumps(result, default=str, ensure_ascii=False), 1500))
                out_parts.append(types.Part.from_function_response(name=name, response=result))
            contents.append(types.Content(role="user", parts=out_parts))
            if ctx.finished:
                break
            if run.tool_calls >= MAX_TOOL_CALLS + 20:
                ctx.failures.append(f"tool-call budget of {MAX_TOOL_CALLS} exhausted")
                break
        else:
            ctx.failures.append(f"turn budget of {MAX_TURNS} exhausted")
    except (Exception, KeyboardInterrupt) as e:
        ctx.failures.append(f"model/tool failure: {type(e).__name__}: {clip(str(e), 400)}")
        run.log("fatal", error=str(e))

    if not ctx.finished:
        for s in ctx.target_reqs:
            if s not in ctx.decisions:
                fill_unassessed(ctx, s, "Not assessed: the run ended before this requirement was decided "
                                        "(see Failures).")
    elapsed = time.time() - run.t0
    overall, stats = write_report(ctx, run, outdir, args, elapsed)
    run.log("end", overall=overall, **stats)
    run.trace.close()
    print(f"\nOverall: {overall}  |  {elapsed:.0f}s  |  tokens {stats['tokens']}  |  "
          f"tool calls {run.tool_calls}")
    print(f"Report: {outdir / 'report.md'}")
    return 1 if ctx.failures else 0


if __name__ == "__main__":
    sys.exit(main())