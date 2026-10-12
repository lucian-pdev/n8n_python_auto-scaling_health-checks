# stdlib only - no "requires" line needed
"""
sheets_to_airtable.py - Google Sheet -> Airtable one-way sync planner for python-api.
Pure transform: no network, no secrets, stdlib only. n8n does all I/O with its own
credentials; this script decides WHAT to send.

Called twice per run via POST /execute:
    {"code_file_name": "sheets_to_airtable.py",
     "data": {"mode": "plan" | "attachments", "config": {...}, "rows": [...], "records": [...]}}

- mode "plan"        - rows = sheet rows, records = current Airtable records
                       -> upserts: Airtable PATCH bodies (performUpsert, <=10 records each)
- mode "attachments" - rows = sheet rows, records = Airtable records + upsert responses
                       -> attachments: one job per Drive file not yet on its record

Return value (python-api puts it in result.result):
    {"mode": ..., "summary": "created=2 updated=1 ...", "upserts" | "attachments": [...], "report": {...}}

Errors: python-api returns HTTP 200 with result.status="error", result.error, result.traceback
(see DOCUMENTATION/04 -> Error Reporting in Scripts). @reports_errors adds the mode:
every exception leaves as ScriptError("<mode>: <Type>: <msg> (line N in func)").
The workflow's "Script OK?" nodes stop the run with that text (red execution in n8n).

Config fields: see README.md -> Config node.
"""
import functools
import json
import re
import traceback

DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{20,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{20,})"),
    re.compile(r"^([A-Za-z0-9_-]{25,})$"),          # bare file ID
]
UPSERT_BATCH = 10                                    # Airtable max records per request
SHEET_META = {"row_number"}                          # added by the n8n Google Sheets node
RULE_OPS = {"equals", "not_equals", "contains", "in", "empty", "not_empty"}
MODES = {}                                           # filled by @mode


# ─────────────────────────────────────────────
# error reporting
# ─────────────────────────────────────────────
class ScriptError(Exception):
    """Bad input or a failed step. str(e) is what n8n shows, so it must stand alone.
    Defined here for local runs; python-api injects its own, matched by name."""


def mode(name: str):
    """Register a function as a mode: run() dispatches data["mode"] to it."""
    def register(func):
        MODES[name] = func
        return func
    return register


def reports_errors(func):
    """Re-raise any exception as one ScriptError line prefixed with the mode,
    so a single Stop and Error node in n8n still says which call failed."""
    @functools.wraps(func)
    def wrapper(data: dict) -> dict:
        try:
            return func(data)
        except ScriptError as e:
            raise ScriptError(f"{func.__name__}: {e}") from e
        except Exception as e:
            frame = traceback.extract_tb(e.__traceback__)[-1]
            raise ScriptError(
                f"{func.__name__}: {type(e).__name__}: {e} (line {frame.lineno} in {frame.name})"
            ) from e
    return wrapper


def _summary(report: dict) -> str:
    """One line for the n8n node output: counts, then how many skipped/warnings."""
    parts = [f"{k}={v}" for k, v in report.items() if isinstance(v, int)]
    parts += [f"{k}={len(v)}" for k, v in report.items() if isinstance(v, list)]
    return " ".join(parts)


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────
def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _norm(value) -> str:
    """Comparable text form of a sheet cell or an Airtable cell value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, dict):                     # collaborator / select objects
        return _norm(value.get("name") or value.get("email") or value.get("id"))
    if isinstance(value, list):
        return ", ".join(_norm(v) for v in value)
    return str(value).strip()


def _flatten_records(records) -> list[dict]:
    """Accept Airtable pages ({records: [...]}), raw records ({id, fields}) or
    n8n-flattened records ({id, createdTime, Field: value})."""
    flat = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("records"), list):
            flat.extend(_flatten_records(item["records"]))
        elif "id" in item:
            fields = item.get("fields")
            if not isinstance(fields, dict):
                fields = {k: v for k, v in item.items() if k not in ("id", "createdTime")}
            flat.append({"id": item["id"], "fields": fields})
    return flat


def _key(fields: dict, key_fields: list[str]) -> tuple | None:
    values = tuple(_norm(fields.get(f)) for f in key_fields)
    return None if any(v == "" for v in values) else values


def _drive_ids(cell) -> tuple[list[str], list[str]]:
    """Return (file_ids, unparsed_parts) for a cell holding one or more Drive links."""
    ids, bad = [], []
    for part in re.split(r"[\s,;]+", _norm(cell)):
        if not part:
            continue
        for pattern in DRIVE_ID_PATTERNS:
            match = pattern.search(part)
            if match:
                ids.append(match.group(1))
                break
        else:
            bad.append(part)
    return ids, bad


def _conditions(rule: dict) -> list[dict]:
    conds = rule.get("when") or []
    return [conds] if isinstance(conds, dict) else conds


def _condition_true(cond: dict, row: dict, existing: dict) -> bool:
    source = existing if cond.get("source") == "airtable" else row
    actual = _norm(source.get(cond.get("column")))
    expected = cond.get("value")
    op = cond.get("op", "equals")
    if op == "equals":
        return actual == _norm(expected)
    if op == "not_equals":
        return actual != _norm(expected)
    if op == "contains":
        return _norm(expected).lower() in actual.lower()
    if op == "in":
        return actual in {_norm(v) for v in (expected or [])}
    if op == "empty":
        return actual == ""
    return actual != ""                              # not_empty (ops checked in _config)


def _apply_rules(rules: list, row: dict, existing: dict, fields: dict) -> bool:
    """Mutate fields with matching rules' "set"; return False if a rule says skip."""
    for rule in rules:
        if all(_condition_true(c, row, existing) for c in _conditions(rule)):
            if rule.get("skip"):
                return False
            fields.update(rule.get("set") or {})
    return True


def _rows_by_key(rows, cfg, existing_by_key, report) -> dict:
    """Map + rule each sheet row; returns {key: (row, fields)}; last duplicate wins."""
    out = {}
    for row in rows:
        row = {k: v for k, v in row.items() if k not in SHEET_META} | {"row_number": row.get("row_number")}
        fields = {}
        for column, field in cfg["mapping"].items():
            value = row.get(column)
            if cfg["skip_empty"] and _blank(value):
                continue
            fields[field] = value.strip() if isinstance(value, str) else value
        key = _key(fields, cfg["key_fields"])
        if key is None:
            report["skipped"].append({"row": row["row_number"], "reason": "key field empty"})
            continue
        existing = existing_by_key.get(key, {}).get("fields", {})
        if not _apply_rules(cfg["rules"], row, existing, fields):
            report["skipped"].append({"row": row["row_number"], "reason": "rule: skip"})
            continue
        if key in out:
            report["warnings"].append(f"duplicate key {key} in rows {out[key][0]['row_number']} and {row['row_number']}; last wins")
        out[key] = (row, fields)
    return out


def _config(data: dict) -> dict:
    """Parse + validate the Config node. Fails fast with a ScriptError naming the field."""
    cfg = dict(data.get("config") or {})
    for name in ("mapping", "attachments", "rules", "key_fields"):
        if isinstance(cfg.get(name), str):          # n8n "object"/"array" fields may arrive as JSON text
            try:
                cfg[name] = json.loads(cfg[name] or "null")
            except json.JSONDecodeError as e:
                raise ScriptError(f"config.{name} is not valid JSON: {e}") from e
    cfg["attachments"] = cfg.get("attachments") or {}
    cfg["rules"] = cfg.get("rules") or []
    cfg.setdefault("create_missing", True)
    cfg.setdefault("skip_empty", True)
    cfg.setdefault("typecast", True)

    if not cfg.get("key_fields") or not cfg.get("mapping"):
        raise ScriptError("config needs key_fields and mapping")
    missing = [f for f in cfg["key_fields"] if f not in cfg["mapping"].values()]
    if missing:
        raise ScriptError(f"key_fields {missing} must be targets in mapping")
    for i, rule in enumerate(cfg["rules"]):
        for cond in _conditions(rule):
            if cond.get("op", "equals") not in RULE_OPS:
                raise ScriptError(f"rules[{i}]: unknown op {cond.get('op')!r}, use one of {sorted(RULE_OPS)}")
            if "column" not in cond:
                raise ScriptError(f"rules[{i}]: condition without 'column'")
    return cfg


def _inputs(data: dict) -> tuple[list, list]:
    rows, records = data.get("rows") or [], data.get("records") or []
    if not isinstance(rows, list) or not isinstance(records, list):
        raise ScriptError("rows and records must be lists")
    return rows, records


# ─────────────────────────────────────────────
# modes
# ─────────────────────────────────────────────
@mode("plan")
@reports_errors
def plan(data: dict) -> dict:
    cfg = _config(data)
    rows, records = _inputs(data)
    existing_by_key = {}
    for rec in _flatten_records(records):
        key = _key(rec["fields"], cfg["key_fields"])
        if key is not None:
            existing_by_key[key] = rec

    report = {"created": 0, "updated": 0, "unchanged": 0, "skipped": [], "warnings": []}
    to_send = []
    for key, (row, fields) in _rows_by_key(rows, cfg, existing_by_key, report).items():
        existing = existing_by_key.get(key)
        if existing is None:
            if not cfg["create_missing"]:
                report["skipped"].append({"row": row["row_number"], "reason": "no matching record (create_missing=false)"})
                continue
            report["created"] += 1
        elif all(_norm(existing["fields"].get(f)) == _norm(v) for f, v in fields.items()):
            report["unchanged"] += 1
            continue
        else:
            report["updated"] += 1
        to_send.append({"fields": fields})

    upserts = [
        {
            "performUpsert": {"fieldsToMergeOn": cfg["key_fields"]},
            "typecast": cfg["typecast"],
            "records": to_send[i:i + UPSERT_BATCH],
        }
        for i in range(0, len(to_send), UPSERT_BATCH)
    ]
    return {"upserts": upserts, "report": report}


@mode("attachments")
@reports_errors
def attachments(data: dict) -> dict:
    cfg = _config(data)
    rows, records = _inputs(data)
    records_by_key = {}
    for rec in _flatten_records(records):           # later entries (upsert responses) win
        key = _key(rec["fields"], cfg["key_fields"])
        if key is not None:
            records_by_key[key] = rec

    report = {"queued": 0, "already_attached": 0, "skipped": [], "warnings": []}
    jobs = []
    for key, (row, _fields) in _rows_by_key(rows, cfg, records_by_key, report).items():
        record = records_by_key.get(key)
        for column, field in cfg["attachments"].items():
            ids, bad = _drive_ids(row.get(column))
            for part in bad:
                report["warnings"].append(f"row {row['row_number']} {column}: not a Drive file link: {part[:80]}")
            if not ids:
                continue
            if record is None:
                report["skipped"].append({"row": row["row_number"], "reason": f"no Airtable record for {column}"})
                continue
            present = {a.get("filename", "") for a in record["fields"].get(field) or [] if isinstance(a, dict)}
            for file_id in ids:
                if any(name.startswith(f"gd-{file_id}") for name in present):
                    report["already_attached"] += 1
                    continue
                jobs.append({
                    "record_id": record["id"],
                    "field": field,
                    "drive_file_id": file_id,
                    "filename_prefix": f"gd-{file_id}-",
                    "row": row["row_number"],
                })
                report["queued"] += 1
    return {"attachments": jobs, "report": report}


def run(data: dict) -> dict:
    name = data.get("mode", "plan")
    if name not in MODES:
        raise ScriptError(f"unknown mode {name!r}, use one of {sorted(MODES)}")
    out = MODES[name](data)
    return {"mode": name, "summary": _summary(out["report"]), **out}


if "data" in globals():                    # python-api injects `data`
    result = run(data)                     # noqa: F821
else:                                      # local test: python3 sheets_to_airtable.py payload.json
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    try:                                   # same shape python-api returns in "result"
        print(json.dumps({"status": "success", "result": run(payload.get("data", payload))}, indent=2, ensure_ascii=False))
    except ScriptError as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        sys.exit(1)
