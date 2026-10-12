## n8n Integration

### HTTP Request Node Configuration

| Field | Value |
|-------|-------|
| **Method** | POST |
| **URL** | `http://python-api:8000/execute` |
| **Authentication** | None (private network) |
| **Body Content Type** | JSON |
| **JSON Body** | `{"data": {{ $json }}, "code_file_name": "your_script.py"}` |

### Workflow Example

```
[Webhook Trigger] → [Set: Prepare Data] → [HTTP Request: python-api] → [If: Script OK?]
                                                                        true  ↓        ↓ false
                                                            [Process result.result]  [Stop and Error]
```

### Error Handling

python-api returns HTTP 200 even when the script fails, so the HTTP Request node stays green. Add after every python-api call:

- If node - condition `{{ $json.result?.status }}` equals `success` (string).
- false -> Stop and Error node, message:
  ```
  python-api {{ $json.result?.status ?? $json.status ?? 'no response' }}: {{ $json.result?.error ?? $json.message ?? JSON.stringify($json).slice(0, 500) }}
  ```
- Result: execution turns red, the script's error line shows in Executions; Error Workflow (Workflow settings) fires if set.
- `$json.status` / `$json.message` cover HTTP 202 `retrying` (no `result` object).
- Full traceback: open the HTTP Request node output -> `result.traceback`.
- Several python-api calls can share one Stop and Error node; the script's message says which mode/step failed.

Reference: `workflows/sheets_to_airtable/` (Upserts OK? / Attachments OK? -> Script Failed).

### Available Scripts Endpoint

Use `GET http://python-api:8000/scripts` to populate dropdowns in n8n.

**n8n Setup:**
1. HTTP Request node → GET `http://python-api:8000/scripts`
2. Split Out node → `{{ $json.scripts }}`
3. Options appear in dropdown

### Webhook URL Configuration

Set in n8n environment or UI:
```
https://n8n.dashboard.com/webhook/your-webhook-path
```