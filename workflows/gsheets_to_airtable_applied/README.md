# Google Sheet -> Airtable sync (applied: Signups -> Candidates, Onboarding -> Onboarding)

Applied version of [`../sheets_to_airtable`](../sheets_to_airtable/README.md), tested on real-shaped data.
Same split of work: n8n does all I/O with its own credentials, python-api decides what to send.

Live server: step by step in [DEPLOY.md](DEPLOY.md).

## Google access: pick one

| Method | Use when | Workflow files | Setup |
|---|---|---|---|
| **Service account** (primary) | A Google Cloud project is available. Sheet and linked files can be shared with the service account | `onboarding.workflow.json`, `signups.workflow.json` | [DEPLOY.md](DEPLOY.md) |
| OAuth2 (fallback) | Files are owned by many people and can't all be shared with a service account | `alternatives/oauth2/` | [alternatives/README.md](alternatives/README.md#oauth2) |
| Apps Script proxy (fallback) | No Google Cloud project at all | `alternatives/apps_script/` + `Code.gs` | [alternatives/README.md](alternatives/README.md#apps-script-proxy) |

Only **Read Sheet**, **Fetch File** and the **Build Upload** code differ between the three. Config fields,
python scripts, Airtable side, failsafes and reports are the same. The fallbacks are separate workflow files:
import the one you need; don't mix nodes between them.

## Files

- `gsheets_to_airtable.py` - python-api script (stdlib only), modes `plan` and `attachments`. Shared by all methods
- `shrink_image.py` - python-api script (`# requires: Pillow`, venv built on first call), only called for oversized images
- `onboarding.workflow.json`, `signups.workflow.json` - service account workflows (same nodes, different Config)
- `alternatives/oauth2/*.workflow.json` - OAuth2 workflows
- `alternatives/apps_script/*.workflow.json`, `alternatives/apps_script/Code.gs` - Apps Script workflows + the proxy script

Repo copies have placeholders: `sheet_url` / `gas_url`, `airtable_base_id`, `airtable_table` = `...REPLACE_ME...`.

## Flow

```
Config -> Read Sheet -> Fetch Schema -> Fetch Airtable --> Plan Upserts (python-api, mode=plan)
Upserts OK? -> Anything to upsert? (and not dry_run) -> Upsert Airtable (PATCH: updates by id, creates with performUpsert)
            -> Plan Attachments (mode=attachments) -> Fetch File -> Build Upload -> Uploadable? -> Too Big?
                 Too Big? true  -> Shrink Image (python-api) -> Apply Shrink -> Shrunk OK? -> Upload Attachment | Skipped
                 Too Big? false -> Upload Attachment (content.airtable.com)
```

| Node | Service account / OAuth2 | Apps Script |
|---|---|---|
| Read Sheet | Google Sheets node, Read Rows, one item per row (+ `row_number`), formatted values, header row from Config | HTTP GET `gas_url?tab=&header=`, one item `{columns, header_row, rows}` |
| Fetch File | Google Drive node, Download (binary) | HTTP GET `gas_url?file=<id>`, base64 JSON |
| Build Upload | reads the binary, converts to base64 | uses the base64 as is |
| Reads as | the service account / the user who connected OAuth2 | the sheet owner |

## Changes from the general version

| Change | Why (found in testing) |
|---|---|
| **Fetch Schema** node -> values converted to the Airtable field type | `x` in a number field or `employee` in a select fails/pollutes a whole batch of 10; dates as `8/7/2026` vs `2026-08-07` looked "changed" on every run |
| `key_match: "name"` | Human names: `Oana tEST` = `OANA Test`, `Lișiță` = `Lisita`, `Sergiu-Valentin` = `Sergiu Valentin`. Similar-but-different names are only listed in `report.possible_matches`, never merged |
| Updates go by record id; only changed fields are sent | Airtable's `performUpsert` matches exact text, so a case/accent difference would create a duplicate |
| `compose` | Signups has First/Last Name, Candidates has one Name field |
| Rules with `"on": "create"` | e.g. `Applicant Status = Applicant` only for new records, never overwrite a status someone changed |
| Bad cells drop the field, not the row | `#REF!`, non-numbers, unknown select options -> `report.field_warnings` |
| Header row option | a leftover row above the real header made blank headers overwrite each other |
| `email_key` toggle | two different people can share a name (`Angela TEST` x2). Off by default: the real Candidates table has no Email field. On + field missing -> warning, name matching, run continues |
| Shrink Image branch (`shrink_image.py`, Pillow) | images over `max_upload_mb` are re-encoded as JPEG / downscaled instead of skipped |
| Attachments can't fail a run | every attachment node continues on error; failed attachment plan -> "Attachments Failed (ignored)"; wrong attachment field -> warning |
| Upsert errors don't stop the run | failed batches -> `report.warnings` of Plan Attachments |
| Retries on Read Sheet / Fetch Schema / Fetch Airtable / Fetch File (3x, 5 s) | Google once answered "Fișierul nu poate fi deschis acum" (transient) and the run went red |
| `dry_run` (default **on**) | first run on a live table plans and reports without writing |
| `location_filter` | only people in geographic Europe (incl. UK), USA, Canada, Australia are wanted |
| `create_only_fields` | two sheets (Signups, Onboarding) feed the same Candidates table; each field needs one owner or they overwrite each other every run |
| Execute Once on Fetch Schema / Fetch Airtable / Plan Upserts | the Google Sheets node outputs one item per row; without it every row would trigger its own fetch |

## Config node

Full field list with defaults: [DEPLOY.md, step 6](DEPLOY.md#6-n8n-config-node-the-only-place-to-edit).
Fields added to the general version:

| Field | Example | Meaning |
|---|---|---|
| `sheet_url` (SA / OAuth2) or `gas_url` (Apps Script) | `https://docs.google.com/spreadsheets/d/<id>/edit` | Source |
| `sheet_tab`, `header_row` | `Signups`, `1` | Google methods: header row number (default 1). Apps Script: `0` = auto (first row with 2+ filled cells) |
| `key_match` | `name` | `exact` (case-insensitive) or `name` (also accents, punctuation, spaces) |
| `email_key`, `email_key_column`, `email_key_field` | `false`, `Email Address`, `Email` | **On/off switch** for matching on email, name as fallback |
| `compose` | `{"Full Name": "{First Name} {Last Name}"}` | Virtual sheet columns, usable in `mapping` |
| `date_order` | `MDY` | How to read `8/7/2026` (`MDY` = Aug 7) for date fields |
| `new_options` | `skip` | Select value not in the field's options: `skip` (warn, field not sent) or `create` |
| `option_aliases` | `{"Type": {"employee": "Volunteer"}}` | Sheet value -> existing option, per field |
| rules `on` | `"create"` | `always` (default), `create`, `update` |
| `max_upload_mb`, `shrink_images` | `5`, `true` | Oversized images -> JPEG re-encode / downscale; other files skipped |
| `dry_run` | `true` | Plan + report only, nothing written or uploaded |
| `location_filter` | `{"field": "Current Location", "regions": ["europe", "north_america", "australia"], "unknown": "keep"}` | Skip rows located outside the allowed regions (see DEPLOY step 6). `{}` = off |
| `create_only_fields` | `["Position"]` | Written only when the record is created; afterwards another source (or a person) owns them |

## Report fields

| Field | Meaning |
|---|---|
| `summary` | `created / updated / unchanged` (plan), `queued / already_attached` (attachments); prefixed `DRY RUN, nothing sent:` in dry runs |
| `skipped` | row not sent: key empty, rule skip, no record and `create_missing=false` |
| `field_warnings` | one cell not sent: `#REF!`, `'x' is not a number`, `'employee' is not an option of 'Type'` |
| `possible_matches` | new record whose name looks like an existing one (word order, extra middle name, 1-2 letter typo). Check by hand |
| `warnings` | duplicate keys in the sheet (last row wins), bad Drive links, failed upsert batches, ignored config parts |

## Test results (homelab VM 3001, n8n 2.41.6, python-api Python 3.12)

Run with the **Apps Script** method (2026-10-06/07). Planner, Airtable side, failsafes and shrink are the same code in all methods.

| Test | Result |
|---|---|
| Onboarding first run: 198 rows | 198 created, 20 batches, 0 errors; 268 field warnings (141 `Type=employee`, WW `Test*` options, 32 `#REF!`, 22 `x` in Availability) |
| Onboarding second run | 198 unchanged, nothing sent |
| Signups first run: 17 rows | 16 created (`Angela TEST` twice -> last wins), 3 Drive images uploaded, 13 placeholder links reported |
| Signups second run | 16 unchanged, 3 already attached |
| Name changed in Airtable to `OANA Test` / `Lisita Daniel Test` | matched, unchanged, no duplicate |
| Position changed in Airtable | updated=1, only that field sent |
| `Name` (long text) as `fieldsToMergeOn` | works |
| `email_key` on, Email field added to Candidates | 16 old records got their Email (found by name), 2nd `Angela TEST` created as its own record |
| `email_key` on, field missing / column misnamed (local) | warning, name matching, run goes on |
| `max_upload_mb=1` (test) | 3 cats 1.07-4.0 MB -> 0.75-0.98 MB JPEG in 1-3 steps, uploaded; Pillow venv built on first call within the timeout |
| Re-run after shrink | `already_attached=4` (the `gd-<id>-` prefix survives the `.jpg` rename) |
| Failure injection: attachment field missing / python-api 404 on attachment plan / Drive file unreadable / shrink script missing | run green each time; upserts unaffected; files skipped with reason, uploaded on the next normal run |
| `dry_run` on, Position drifted in Airtable | `DRY RUN, nothing sent: updated=1`, Airtable unchanged; `dry_run` off -> fixed |

**Service account / OAuth2 (2026-10-08): not run against real data yet.** Smoke test only: the service account
workflow imports and its Google nodes get as far as signing the auth request (dummy key).

Open:
- Full test run with a real service account key: first run, re-run, attachments, Onboarding with `header_row=2`,
  a linked file not shared with the service account (must be skipped, not fail the run).
- How the Google Sheets node names blank / repeated header cells (the Apps Script proxy names them `Column C`, `Name (2)`).
- A real >5 MB Drive file (tested via `max_upload_mb=1` only).
- An attachment removed from the sheet stays in Airtable (by design, as in the general version).
