# Signup spam filter (signups workflow)

Sits between **Fetch Airtable** and **Plan Upserts**. Rows that fail never reach Airtable. n8n does all I/O
(Google Sheets, Data Table, Slack); `spam_score.py` (python-api) decides. Everything tunable is in the **Config** node,
field `spam_filter` plus a few helper fields. Only the Signups workflow has it so far. No AI: a person decides the
unclear rows.

```
Fetch Airtable -> Read Known Spam (list 1) -> Read Spam Table (list 2) -> Score Rows -> Score OK?
Score OK? -> Split Table Writes   -> Upsert Spam Table                       (side branch, runs first)
          -> Split Slack Messages -> Send Slack Message (sub-workflow, not waited for)
          -> Plan Upserts (only the rows that passed)                         (+ Plan Attachments, same rows)
```
The whole sheet is read and scored in one call (each row is judged on its own; the call is shared so a contact repeated
within the batch is noticed). Each Slack message then runs in its own execution of the **Spam Slack** sub-workflow
([`spam_slack.workflow.json`](spam_slack.workflow.json)), so a card waiting for its click never blocks the 15-minute sync.
A failing `Score OK?` goes to **Script Failed** like the rest of the workflow. Failing closed on purpose: a broken filter
must not let everything through. Set `spam_filter.enabled` to `false` to bypass it.

## The two lists
| | List 1: known-spam sheet tab | List 2: n8n data table |
|---|---|---|
| Our access | **read only**, never edited | read + write |
| Searched | first | second |
| Matched on | email, phone (as E.164) | `match_key`, email, phone |
| Verdict | column `Human Verdict`: `spam`, `spam_banned`, `spam_delete_requested`, `not_spam` | column `status`: `pending`, `spam`, `not_spam` |

Per row, before scoring: any list says spam -> **dropped**; else a list says not_spam -> **passes, not scored**;
else list 2 says pending -> **held** (a Slack card is already out). Spam beats not_spam; list 1 beats list 2.
A list-1 row scored `spam` by its own rules but with no verdict yet is not a hit: it only deducts `list1_unreviewed_spam`.

List 2 is created once from [`known_spam.csv`](known_spam.csv) (header only): n8n -> Data tables -> Create -> Import CSV.
A header-only import makes every column text; the script writes strings. Put the table's id in Config `spam_table_id`.

## Score and routes
Every row starts at `start` (100). Each failed check deducts its `points`. The score decides:

| Score | What happens |
|---|---|
| `>= pass_at` (75) | passes to Airtable, nothing stored |
| `review_at <= score < pass_at` (50-74) | stored as `pending`, **review card** in Slack, row held until a person answers |
| `< review_at` (below 50) | stored as `spam`, **one-line note** in Slack, row dropped |

Later runs are silent for rows already in a list: `spam` is dropped, `pending` is held, `not_spam` passes, with no new
message. Rows found in list 1 are never copied into list 2. In a **dry run** nothing is stored or sent; the report says
how many table writes and messages there would have been.

## Config fields
| Field | Meaning |
|---|---|
| `spam_filter.enabled` | `false` = whole filter off |
| `spam_filter.start`, `pass_at`, `review_at` | starting points and the two thresholds (`review_at < pass_at <= start`) |
| `spam_filter.review_title` | first line of the review card (bold) |
| `spam_filter.blocked_text` | the one-line note; `{score}` and `{name}` are filled in |
| `spam_filter.points` | points deducted per check; `0` switches a check off (table below) |
| `spam_filter.columns` | headers of the signup sheet: `first_name`, `last_name`, `email`, `phone`, `location_1`, `location_2`. **Set these to the real headers** |
| `spam_filter.list1_columns` | headers of the known-spam tab: `email`, `phone`, `country`, `verdict`, `zone` |
| `spam_filter.list1_spam_verdicts`, `list1_ok_verdicts` | which `Human Verdict` values mean spam / not spam |
| `spam_filter.allowed_continents`, `allowed_countries`, `denied_countries` | zones. Denied beats allowed; anything not allowed counts as denied. Continents are geonames codes `EU AS AF NA SA OC` |
| `spam_filter.phone_default_region` | ISO-2 used to read numbers typed without `+country code` (optional) |
| `spam_filter.name_email_min_overlap` | below this share of the name's letters in the email: small deduction (0.2) |
| `spam_filter.test_names` | `{"enabled": false, "pattern": "..."}`; on = names matching the pattern are dropped, nothing stored |
| `spam_filter.spam_keywords`, `max_field_length` | keyword list (location fields only) and the longest plausible field |
| `spam_list1_sheet_url`, `spam_list1_tab`, `spam_list1_header_row` | where list 1 is. The Google credential needs read access to that doc |
| `spam_table_id` | id of the data table (list 2) |
| `spam_slack_channel` | Slack channel id for the review cards and the notes |
| `spam_review_mention_ids` | list of Slack ids tagged on every review card: `U...` = a person, `S...` = a user group. Add or remove ids here; `[]` = no tags |
| `spam_slack_workflow_id` | id of the imported **Spam Slack** sub-workflow (from its URL) |
| `spam_script` | script name in python-api (`spam_score.py`) |

You can give a **partial** `spam_filter`: keys you leave out keep the defaults, nested objects are merged key by key.

## Checks (default points)
| Group | Check | Points |
|---|---|---|
| Geography | `phone_region_denied`: phone number belongs to a denied country | 25 |
| | `place_denied`: the place fields name only denied countries (one deduction for both fields) | 25 |
| | `geo_both_denied`: phone and place both denied | 10 |
| | `geo_phone_place_mismatch`: phone country is not among the place countries | 5 |
| | `place_unresolved`: text given, no country recognised / `place_blank`: both empty | 5 / 10 |
| Phone | `phone_missing` / `phone_unparseable` / `phone_not_possible` / `phone_not_valid` | 10 / 25 / 25 / 15 |
| | `phone_no_country` (no `+` and no region to try) / `phone_repeated_digits` | 5 / 15 |
| Email | `email_missing` / `email_bad_syntax` / `email_disposable` / `email_random_local` | 15 / 30 / 30 / 10 |
| | `name_email_low_overlap` | 3 |
| Name | `name_missing` / `name_bad_chars` / `name_initials` / `name_same` / `name_gibberish` | 15 / 25 / 10 / 10 / 10 |
| Content | `link_or_keyword` / `field_too_long` | 20 / 15 |
| Batch and lists | `duplicate_in_batch` (same email or phone as an earlier row of this run) / `list1_unreviewed_spam` | 15 / 10 |

How places are read: country and state/province names are found anywhere in the text; a city counts only when it is a whole
comma-separated part (`Nice` yes, `somewhere nice` no). If **any** named place is allowed, the place is allowed
(`Atlanta, Georgia` is the US state). Phone countries come from `phonenumbers`. Non-Latin and accented names are never penalised.
Defaults: allowed = Europe + US, CA, AU, KR, CN, JP; denied = everything else, plus RU, TR, IL, MX, NZ.

## Slack messages (sub-workflow `Spam Slack`)
- **Review card** (50-74): every line bold: the title, the tags from `spam_review_mention_ids`, then
  `Volunteer name: ... Email: ... Phone: ...`, with two buttons, **Spam** and **Not spam** (Slack "send and wait").
  Spam -> `status` = `spam`; Not spam -> `not_spam` (the row passes on the next run). No answer in 14 days -> `not_spam`.
  The buttons open n8n's public URL, so n8n must be reachable at its `WEBHOOK_URL`.
- **One-line note** (below 50): `Spam in Gsheets 2 Airtable, score <score>, Name <name>, blocked.`
- Text typed in the form can't format or mention in Slack (`<`, `>`, `&`, `*` are neutralised).

## Setting it up
1. Data table: n8n -> Data tables -> Create -> Import CSV -> [`known_spam.csv`](known_spam.csv). Its id -> `spam_table_id`.
2. Import [`spam_slack.workflow.json`](spam_slack.workflow.json), link the Slack credential on `Review Card` and
   `Blocked Note`, save. Its id (from the URL) -> `spam_slack_workflow_id`.
3. In Signups' Config: `spam_slack_channel`, `spam_review_mention_ids`, the list-1 sheet fields. Link the Google
   credential on `Read Known Spam`.
4. First run with `dry_run: true`: the report shows what would be stored and sent.

## Tests
`python -m pytest tests/` in this folder, in a venv with `phonenumbers geonamescache disposable-email-domains pytest`
(same versions as the `# requires:` line of `spam_score.py`). The disposable-domain list is pinned: bump the version now and then.

## Not covered yet
- Tuning on real rows: the default points are a starting point.
- Signups workflow only; Onboarding and `alternatives/` are unchanged.
- Not imported into n8n yet: the Execute Workflow, Slack send-and-wait and Data Table update node parameters follow
  n8n 2.x. If a node shows a warning after import, re-select the workflow / channel / table there.

- Tuning on real rows: the default points are a starting point.
- Signups workflow only; Onboarding and `alternatives/` are unchanged.
- Not run inside n8n yet (no import test of the new nodes). The Data Table and Slack node parameters follow n8n 2.x; if a node
  shows a warning after import, re-select the table / channel there.
