# Setup (run on your own machine)

## 0. Revoke the old Jira token
The token you pasted in chat is exposed. Revoke it at
https://id.atlassian.com/manage-profile/security/api-tokens and create a new one.

## 1. Python environment (Python 3.10+)
    python -m venv .venv
    # Windows:  .venv\Scripts\activate
    # Mac/Linux: source .venv/bin/activate
    pip install -r requirements.txt

## 2. Credentials
    copy .env.example .env      (Mac/Linux: cp .env.example .env)
Edit `.env`: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY,
GEMINI_API_KEY. The project key is the prefix on your ticket IDs (e.g. YS-1).

## 3. Test the Jira connection
    python importer.py --check
Expect: "Auth OK", "Project OK", and your issue types / link types.
If it warns that "Task" or "Relates" is missing, set JIRA_ISSUE_TYPE /
JIRA_LINK_TYPE in `.env` to a name from the printed list.

## 4. Preview, then import
    python importer.py --dry-run
    python importer.py
About 400 issues, 2,059 comments and 76 links. Expect roughly 10-20 minutes.
If it stops or reports failures, just run it again; it resumes without duplicates.
Final summary should show Issues 400/400 and Comments 2059/2059.

## 5. Check Jira
Open your project and search: labels = "tyn-release-assessment-v2"
You should see 400 issues. Open REQ-02 and confirm it has linked issues and
comments that begin with "Recorded event ... | actor ... | role ...".

## 6. Find your Gemini model
    python list_models.py
Send me the output (model names only, not your key).
