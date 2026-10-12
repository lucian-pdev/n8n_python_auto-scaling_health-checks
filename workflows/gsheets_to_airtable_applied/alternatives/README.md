# Fallback Google access methods

The primary method is a **service account** ([../DEPLOY.md](../DEPLOY.md)). Use one of these only when that
doesn't fit. Each is a separate pair of workflow files: import the files of the method you use, then follow
[../DEPLOY.md](../DEPLOY.md) with **step 2** and the **Google credential in step 5** replaced by the section below.
Scripts, Airtable side, Config fields (except where noted), failsafes and dry run are identical.

| | Service account (primary) | OAuth2 | Apps Script proxy |
|---|---|---|---|
| Reads as | the service account | the Google user who clicks "Sign in" in n8n | the sheet owner |
| Linked Drive files must be | shared with the service account | readable by that user | readable by the sheet owner |
| Google Cloud project | yes | yes + OAuth consent screen + client | **no** |
| Expires | no (key until deleted) | refresh token after **7 days** if the consent screen is in "Testing" | no |
| Needs a public HTTPS n8n URL | no | yes (OAuth callback) | no |
| Public endpoint | none | none | `/exec` URL, guarded only by a token |
| Link behind display text / smart chips | lost (display text) | lost | real URL |
| Header row | `header_row` (number, default 1) | same | `header_row`, `0` = auto-detect |

---

## OAuth2

Files: `oauth2/onboarding.workflow.json`, `oauth2/signups.workflow.json`.
Use when the linked files belong to many people and can't be shared with a service account: OAuth2 sees what
the connected user sees.

### Google side (replaces DEPLOY step 2)

1. [console.cloud.google.com](https://console.cloud.google.com) -> project -> APIs & Services -> Library ->
   enable **Google Sheets API** and **Google Drive API**.
2. APIs & Services -> **OAuth consent screen**:
   - User type **Internal** if the sheet owner's account is in the same Google Workspace as the project
     (no review, no 7-day expiry). Otherwise **External**.
   - External: add your account as a test user, then **Publish app** -> "In production". In "Testing" status
     refresh tokens expire after 7 days and the sync stops with an auth error a week after setup.
     Unverified is fine for your own account (Google shows a warning screen once).
3. APIs & Services -> Credentials -> Create credentials -> **OAuth client ID** -> Web application.
   Authorized redirect URI: copy it from the n8n credential screen (step below), normally
   `https://<n8n domain>/rest/oauth2-credential/callback`. Must be HTTPS on a real domain (or exactly `http://localhost:...`).
4. Note the Client ID and Client Secret.

### n8n credentials (replaces the Google credential in DEPLOY step 5)

| Type | Fields |
|---|---|
| Google Sheets OAuth2 API | Client ID, Client Secret -> **Sign in with Google** as the account that can read the sheet |
| Google Drive OAuth2 API | same Client ID / Secret -> Sign in with the same account |

Pick them on: **Read Sheet** (Sheets OAuth2), **Fetch File** (Drive OAuth2).
**Check:** Read Sheet -> Execute step -> one item per row.

| Problem | Fix |
|---|---|
| `redirect_uri_mismatch` on sign-in | Redirect URI in the Google client must equal the one n8n shows, character for character |
| Works, then fails after ~7 days with `invalid_grant` | Consent screen still in "Testing": publish it, sign in again |
| Homelab / no public domain | Sign in through an SSH tunnel to `http://localhost:5678` with n8n's `WEBHOOK_URL` / `N8N_EDITOR_BASE_URL` set to that address, or use the service account |

---

## Apps Script proxy

Files: `apps_script/onboarding.workflow.json`, `apps_script/signups.workflow.json`, `apps_script/Code.gs`.
Use when there is no Google Cloud project (e.g. the Cloud Console asks for a credit card). A small web app
inside the sheet reads rows and files **as the sheet owner** and returns them as JSON.

### Google side (replaces DEPLOY step 2)

1. Open the **real** sheet -> Extensions -> Apps Script. It must be opened from the sheet; a standalone project
   fails with `Cannot read properties of null (reading 'getSheetByName')`.
2. Replace `Code.gs` with [apps_script/Code.gs](apps_script/Code.gs). Set `TOKEN` to a new random value
   (`openssl rand -hex 24`), different from any test sheet.
3. Optional: pick `authorize` in the function list -> **Run** -> approve, so `?whoami=1` can show the account.
4. Deploy -> New deployment -> type **Web app**: **Execute as: Me**, **Who has access: Anyone** -> Deploy -> copy the `/exec` URL.
5. Later edits to `Code.gs` go live only via Deploy -> Manage deployments -> Edit -> **Version: New version** (same URL).

**Check** (from any machine; `-L` because Google redirects):
```bash
U='https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec'; T='<TOKEN>'
curl -sL "$U?tab=Signups" | head -c 100                  # {"error":"forbidden"}  (no token -> refused)
curl -sL "$U?token=$T&whoami=1"                          # account + sheet name
curl -sL "$U?token=$T&tab=Signups" | head -c 300         # {"columns":[...],"header_row":1,"rows":[...
```
| You get | Meaning / fix |
|---|---|
| HTML "Acces refuzat" / "You need access" | Access isn't **Anyone**, or Execute as isn't **Me** |
| HTML "Pagina nu a fost găsită" / 404 | Wrong deployment ID, or the deployment was archived / the project deleted |
| HTML "Fișierul nu poate fi deschis acum" | Transient Google error (the workflow retries 3x); if it persists, same as 404 |
| `{"error":"no tab X"}` | Tab name; exact spelling |
| `header_row` not the real header | Leftover cells above the header: clear them, or set `header_row` in Config |
| `whoami` -> "Nu ai permisiunea ... Session.getEffectiveUser" | Run `authorize` once (step 3) |

### n8n credential (replaces the Google credential in DEPLOY step 5)

| Type | Fields |
|---|---|
| Query Auth | Name = `token`, Value = `TOKEN` from Code.gs |

Pick it on **Read Sheet** and **Fetch File**.

### Config differences

| Field | Apps Script method |
|---|---|
| `gas_url` (instead of `sheet_url`) | the `/exec` URL |
| `header_row` | `0` = auto (first row with 2+ filled cells), or the row number |

Notes:
- Blank / repeated header cells come out as `Column C`, `Name (2)`.
- Files come back whole as base64 JSON (~1.33x their size); fine up to tens of MB.
- Anyone with the URL **and** the token can read the sheet and any Drive file the owner can open. Archive the
  deployment when it's no longer used.
