# Deploy on a live naypyers server

Step-by-step for adding the Sheet -> Airtable sync to a running naypyers stack (n8n + python-api),
e.g. a VPS or Proxmox VM. Nothing on the server is rebuilt or restarted. All per-sync settings are made in n8n;
the Python scripts are never edited.

This guide uses the **service account** method (primary). For OAuth2 or the Apps Script proxy, replace
step 2 and the Google part of step 5 with [alternatives/README.md](alternatives/README.md); everything else is the same.

Time: ~30 min. You need:
- a Google Cloud project you can create a service account in,
- the Google account that owns the real sheet and the linked Drive files (to share them),
- an Airtable account with access to the real base,
- n8n UI login on the live server,
- push rights to the GitHub repo + branch that the server's `github_sync` pulls scripts from.

Each step ends with a **check**. Don't go on until it passes.

---

## 1. Check the server

On the server, in the project directory:
```bash
docker compose ps python-api n8n-main            # both "Up", python-api "(healthy)"
curl -s http://127.0.0.1:8000/scripts             # {"scripts":[...]}
systemctl list-timers github_sync.timer           # has a NEXT time
grep -E '^Environment="SCRIPTS_(REPO|BRANCH)' /etc/systemd/system/github_sync.service
```
**Check:** note `SCRIPTS_REPO` and `SCRIPTS_BRANCH` (normally the GitHub repo, branch `scripts`). Step 3 pushes there.
python-api needs outbound internet once, to `pip install Pillow` for `shrink_image.py`.

## 2. Google side: service account

1. [console.cloud.google.com](https://console.cloud.google.com) -> pick or create a project.
   APIs & Services -> Library -> enable **Google Sheets API** and **Google Drive API**.
2. IAM & Admin -> Service Accounts -> **Create service account** (name e.g. `n8n-sync`, no roles needed:
   access comes from sharing). Open it -> Keys -> Add key -> Create new key -> **JSON**. The file is the credential:
   keep it out of git, delete it after step 5.
3. Copy `client_email` from the JSON (`n8n-sync@<project>.iam.gserviceaccount.com`).
4. Share with that address as **Viewer** (untick "Notify"; the address has no mailbox):
   - the **sheet**,
   - the **Drive folder(s)** holding the linked files, or each file. Files set to "Anyone with the link" also work,
     except some shared that way before Google's 2021 link-sharing update (they need a resource key): share those directly.
     A linked file the service account can't open is skipped with a reason; it never fails the run.

| Problem | Seen later as |
|---|---|
| API not enabled | `403 ... has not been used in project ... or it is disabled` on Read Sheet / Fetch File |
| Sheet not shared | `403 The caller does not have permission` on Read Sheet |
| File not shared | Skipped Attachments: `Drive download failed: ... File not found` |
| Wrong sheet URL / tab | `Requested entity was not found` / `Unable to parse range: <tab>` |

The check for this step is in step 5 (Execute step on Read Sheet).

## 3. Scripts to the server

Put both files on the branch from step 1 (here the `scripts` branch of the GitHub repo):
```bash
# in a clone that can push to that repo
git worktree add ../scripts-branch scripts
cp workflows/gsheets_to_airtable_applied/{gsheets_to_airtable.py,shrink_image.py} ../scripts-branch/
cd ../scripts-branch && git add gsheets_to_airtable.py shrink_image.py \
  && git commit -m "add gsheets_to_airtable + shrink_image" && git push
```
Never copy them into the server's `scripts/` by hand: `github_sync` resets that folder.

**Check** (server, after `SYNC_INTERVAL_MINUTES`):
```bash
curl -s http://127.0.0.1:8000/scripts           # lists gsheets_to_airtable.py and shrink_image.py
```

## 4. Airtable token

airtable.com/create/tokens -> Create token:
- Scopes: `data.records:read`, `data.records:write`, `schema.bases:read`
- Access: the **real** base only

**Check:**
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <PAT>" \
  https://api.airtable.com/v0/meta/bases/<appBASE>/tables      # 200
```
Base and table IDs are in the table's URL: `https://airtable.com/<appBASE>/<tblTABLE>/<viwVIEW>`.

## 5. n8n: credentials + import

Credentials -> Add credential:
| Type | Name (suggested) | Fields |
|---|---|---|
| Airtable Personal Access Token API | Airtable PAT (gsheets_to_airtable) | Access Token = PAT from step 4 |
| Google Service Account API | Google service account (gsheets_to_airtable) | Service Account Email = `client_email`; Private Key = `private_key` from the JSON, pasted as is (with `-----BEGIN PRIVATE KEY-----`); Impersonate a User: off |

Then delete the downloaded JSON key file.

Workflows -> Import from File: `onboarding.workflow.json`, then `signups.workflow.json`.
In each, open these nodes and pick the credential (n8n marks them red until you do):
- Google service account: **Read Sheet**, **Fetch File**
- Airtable: **Fetch Schema**, **Fetch Airtable**, **Upsert Airtable**, **Upload Attachment**

**Check:** set `sheet_url` / `sheet_tab` / `header_row` in Config (step 6) first, then open **Read Sheet** ->
**Execute step**: one item per sheet row, keys = the header names, plus `row_number`. Errors: table in step 2.

## 6. n8n: Config node (the only place to edit)

Open **Config** in each workflow. Everything the sync does is set here; values are kept per workflow.

Must set:
| Field | Set to |
|---|---|
| `sheet_url` | the sheet's browser URL (`https://docs.google.com/spreadsheets/d/<id>/edit`) |
| `airtable_base_id` | `app...` of the real base |
| `airtable_table` | `tbl...` (or exact table name) of the real table |
| `dry_run` | leave **true** for the first run (step 7) |

Check against the real table (same names as the test base unless it was changed):
| Field | Onboarding workflow | Signups workflow |
|---|---|---|
| `sheet_tab` | `Onboarding` | `Signups` |
| `header_row` | `2` while row 1 has leftovers, else `1` | `1` |
| `key_fields` | `["Name"]` | `["Name"]` |
| `key_match` | `name` | `name` |
| `compose` | `{}` | `{"Full Name": "{First Name} {Last Name}"}` |
| `mapping` | sheet column -> Airtable field, all 21 columns except `Reporting to` | `Full Name`->`Name`, `Position Applying For`->`Position`, picture column -> `Photo link` |
| `attachments` | `{}` | picture column -> `Photo` |
| `rules` | `[]` | new records get `Applicant Status = Applicant` |
| `email_key` | `false` (no email column) | **`false`**: the real Candidates table has no Email field. Turn on only after adding one |

All fields:
| Field | Type | Default | What it does |
|---|---|---|---|
| `sheet_url` | text | - | Sheet URL (Apps Script method: `gas_url` = the `/exec` URL instead) |
| `sheet_tab` | text | - | Tab name |
| `header_row` | number | `1` | Header row number; data starts on the next row. (Apps Script method: `0` = auto-detect) |
| `airtable_base_id`, `airtable_table` | text | - | Target |
| `key_fields` | JSON list | - | Airtable field(s) that identify a record; must be `mapping` targets |
| `key_match` | text | `exact` | `name`: ignore case, accents, punctuation, extra spaces. `exact`: ignore case only |
| `compose` | JSON object | `{}` | New sheet columns from others: `{"Full Name": "{First Name} {Last Name}"}` |
| `mapping` | JSON object | - | Sheet column -> Airtable field, exact spelling. Unmapped columns are ignored |
| `attachments` | JSON object | `{}` | Sheet column with Drive links -> attachment field. Wrong field = warning, rest of the sync runs |
| `rules` | JSON list | `[]` | `{"when": {...}, "set": {...}}` / `"skip": true`; `"on": "create"\|"update"\|"always"`. Syntax: [general README](../sheets_to_airtable/README.md#rules) |
| `option_aliases` | JSON object | `{}` | Sheet value -> existing select option: `{"Type": {"employee": "Volunteer"}}` |
| `email_key` | on/off | `false` | Match on email instead of name; name stays the fallback. Missing field/column = warning, name matching |
| `email_key_column`, `email_key_field` | text | `Email Address`, `Email` | Sheet column / Airtable field for `email_key` |
| `date_order` | text | `MDY` | How `8/7/2026` is read for date fields (`MDY` = Aug 7, `DMY` = 8 Jul) |
| `new_options` | text | `skip` | Select value that isn't an option: `skip` (warn, cell not sent) or `create` (adds the option) |
| `dry_run` | on/off | `true` | Plan and report only; nothing written or uploaded |
| `location_filter` | JSON object | `{}` (off) | `{"field": "Current Location", "regions": [...], "unknown": "keep"}`. Location = the sheet value for `field` if mapped, else the Airtable record's. Allowed if any place in it belongs to `regions`; skipped if it only names other places; blank / unrecognised -> `unknown` (`keep` or `skip`). Regions: `europe` (geographic, incl. UK, Norway, Switzerland, Balkans, Moldova, Ukraine), `north_america` (USA + Canada, incl. states/provinces), `australia`, and off by default: `mexico`, `central_america_caribbean`, `transcontinental` (Turkey, Russia, Caucasus), `new_zealand`. Country names in English and Romanian; cities mostly not recognised (they count as unknown) |
| `create_only_fields` | JSON list | `[]` | Fields only written when the record is created, never updated |
| `create_missing` | on/off | `true` | `false` = only update existing records |
| `skip_empty` | on/off | `true` | Blank cells never clear Airtable values |
| `typecast` | on/off | `true` | Airtable converts text to numbers / options / links |
| `max_upload_mb` | number | `5` | Airtable's limit. Bigger images are shrunk, other files skipped |
| `shrink_images` | on/off | `true` | Shrink oversized images in python-api (JPEG, lower quality / size) |
| `python_api_url`, `script`, `shrink_script` | text | `http://python-api:8000/execute`, file names | Leave as is |

Other settings, outside Config:
- Schedule: **Every 15 min** node.
- Airtable rate: **Upsert Airtable** / **Upload Attachment** -> Options -> Batching (4 requests / 1.1 s; Airtable allows 5/s).
- Not needed for normal use, set in the scripts: Airtable batch size 10, which cell texts count as checkbox true/false, sheet error codes (`#REF!`...), shrink quality steps.

## 7. First run: dry run

With `dry_run = true`: open the workflow -> **Execute workflow**. Then open **Plan Upserts** -> output:
- `result.status` = `success`
- `result.result.summary`, e.g. `DRY RUN, nothing sent: created=198 updated=0 unchanged=0 ... field_warnings=268`
- `result.result.report`:
  - `field_warnings`: cells that won't be sent (`'employee' is not an option of 'Type'`, `#REF!`, `'x' is not a number`).
    Fix in the sheet, or add `option_aliases`, or accept.
  - `possible_matches`: new names that look like existing records. Check by hand; fix the spelling in the sheet if it's the same person.
  - `warnings`: duplicate keys, bad Drive links, config problems.
  - `skipped`: rows not sent and why.
- `result.result.upserts`: the exact Airtable request bodies.

Also read **Plan Attachments** (`dry_run_not_uploaded` = files it would upload).

**Check:** `created` is what you expect. If the table already has these people and `created` is close to the
row count, the key doesn't match: fix `key_fields` / `mapping` / `key_match` before going live.

## 8. Go live

1. Config -> `dry_run` = **false** -> Save -> **Execute workflow** once.
   - Plan Upserts summary without `DRY RUN`; Upsert Airtable items have `records`.
   - Spot-check a few records in Airtable.
2. **Execute workflow** again: expect `created=0 updated=0 unchanged=<all>`, `already_attached=<n>`.
   Anything else means a field keeps "changing": look at `upserts` for which one.
3. Toggle the workflow **Active**. It now runs every 15 min.

## 9. Day-to-day

- Executions list: red = Read Sheet, Fetch Schema, Fetch Airtable or Plan Upserts failed (nothing was written).
  The message says which: `python-api error: plan: no such Airtable field(s) ['Pozition']; close: {...}`.
- Attachment problems never turn a run red: see **Skipped Attachments** and **Attachments Failed (ignored)**.
  Skipped files are retried every run.
- Changing mappings: Config only. Run once with `dry_run = true` after every change.
- New column in the sheet: add it to `mapping` (and the field in Airtable), dry run, go.

| Symptom | Cause / fix |
|---|---|
| Read Sheet red: `403` / `not found` / `Unable to parse range` | Step 2 table: API enabled, sheet shared with the service account, `sheet_url`, `sheet_tab`. Transient Google errors are retried 3x first |
| Columns shifted / named after the wrong row | `header_row` |
| `schema missing` | PAT lacks `schema.bases:read`, or `airtable_table` wrong |
| `key_fields [...] must be targets in mapping` | Add the key field to `mapping` |
| `read-only (computed) Airtable field(s)` | Formula/rollup/AI fields can't be written: remove from `mapping` |
| Same rows "updated" every run | A field Airtable stores differently (rich text, lookups). See `upserts` in Plan Upserts; remove or fix the mapping |
| Photos never upload | Drive link format (`/file/d/<id>`, `open?id=<id>`); the file must be shared with the service account (step 2.4) |
| Shrink fails with `venv_error` | python-api couldn't install Pillow (no internet / disk). Set `shrink_images` off |

## Rollback

Deactivate the workflow. Records it created have `createdTime` after the go-live time. Records it updated
keep the sheet values (Airtable record history shows the old ones). The workflow never deletes records or attachments.
