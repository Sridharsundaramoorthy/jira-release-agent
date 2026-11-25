# Jira Release Evidence Agent

A tool-using AI agent that decides whether the requirements of a software release are satisfied by evidence recorded in Jira. It has two parts:

1. **`importer.py`** loads `inputs/dataset.json` (400 tickets, 2,059 comments, 76 links) into a Jira Cloud project. It is repeatable and safe to re-run.
2. **`agent.py`** investigates the imported records through Jira tools (Gemini function calling) and writes one release-readiness report.

The agent never reads `dataset.json`. It only sees what its tools fetch from Jira, limited to the dataset label.

## Files

| File | Purpose |
|---|---|
| `importer.py` | Idempotent import into Jira (issues, comments, links, statuses) |
| `agent.py` | The agent: tools, bounded loop, decisions, report |
| `list_models.py` | Lists Gemini models your key can call |
| `requirements.txt` | Pinned dependencies |
| `inputs/dataset.json` | The supplied dataset (import input only) |
| `.env` | Your credentials (never commit this) |
| `runs/<timestamp>-<release>/` | Generated `report.md`, `report.json`, `trace.jsonl` |

## 1. Setup

Requires Python 3.10+.

```
python -m venv .venv
# Windows:   .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a dedicated Jira Cloud project (Scrum or Kanban, with a **Task** issue type) and an API token at https://id.atlassian.com/manage-profile/security/api-tokens. Get a Gemini API key from https://aistudio.google.com/apikey.

## 2. Credentials

Copy `.env.example` to `.env` (`copy` on Windows, `cp` on Mac/Linux) and fill it in:

```
JIRA_BASE_URL=https://yourname.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your_token
JIRA_PROJECT_KEY=YELLOW
GEMINI_API_KEY=your_gemini_key

# Optional tuning
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash-lite
AGENT_MIN_INTERVAL=6
AGENT_KEEP_FULL_RESULTS=4
AGENT_MAX_TURNS=200
AGENT_MAX_TOOL_CALLS=400
AGENT_MAX_SECONDS=2700
```

- `JIRA_PROJECT_KEY` is the prefix on your ticket IDs (for example `YELLOW` for `YELLOW-1`).
- `JIRA_BASE_URL` is only the `https://xxx.atlassian.net` part.
- No quotes and no spaces around `=`.
- Run `python list_models.py` to see which Gemini models your key can use.

## 3. Insert the data into Jira

Place the dataset at `inputs/dataset.json`, then:

```
python importer.py --check      # tests login, project, issue types, link types
python importer.py --dry-run    # shows what would be created, changes nothing
python importer.py              # imports (about 8-20 minutes)
```

- Expected summary: `Issues in Jira: 400 / 400`, `Comments in Jira: 2059 / 2059`, 76 links, `No failures.`
- If it stops or reports failures, run `python importer.py` again. It detects what already exists and only does the missing work, with no duplicates.
- `network error ... retry` lines are normal. The importer retries automatically. If they become constant, set `IMPORT_WORKERS=2` in `.env`.

What the importer does:
- One Jira Task per record, with summary `[SOURCE-ID] original summary`.
- Labels on every issue: the dataset label `tyn-release-assessment-v2`, a kind label (`rr-requirement`, `rr-evidence`, `rr-work`, `rr-manifest`) and a release label (`release-R2.4`).
- The description keeps the source id, recorded release, recorded status and original text.
- Comments are posted with their original text unchanged. Each starts with `Recorded event <timestamp> | actor <name> | role <role>`, so the scenario actor and time are preserved in the text. Jira's native author and created time belong to the importer, not to the fictional actors.
- Dataset links become Jira issue links, and each Jira status is moved to the recorded status (context only, never proof).

Check in Jira with this JQL, which should return 400 issues:

```
labels = "tyn-release-assessment-v2"
```

## 4. Run the agent

```
python agent.py --release R2.4
```

Options:

```
python agent.py --project YELLOW --label tyn-release-assessment-v2 --release R2.4 --out runs
```

| Argument | Default | Meaning |
|---|---|---|
| `--project` | `JIRA_PROJECT_KEY` | Jira project key |
| `--label` | `tyn-release-assessment-v2` | Dataset scope: the label applied by the importer |
| `--release` | required | Target release, for example `R2.4` (label `release-R2.4`) |
| `--out` | `runs` | Output folder |

The console prints each tool call as it happens. A run takes roughly 5-20 minutes depending on model and rate limits. At the end it prints the overall decision, elapsed time, token usage and the report path.

## 5. Outputs

Each run writes to `runs/<timestamp>-<release>/`:

- **`report.md`**: overall decision, requirement table, and per requirement the decision, explanation, evidence links (with the actor, role and timestamp from the recorded event text), outstanding verification, and run statistics.
- **`report.json`**: the same data in machine-readable form.
- **`trace.jsonl`**: the investigation trace, one line per model turn and tool call (arguments, result size and preview, token counts, retries and model fallbacks).

## How it works

**Tools the model can call** (all query Jira live, limited to the dataset label):

| Tool | Use |
|---|---|
| `search_issues` | Find issues by kind, release, status or keyword |
| `get_issue` | Read a description and its links |
| `get_comments` | Read comments in order, as refs like `REQ-02#c3` |
| `grep_comments` | Regex search within one issue's comments |
| `submit_decision` | Record PASS, FAIL or INSUFFICIENT_EVIDENCE for one requirement |
| `finish_assessment` | End the run (asks once if requirements are still undecided) |

**Decision rules:**
- PASS: applicable evidence establishes the acceptance criterion.
- FAIL: applicable evidence establishes a failure or violation.
- INSUFFICIENT_EVIDENCE: evidence is missing, inapplicable or inconclusive. Missing proof is not a failure.
- Overall (computed in code): BLOCKED if any mandatory requirement fails; NEEDS_REVIEW if none fails but at least one mandatory requirement has insufficient evidence; READY only if every mandatory requirement passes.

**Safeguards:**
- A decision is rejected if it cites a ticket or comment the agent did not read in that run, and PASS or FAIL need at least one such citation. This prevents invented evidence.
- Ticket status is context only. The prompt requires evidence to match the release, commit, service, environment and prompt version.
- Instructions found inside ticket or comment text are treated as data, not commands.
- The loop is bounded by turns, tool calls and wall-clock time. Older tool results are trimmed to save context.
- Model errors (429, 5xx) are retried with backoff, with one fallback to a second model. Anything that goes wrong is listed under Failures in the report. Undecided requirements are marked as not assessed, and an incomplete run can never report READY.

## Token usage and cost

Gemini has no memory between calls, so each turn resends the conversation. To keep runs cheap:
- Prefer a Flash or Flash-Lite model.
- Keep `AGENT_KEEP_FULL_RESULTS` low (4-6).
- Keep the system prompt's batching instruction ("issue independent tool calls together") so the run takes fewer turns.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing environment variable ...` | `.env` is missing or not in the project folder |
| `Project OK` fails with 404 | Wrong `JIRA_PROJECT_KEY`, or the account has no access |
| `0 requirements labelled for ...` | The release value does not match the `release-<value>` label |
| `429` model errors | Free-tier rate limit; raise `AGENT_MIN_INTERVAL` or switch model |
| `503` model errors | Model overloaded; the script retries automatically |
| Run ends with "budget exhausted" | Raise `AGENT_MAX_TURNS` and `AGENT_MAX_SECONDS` in `.env` |

## Security

- Keep `.env` out of Git. Add `.env`, `.venv/` and `runs/` to `.gitignore` if you do not want to submit generated runs.
- If a token is ever pasted into chat or committed, revoke it and create a new one.

## Known limits

- Decision quality depends on the model and prompt. Review the trace for any decision you want to verify.
- The agent calls tools sequentially, so a full run takes many turns and is slowed by free-tier rate limits.
- The agent does not resume. Re-running starts a fresh investigation.
- The release label naming (`release-<value>`) and kind labels (`rr-*`) come from this importer.

## AI assistance

AI coding assistance (Claude) was used to design and write the importer and agent. The code was run and checked against the Jira project and the generated report.