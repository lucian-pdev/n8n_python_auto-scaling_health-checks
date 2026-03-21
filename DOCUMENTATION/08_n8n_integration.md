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
[Webhook Trigger] → [Function: Prepare Data] → [HTTP Request: python-api]
                                                        ↓
[Function: Process Result] ← [HTTP Response] ← [If: Success?]
       ↓
[Save to Database]
```

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