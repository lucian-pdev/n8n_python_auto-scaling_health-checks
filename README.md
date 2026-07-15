# rufscripts
### Script Requirements Declaration

Scripts can declare additional Python packages in header comments:

```python
# requires: pandas==2.1.4, numpy>=1.24.0, requests

def process_data(data):
    import pandas as pd
    df = pd.DataFrame(data["items"])
    result = df.groupby("category").sum()
    return result

result = process_data(data)
```

**Rules:**
- The **first lines must be commented** and the requirements must be present in those lines
        Note: the requirements checking stops at first non-commented line (#)
- Format: `# requires: package==version, package2`
- Version specifiers: `==`, `>=`, `<=`, `>`, `<` supported
- Base packages (fastapi, uvicorn, pydantic, prometheus-client) are pre-installed and filtered out

---

### Virtual Environment Lifecycle

| State | Action | Performance |
|-------|--------|-------------|
| Cache Hit | Venv exists for exact requirements | Zero overhead |
| Cache Miss | Create venv + `pip install` | 10-60 seconds first time |
| Corruption | Detected via ImportError/ModuleNotFoundError | Auto-purge + recreate on next use |

**Venv Storage:** `/app/venvs/` (Docker volume, persists across restarts)

**Cleanup:** Automatic (corrupted) + manual `_cleanup_old_venvs(max_age_days=7)` available

### Script Execution Context

**Available in User Scripts:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `data` | Full payload from n8n | Primary input data |
| `_n8n_data` | Alias of `data` | Backward compatibility |
| `__builtins__` | Python builtins | Standard library access |

**Required Output:**

User script **must** define a `result` variable:

```python
# Good: result defined
result = {"processed": len(items)}

# Bad: no result
print("done")  # Output lost
```

**Security Constraints:**
- 60-second execution timeout
- Subprocess isolation (no shared memory with main process)
- No network restrictions (can HTTP call external APIs)
- Filesystem limited to container (tmp writable, scripts read-only)

___

