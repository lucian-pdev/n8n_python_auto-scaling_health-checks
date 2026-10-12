# Google Sheet -> Airtable sync

One-way sync, every 15 min. Each sheet row is matched to an Airtable record on `key_fields` and created or updated (upsert). Google Drive links in the sheet become Airtable attachments, uploaded once per file.

- `sheets_to_airtable.workflow.json` - n8n workflow, import via Workflows -> Import from file
- `sheets_to_airtable.py` - python-api script: matching, rules, diff, attachment dedupe

## Split of work

```
n8n (credentials + I/O)                          python-api (pure logic, no secrets)
Config -> Read Sheet -> Fetch Airtable --rows+records--> mode=plan
                                       <--upsert bodies--
Upserts OK? -> Upsert Airtable (PATCH, performUpsert)
                    --rows+records+responses--> mode=attachments
                    <--attachment jobs--
Attachments OK? -> Download from Drive -> Build Upload -> Upload Attachment (content.airtable.com)
Upserts OK? / Attachments OK? false -> Script Failed (execution turns red)
```

- Not n8n only: Airtable pulls attachments only from public URLs. Private Drive files must be downloaded and pushed via `uploadAttachment`, which the Airtable node can't do. Match/rule/dedupe logic is clumsy in IF nodes.
- Not Python only: Google + Airtable tokens would live in python-api, and its 60s job timeout doesn't fit a loop of uploads.

## Setup

1. Deploy the script to the `scripts` branch (github_sync runs `git reset --hard` on `scripts/`, local copies get wiped):
   ```bash
   git worktree add ../scripts-branch scripts
   cp workflows/sheets_to_airtable/sheets_to_airtable.py ../scripts-branch/
   cd ../scripts-branch && git add sheets_to_airtable.py && git commit -m "add sheets_to_airtable" && git push
   # after SYNC_INTERVAL_MINUTES, on the VM:
   curl -s http://127.0.0.1:8000/scripts      # lists sheets_to_airtable.py
   ```
2. Credentials in n8n (Credentials -> Add):
   - Airtable Personal Access Token - airtable.com/create/tokens, scopes `data.records:read`, `data.records:write`, access to the base.
   - Google Sheets OAuth2 + Google Drive OAuth2 - Google Cloud project with Sheets and Drive APIs enabled, redirect URL `https://<DOMAIN>/rest/oauth2-credential/callback`.
3. Import the workflow, pick the credential on: Read Sheet, Fetch Airtable, Upsert Airtable, Download from Drive, Upload Attachment.
4. Edit the Config node, Run manually, read `summary` / `report` in the output of Plan Upserts and Plan Attachments, then Activate.

## Config node

| Field | Example | Meaning |
|---|---|---|
| `sheet_url`, `sheet_name` | `https://docs.google.com/...`, `Sheet1` | Source; row 1 = header row |
| `airtable_base_id`, `airtable_table` | `app...`, `People` (or `tbl...`) | Target |
| `key_fields` | `["Email"]` | "If X == X": fields that identify a record. Must be mapping targets, plain text/number/email |
| `mapping` | `{"Full name": "Name", "Email": "Email"}` | Sheet column -> Airtable field, exact spelling |
| `attachments` | `{"Photo": "Photo"}` | Sheet column with Drive link(s) -> attachment field |
| `rules` | see below | "Then Y" |
| `create_missing` | `true` | `false` = only update existing records |
| `skip_empty` | `true` | Blank cells never clear Airtable values |
| `typecast` | `true` | Airtable converts text to select options / numbers / linked records |

### Rules

Checked top to bottom on every row, after mapping. All conditions in `when` must be true (AND). `set` writes Airtable fields, `skip` drops the row.

```json
[
  {"when": {"column": "Stage", "op": "equals", "value": "Hired"},
   "set":  {"Status": "Onboarding"}},
  {"when": [{"column": "Stage", "op": "in", "value": ["Rejected", "Withdrawn"]},
            {"column": "Notes", "op": "empty"}],
   "set":  {"Status": "Closed"}},
  {"when": {"column": "Stage", "op": "equals", "value": "Archived", "source": "airtable"},
   "skip": true}
]
```
- `column` - sheet column, or Airtable field with `"source": "airtable"` (value currently in Airtable)
- `op` - `equals`, `not_equals`, `contains` (case-insensitive), `in`, `empty`, `not_empty`. Unknown op = run fails before anything is sent.

## Sheet rules of thumb

- Drive links as plain URLs: `https://drive.google.com/file/d/<id>/view`, `open?id=<id>` or bare file ID. Several per cell, separated by commas/new lines.
- Smart chips and `=HYPERLINK()` return only display text via the Sheets API, so the file ID is lost -> "not a Drive file link" warning.
- Files only, no folders. Max 5 MB per file (Airtable limit).
- The n8n Google account needs Viewer on the sheet and every linked file.

## Behaviour

- Unchanged rows send nothing. Changed/new rows go out 10 per request, max 4 requests/s.
- Uploads are named `gd-<driveFileId>-<original name>`; the prefix tells later runs the file is already attached. Removing a link from the sheet does not remove the attachment.
- Duplicate keys in the sheet: last row wins, warning in the report.
- Failed downloads/uploads don't stop the run: see Skipped Attachments and the error output of Upload Attachment. They retry next run.

## Errors

python-api answers HTTP 200 even when the script fails; the outcome is in `result.status`.

| `result.status` | Meaning | Where it shows |
|---|---|---|
| `success` | `result.result` = `{mode, summary, upserts/attachments, report}` | node output |
| `error` | script raised; `result.error` = `"<mode>: <Type>: <msg> (line N in func)"` | Script Failed -> red execution, message in Executions list |
| `timeout` | script ran > 60s | Script Failed |
| `venv_error` | venv for the script's `requires` couldn't be built | Script Failed |
| `retrying` (HTTP 202) | python-api worker died, job requeued | Script Failed; next scheduled run retries |

How the script makes errors readable:
- `@reports_errors` - wraps each mode; re-raises any exception as one `ScriptError` line prefixed with the mode (`plan: KeyError: ... (line N in func)`). python-api's wrapper already adds type + line for any script; the decorator adds which mode failed.
- Full traceback: Plan Upserts / Plan Attachments output -> `result.traceback`.
- `@mode("plan")` - registers the function; `run()` dispatches `data["mode"]` through the registry, unknown mode -> error listing valid ones.
- `_config()` - validates JSON fields, `key_fields` vs `mapping`, rule ops, before any work.
- Row-level problems (empty key, bad link, duplicate) are not errors: they go to `report.skipped` / `report.warnings`, counted in `summary`.

## Local test

```bash
python3 sheets_to_airtable.py payload.json
# payload.json = {"data": {"mode": "plan", "config": {...}, "rows": [...], "records": [...]}}
```
- Prints the same `{status, result | error}` shape python-api returns; exit code 1 on error.
- Ready payload: in n8n open Plan Upserts -> input, copy the JSON body.

## Origin

Ideas carried over from `blocked-checker/airtable-upload_v3.py` (not called directly; it uploads local `proof/` screenshots): 5 MB size check, skip if already attached, continue past single failures, progress journal (here: `report` + n8n execution log).
