# stdlib only - no "requires" line needed
"""
gsheets_to_airtable.py - Google Sheet -> Airtable one-way sync planner for python-api.
Pure transform: no network, no secrets, stdlib only. n8n does all I/O (Google Sheets/Drive nodes
with a service account or OAuth2, or the Apps Script read proxy; Airtable API); this script decides WHAT to send.

Called twice per run via POST /execute:
    {"code_file_name": "gsheets_to_airtable.py",
     "data": {"mode": "plan" | "attachments", "config": {...}, "schema": {...},
              "rows": [...], "records": [...]}}

- mode "plan"        - rows = sheet rows (list of row objects from the Google Sheets node, or the
                       Apps Script proxy answer {columns, rows}), records = current Airtable records,
                       schema = the table object from GET /v0/meta/bases/{base}/tables
                       -> upserts: Airtable PATCH bodies (<=10 records each)
                          updates go by record id (key matched here, not by Airtable),
                          creates go with performUpsert as a guard against double creates
- mode "attachments" - rows = sheet rows, records = Airtable records + upsert responses
                       -> attachments: one job per Drive file not yet on its record

Differences from workflows/sheets_to_airtable (the general version):
- values are converted to the Airtable field type from `schema` (dates, numbers, checkboxes,
  select options) and compared by meaning, so "8/7/2026" vs "2026-08-07" is "unchanged"
- key_match "name": human-name keys ignore case, accents, punctuation and extra spaces;
  similar-but-different names are only reported as "possible match", never merged
- email_key (on/off in Config): match on email, name as fallback for records without one;
  missing Airtable field / sheet column -> warning + name matching, the run goes on
- compose: build a sheet column from others, e.g. "{First Name} {Last Name}"
- rules can be limited to new records: "on": "create"
- sheet error cells (#REF!, #N/A ...) and unconvertible values drop that field only, not the row
- dry_run (Config): plan + report as usual, but nothing is written or uploaded
- location_filter: skip rows whose location (sheet value, else the Airtable record's) is outside the allowed
  regions; unknown/blank locations pass by default
- create_only_fields: fields written only when the record is created (another source owns them afterwards)

Errors: python-api returns HTTP 200 with result.status="error", result.error, result.traceback.
@reports_errors adds the mode: every exception leaves as ScriptError("<mode>: <Type>: <msg> (line N in func)").
"""
import datetime as dt
import difflib
import functools
import json
import re
import traceback
import unicodedata

DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{20,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{20,})"),
    re.compile(r"^([A-Za-z0-9_-]{25,})$"),          # bare file ID
]
UPSERT_BATCH = 10                                    # Airtable max records per request
SHEET_META = {"row_number"}                          # added by the read proxy / Sheets node
RULE_OPS = {"equals", "not_equals", "contains", "in", "empty", "not_empty"}
RULE_ON = {"always", "create", "update"}
SHEET_ERRORS = {"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!", "#ERROR!", "#SPILL!"}
READ_ONLY_TYPES = {"formula", "rollup", "count", "lookup", "multipleLookupValues", "autoNumber",
                   "createdTime", "lastModifiedTime", "createdBy", "lastModifiedBy", "button",
                   "aiText", "externalSyncSource"}
NUMBER_TYPES = {"number", "percent", "currency", "rating", "duration"}
TRUE_WORDS = {"true", "yes", "y", "1", "x", "da", "✓", "✔", "checked"}
FALSE_WORDS = {"false", "no", "n", "0", "nu", ""}
MAX_NEAR_WARNINGS = 50
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
# text helpers
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
    return re.sub(r"\s+", " ", str(value)).strip()


def _fold(text: str) -> str:
    """Case- and accent-insensitive form: "Lișiță" -> "lisita"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _name_key(value) -> str:
    """Human-name key: no case, accents, punctuation; "Sergiu-Valentin  TEST" == "sergiu valentin test"."""
    return " ".join(re.sub(r"[^\w]+", " ", _fold(_norm(value))).split())


def _option_key(value) -> str:
    """Select option match: "9. Dropped out" == "09. dropped out" == " 09.Dropped Out "."""
    return re.sub(r"\b0+(\d)", r"\1", _name_key(value))


# ─────────────────────────────────────────────
# type conversion (sheet text -> Airtable value)
# ─────────────────────────────────────────────
class Skip(Exception):
    """This one cell can't be sent; the message goes to report.warnings."""


def _parse_date(text: str, order: str) -> dt.date:
    text = text.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)          # ISO, also ISO datetime
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if not m:
        raise ValueError(f"not a date: {text!r}")
    a, b, y = int(m[1]), int(m[2]), int(m[3])
    y += 2000 if y < 100 else 0
    month, day = (a, b) if order == "MDY" else (b, a)
    return dt.date(y, month, day)


def _parse_number(text: str) -> float | int:
    t = text.strip().replace(" ", "").replace(" ", "")
    pct = t.endswith("%")
    t = t.rstrip("%")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", t):            # 1,234.5
        t = t.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", t):                           # 3,5 (decimal comma)
        t = t.replace(",", ".")
    n = float(t)                                                  # ValueError on "x"
    n = n / 100 if pct else n
    return int(n) if n.is_integer() else n


def _choice(value: str, field: dict, cfg: dict) -> str:
    """Map a sheet value to an existing select option, or decide what to do with a new one."""
    choices = [c["name"] for c in (field.get("options") or {}).get("choices", [])]
    aliases = cfg["option_aliases"].get(field["name"], {})
    value = aliases.get(value, value)
    by_key = {_option_key(c): c for c in choices}
    hit = by_key.get(_option_key(value))
    if hit is not None:
        return hit
    if cfg["new_options"] == "create":
        return value                                              # typecast creates it
    raise Skip(f"{value!r} is not an option of {field['name']!r} (new_options=skip)")


def _convert(value, field: dict | None, cfg: dict):
    """Sheet cell -> value Airtable accepts for this field type. Raises Skip for this cell only."""
    text = _norm(value) if not isinstance(value, str) else value.strip()
    if text in SHEET_ERRORS:
        raise Skip(f"sheet error value {text}")
    ftype = (field or {}).get("type", "singleLineText")
    if ftype == "checkbox":
        low = text.casefold()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
        raise Skip(f"{text!r} is not a checkbox value")
    if ftype in NUMBER_TYPES:
        try:
            n = _parse_number(text)
        except ValueError:
            raise Skip(f"{text!r} is not a number") from None
        if ftype == "rating" and not (isinstance(n, int) and 1 <= n <= (field.get("options") or {}).get("max", 5)):
            raise Skip(f"{text!r} is not a rating 1..{(field.get('options') or {}).get('max', 5)}")
        return n
    if ftype in ("date", "dateTime"):
        try:
            d = _parse_date(text, cfg["date_order"])
        except ValueError as e:
            raise Skip(str(e)) from None
        return d.isoformat()
    if ftype == "singleSelect":
        return _choice(text, field, cfg)
    if ftype == "multipleSelects":
        return [_choice(part, field, cfg) for part in re.split(r"\s*[,;\n]\s*", text) if part]
    return text                                                   # text-like: keep sheet text


def _same(new, old, field: dict | None, key_mode: str | None, cfg: dict) -> bool:
    """Would writing `new` change what Airtable shows? Compares by meaning, not by spelling.
    key_mode: None for normal fields, "name"/"exact" for key fields (see _key_mode)."""
    if key_mode == "name":
        return _name_key(new) == _name_key(old)
    if key_mode == "exact":
        return _norm(new).casefold() == _norm(old).casefold()
    if isinstance(new, list):
        return sorted(_norm(v) for v in new) == sorted(_norm(v) for v in (old or []))
    a, b = _norm(new), _norm(old)
    if a == b:
        return True
    if isinstance(new, bool) or isinstance(old, bool):
        return bool(new) == bool(old)
    try:                                                          # 5 == 5.0, "8/7/2026" == "2026-08-07T..."
        return float(a) == float(b)
    except ValueError:
        pass
    try:
        return _parse_date(a, cfg["date_order"]) == _parse_date(b, cfg["date_order"])
    except ValueError:
        return False


# ─────────────────────────────────────────────
# records, keys, rules
# ─────────────────────────────────────────────
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


def _key_mode(field: str, cfg: dict) -> str:
    """How a key field is compared: "name" (human names) or "exact" (case-insensitive, e.g. email)."""
    return "exact" if field in cfg["exact_key_fields"] else cfg["key_match"]


def _key(fields: dict, cfg: dict, key_fields: list | None = None) -> tuple | None:
    values = tuple(
        _name_key(fields.get(f)) if _key_mode(f, cfg) == "name" else _norm(fields.get(f)).casefold()
        for f in key_fields or cfg["key_fields"]
    )
    return None if any(v == "" for v in values) else values


class _Index:
    """Airtable records by key. With email_key on, records without an Email (made before the
    field existed) are also found by the name key, so they get their Email filled in, not a twin."""

    def __init__(self, records, cfg, report):
        self.cfg, self.by_key, self.by_fallback = cfg, {}, {}
        for rec in _flatten_records(records):                     # later entries (upsert responses) win
            key = _key(rec["fields"], cfg)
            if key is not None:
                if key in self.by_key and self.by_key[key]["id"] != rec["id"]:
                    report["warnings"].append(f"Airtable has 2+ records for key {key}: {self.by_key[key]['id']}, {rec['id']}; using the last")
                self.by_key[key] = rec
            elif cfg["fallback_keys"]:
                fallback = _key(rec["fields"], cfg, cfg["fallback_keys"])
                if fallback is not None:
                    self.by_fallback[fallback] = rec

    def find(self, fields: dict) -> dict | None:
        rec = self.by_key.get(_key(fields, self.cfg))
        if rec is None and self.cfg["fallback_keys"]:
            rec = self.by_fallback.get(_key(fields, self.cfg, self.cfg["fallback_keys"]))
        return rec

    def name_keys(self) -> list[tuple]:
        """Name keys of all known records, for possible_matches."""
        names = self.cfg["fallback_keys"] or self.cfg["key_fields"]
        recs = list(self.by_key.values()) + list(self.by_fallback.values())
        return [k for k in (_key(r["fields"], self.cfg, names) for r in recs) if k is not None]


def _resolve_keys(cfg: dict, fields_by_name: dict, rows: list, report: dict) -> None:
    """email_key on: match on the email column, keep key_fields (name) as fallback.
    Missing Airtable field or sheet column -> warning and name matching, never a failed run."""
    cfg["fallback_keys"], cfg["exact_key_fields"] = None, set()
    if not cfg["email_key"]:
        return
    column, field = cfg["email_key_column"], cfg["email_key_field"]
    if field not in fields_by_name:
        report["warnings"].append(f"email_key is on but Airtable has no field {field!r}: matching on {cfg['key_fields']}")
        return
    if not any(not _blank(r.get(column)) for r in rows):
        report["warnings"].append(f"email_key is on but sheet column {column!r} is missing or empty: matching on {cfg['key_fields']}")
        return
    cfg["mapping"] = {**cfg["mapping"], column: field}
    cfg["fallback_keys"], cfg["key_fields"] = cfg["key_fields"], [field]
    cfg["exact_key_fields"] = {field}


def _near_matches(new_keys, existing_keys, report) -> None:
    """Report (never merge) names that look like the same person spelled differently."""
    existing = [" ".join(k) for k in existing_keys]
    by_tokens = {}
    for e in existing:
        by_tokens.setdefault(tuple(sorted(e.split())), []).append(e)
    found = 0
    for key, row_number in new_keys:
        name = " ".join(key)
        hits = set(by_tokens.get(tuple(sorted(name.split())), []))                 # word order
        tokens = set(name.split())
        if len(tokens) >= 2:
            hits |= {e for e in existing if tokens < set(e.split()) or set(e.split()) < tokens}  # middle name
        hits |= set(difflib.get_close_matches(name, existing, n=2, cutoff=0.9))    # typo
        hits.discard(name)
        if hits:
            report["possible_matches"].append({"row": row_number, "sheet": name, "airtable": sorted(hits)[:3]})
            found += 1
            if found >= MAX_NEAR_WARNINGS:
                return


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


def _apply_rules(rules: list, row: dict, existing: dict | None, fields: dict) -> bool:
    """Mutate fields with matching rules' "set"; return False if a rule says skip.
    existing=None means the row will create a new record."""
    for rule in rules:
        on = rule.get("on", "always")
        if (on == "create" and existing is not None) or (on == "update" and existing is None):
            continue
        if all(_condition_true(c, row, existing or {}) for c in _conditions(rule)):
            if rule.get("skip"):
                return False
            fields.update(rule.get("set") or {})
    return True


def _compose(row: dict, cfg: dict) -> dict:
    """Add virtual columns: {"Full Name": "{First Name} {Last Name}"}; blanks collapse."""
    for column, template in cfg["compose"].items():
        text = re.sub(r"\{([^{}]+)\}", lambda m: _norm(row.get(m[1])), template)
        row[column] = re.sub(r"\s+", " ", text).strip(" ,;-")
    return row


def _rows_by_key(rows, cfg, fields_by_name, index, report) -> dict:
    """Compose + map + convert + rule each sheet row; returns {key: (row, fields)}; last duplicate wins.
    With email_key on, a row without email is keyed by name ("name", ...) instead."""
    out = {}
    for row in rows:
        row = {k: v for k, v in row.items() if k not in SHEET_META} | {"row_number": row.get("row_number")}
        row = _compose(row, cfg)
        fields = {}
        for column, name in cfg["mapping"].items():
            value = row.get(column)
            if _blank(value):
                if not cfg["skip_empty"]:
                    fields[name] = None
                continue
            try:
                fields[name] = _convert(value, fields_by_name.get(name), cfg)
            except Skip as e:
                report["field_warnings"].append(f"row {row['row_number']} {name}: {e}")
        key = _key(fields, cfg)
        if key is None and cfg["fallback_keys"]:
            fallback = _key(fields, cfg, cfg["fallback_keys"])
            key = ("name",) + fallback if fallback else None
        if key is None:
            report["skipped"].append({"row": row["row_number"], "reason": "key field empty"})
            continue
        existing = index.find(fields)
        if cfg["location_filter"]:
            allowed, where = _location_allowed(fields, existing, cfg)
            if not allowed:
                report["skipped"].append({"row": row["row_number"], "reason": f"location not allowed: {where[:60]}"})
                continue
        if not _apply_rules(cfg["rules"], row, existing["fields"] if existing else None, fields):
            report["skipped"].append({"row": row["row_number"], "reason": "rule: skip"})
            continue
        if key in out:
            report["warnings"].append(f"duplicate key {key} in rows {out[key][0]['row_number']} and {row['row_number']}; last wins")
        out[key] = (row, fields)
    return out


# ─────────────────────────────────────────────
# location filter (config.location_filter)
# ─────────────────────────────────────────────
# Place names per region, one per comma, matched as whole words after _name_key folding
# (no case/accents/punctuation). Romanian spellings included: the sheets come from the Romanian diaspora.
US_STATES = ("alabama, alaska, arizona, arkansas, california, colorado, connecticut, delaware, florida, georgia, "
             "hawaii, idaho, illinois, indiana, iowa, kansas, kentucky, louisiana, maine, maryland, massachusetts, "
             "michigan, minnesota, mississippi, missouri, montana, nebraska, nevada, new hampshire, new jersey, "
             "new mexico, new york, north carolina, north dakota, ohio, oklahoma, oregon, pennsylvania, rhode island, "
             "south carolina, south dakota, tennessee, texas, utah, vermont, virginia, washington, west virginia, "
             "wisconsin, wyoming, district of columbia, washington dc")
REGION_PLACES = {
    "europe": "albania, andorra, austria, belarus, bielorusia, belgium, belgia, bosnia, bosnia and herzegovina, "
              "bulgaria, croatia, cyprus, cipru, czech republic, czechia, cehia, denmark, danemarca, estonia, finland, "
              "finlanda, france, franta, germany, germania, greece, grecia, hungary, ungaria, iceland, islanda, "
              "ireland, irlanda, italy, italia, kosovo, latvia, letonia, liechtenstein, lithuania, lituania, "
              "luxembourg, luxemburg, malta, moldova, republica moldova, monaco, montenegro, muntenegru, netherlands, "
              "holland, olanda, tarile de jos, north macedonia, macedonia, norway, norvegia, poland, polonia, "
              "portugal, portugalia, romania, san marino, serbia, slovakia, slovacia, slovenia, spain, spania, sweden, "
              "suedia, switzerland, elvetia, ukraine, ucraina, united kingdom, uk, great britain, britain, england, "
              "scotland, wales, northern ireland, marea britanie, regatul unit, anglia, scotia, tara galilor, vatican, "
              "gibraltar, isle of man, jersey, guernsey, faroe islands",
    "north_america": "usa, us, u s, u s a, united states, united states of america, america, statele unite, "
                     "statele unite ale americii, sua, canada, ontario, quebec, british columbia, alberta, manitoba, "
                     "saskatchewan, nova scotia, new brunswick, newfoundland, prince edward island, yukon, nunavut, "
                     "northwest territories, " + US_STATES,
    "australia": "australia, new south wales, victoria, queensland, western australia, south australia, tasmania, "
                 "northern territory, australian capital territory",
    # not allowed unless listed in config.location_filter.regions
    "mexico": "mexico, mexic",
    "central_america_caribbean": "belize, costa rica, el salvador, guatemala, honduras, nicaragua, panama, cuba, "
                                 "dominican republic, haiti, jamaica, bahamas, barbados, trinidad and tobago, "
                                 "puerto rico, dominica, grenada, saint lucia, antigua",
    "transcontinental": "turkey, turkiye, turcia, russia, rusia, russian federation, georgia, armenia, azerbaijan",
    "new_zealand": "new zealand, noua zeelanda, nz",
    "other": "afghanistan, algeria, angola, argentina, bahrain, bangladesh, bolivia, botswana, brazil, brazilia, "
             "chile, china, colombia, congo, ecuador, egypt, egipt, ethiopia, ghana, india, indonesia, iran, iraq, "
             "israel, ivory coast, japan, japonia, jordan, iordania, kazakhstan, kenya, kuwait, lebanon, libya, "
             "malaysia, mauritius, mongolia, morocco, maroc, nepal, nigeria, oman, pakistan, palestine, paraguay, "
             "peru, philippines, qatar, saudi arabia, senegal, singapore, south africa, africa de sud, south korea, "
             "korea, sri lanka, sudan, syria, taiwan, thailand, tunisia, uganda, united arab emirates, uae, "
             "emiratele arabe unite, uruguay, uzbekistan, venezuela, vietnam, yemen, zambia, zimbabwe, hong kong, "
             "greenland, dubai, abu dhabi, doha, istanbul, moscow, tel aviv",
}
REGION_PATTERNS = {
    region: re.compile(r"\b(" + "|".join(sorted({p.strip() for p in places.split(",") if p.strip()},
                                                key=len, reverse=True)) + r")\b")
    for region, places in REGION_PLACES.items()
}
LOCATION_DEFAULT_REGIONS = ["europe", "north_america", "australia"]


def _regions_in(text: str) -> set:
    """Regions whose place names appear in a free-text location ("Toronto, Ontario" -> {"north_america"})."""
    folded = _name_key(text)
    return {region for region, pattern in REGION_PATTERNS.items() if pattern.search(folded)} if folded else set()


def _location_allowed(fields: dict, existing: dict | None, cfg: dict) -> tuple[bool, str]:
    """(allowed, location text). The sheet value wins over Airtable's; any allowed place wins over others
    ("Atlanta, Georgia" is the US state, not the country); unknown/blank -> config.location_filter.unknown."""
    lf = cfg["location_filter"]
    text = _norm(fields.get(lf["field"])) or _norm(((existing or {}).get("fields") or {}).get(lf["field"]))
    found = _regions_in(text)
    if found & set(lf["regions"]):
        return True, text
    if found:
        return False, text
    return lf["unknown"] == "keep", text


# ─────────────────────────────────────────────
# config + inputs
# ─────────────────────────────────────────────
def _config(data: dict) -> dict:
    """Parse + validate the Config node. Fails fast with a ScriptError naming the field."""
    cfg = dict(data.get("config") or {})
    for name in ("mapping", "attachments", "rules", "key_fields", "compose", "option_aliases", "location_filter", "create_only_fields"):
        if isinstance(cfg.get(name), str):          # n8n "object"/"array" fields may arrive as JSON text
            try:
                cfg[name] = json.loads(cfg[name] or "null")
            except json.JSONDecodeError as e:
                raise ScriptError(f"config.{name} is not valid JSON: {e}") from e
    for name, default in (("attachments", {}), ("rules", []), ("compose", {}), ("option_aliases", {})):
        cfg[name] = cfg.get(name) or default
    cfg.setdefault("create_missing", True)
    cfg.setdefault("skip_empty", True)
    cfg.setdefault("typecast", True)
    cfg["email_key"] = cfg.get("email_key") in (True, "true", "True", 1, "1")
    cfg["dry_run"] = cfg.get("dry_run") in (True, "true", "True", 1, "1")
    cfg["create_only_fields"] = cfg.get("create_only_fields") or []
    lf = cfg.get("location_filter") or {}
    if lf:
        lf = {"field": lf.get("field") or "Current Location", "regions": lf.get("regions") or LOCATION_DEFAULT_REGIONS,
              "unknown": lf.get("unknown") or "keep"}
        bad = [r for r in lf["regions"] if r not in REGION_PLACES or r == "other"]
        if bad:
            raise ScriptError(f"location_filter.regions {bad}: use any of {sorted(set(REGION_PLACES) - {'other'})}")
        if lf["unknown"] not in ("keep", "skip"):
            raise ScriptError(f"location_filter.unknown {lf['unknown']!r}: use 'keep' or 'skip'")
    cfg["location_filter"] = lf
    cfg["email_key_column"] = cfg.get("email_key_column") or "Email"
    cfg["email_key_field"] = cfg.get("email_key_field") or "Email"
    cfg["key_match"] = cfg.get("key_match") or "exact"
    cfg["date_order"] = (cfg.get("date_order") or "MDY").upper()
    cfg["new_options"] = cfg.get("new_options") or "skip"

    if not cfg.get("key_fields") or not cfg.get("mapping"):
        raise ScriptError("config needs key_fields and mapping")
    missing = [f for f in cfg["key_fields"] if f not in cfg["mapping"].values()]
    if missing:
        raise ScriptError(f"key_fields {missing} must be targets in mapping")
    if cfg["key_match"] not in ("exact", "name"):
        raise ScriptError(f"key_match {cfg['key_match']!r}: use 'exact' or 'name'")
    if cfg["date_order"] not in ("MDY", "DMY"):
        raise ScriptError(f"date_order {cfg['date_order']!r}: use 'MDY' (8/31/2024) or 'DMY' (31/8/2024)")
    if cfg["new_options"] not in ("skip", "create"):
        raise ScriptError(f"new_options {cfg['new_options']!r}: use 'skip' or 'create'")
    for i, rule in enumerate(cfg["rules"]):
        if rule.get("on", "always") not in RULE_ON:
            raise ScriptError(f"rules[{i}]: unknown on {rule.get('on')!r}, use one of {sorted(RULE_ON)}")
        for cond in _conditions(rule):
            if cond.get("op", "equals") not in RULE_OPS:
                raise ScriptError(f"rules[{i}]: unknown op {cond.get('op')!r}, use one of {sorted(RULE_OPS)}")
            if "column" not in cond:
                raise ScriptError(f"rules[{i}]: condition without 'column'")
    return cfg


def _schema_fields(data: dict, cfg: dict) -> dict:
    """{field name: field} from the table schema; checks every mapping target before any work."""
    schema = data.get("schema") or {}
    if isinstance(schema.get("tables"), list):                    # whole base passed: pick the table
        table = cfg.get("airtable_table")
        schema = next((t for t in schema["tables"] if table in (t.get("id"), t.get("name"))), {})
    fields = {f["name"]: f for f in schema.get("fields", [])}
    if not fields:
        raise ScriptError("schema missing: pass the table from GET /v0/meta/bases/{base}/tables (token scope schema.bases:read)")
    # attachments are optional: a missing/wrong attachment field drops that attachment, never the run
    cfg["config_warnings"] = []
    for column, field in list(cfg["attachments"].items()):
        problem = ("no such Airtable field" if field not in fields
                   else None if fields[field]["type"] == "multipleAttachments"
                   else f"Airtable field is {fields[field]['type']}, not an attachment field")
        if problem:
            cfg["config_warnings"].append(f"attachments {column!r} -> {field!r} ignored: {problem}")
            del cfg["attachments"][column]
    targets = set(cfg["mapping"].values())
    for rule in cfg["rules"]:
        targets |= set((rule.get("set") or {}).keys())
    unknown = sorted(t for t in targets if t not in fields)
    if unknown:
        hint = {t: difflib.get_close_matches(t, fields, n=1) for t in unknown}
        raise ScriptError(f"no such Airtable field(s) {unknown}; close: {hint}")
    ro = sorted(t for t in targets if fields[t]["type"] in READ_ONLY_TYPES)
    if ro:
        raise ScriptError(f"read-only (computed) Airtable field(s) {ro} can't be written")
    bad = sorted(f for f in cfg["mapping"].values() if fields[f]["type"] in ("multipleAttachments", "multipleRecordLinks"))
    if bad:
        raise ScriptError(f"{bad}: attachments go in config.attachments, linked records are not supported")
    return fields


def _inputs(data: dict) -> tuple[list, list]:
    rows, records = data.get("rows") or [], data.get("records") or []
    if isinstance(rows, str):                                     # Apps Script error/login page
        raise ScriptError(f"read proxy returned non-JSON (deployment access or version?): {rows[:120]!r}")
    if isinstance(rows, dict):                                    # whole read-proxy answer
        if rows.get("error"):
            raise ScriptError(f"read proxy: {rows['error']}")
        if not isinstance(rows.get("rows"), list):
            raise ScriptError(f"read proxy answer has no rows (deployment access or version?): {json.dumps(rows)[:120]}")
        rows = rows["rows"]
    if not isinstance(rows, list) or not isinstance(records, list):
        raise ScriptError("rows and records must be lists")
    return rows, records


def _new_report(**counts) -> dict:
    return {**counts, "skipped": [], "warnings": [], "field_warnings": [], "possible_matches": []}


# ─────────────────────────────────────────────
# modes
# ─────────────────────────────────────────────
@mode("plan")
@reports_errors
def plan(data: dict) -> dict:
    cfg = _config(data)
    fields_by_name = _schema_fields(data, cfg)
    rows, records = _inputs(data)
    report = _new_report(created=0, updated=0, unchanged=0)
    report["warnings"] += cfg["config_warnings"]
    _resolve_keys(cfg, fields_by_name, rows, report)
    index = _Index(records, cfg, report)

    creates, plain_creates, updates, new_names, claimed = [], [], [], [], set()
    for key, (row, fields) in _rows_by_key(rows, cfg, fields_by_name, index, report).items():
        existing = index.find(fields)
        if existing is not None and existing["id"] in claimed:   # 2 emails, 1 old record found by name
            report["warnings"].append(f"row {row['row_number']}: record {existing['id']} already matched by another row; creating a new one")
            existing = None
        if existing is None:
            if not cfg["create_missing"]:
                report["skipped"].append({"row": row["row_number"], "reason": "no matching record (create_missing=false)"})
                continue
            report["created"] += 1
            # performUpsert needs a value in every merge field; rows without one are plain creates
            (creates if _key(fields, cfg) is not None else plain_creates).append({"fields": fields})
            new_names.append((_key(fields, cfg, cfg["fallback_keys"]) if cfg["fallback_keys"] else key, row["row_number"]))
            continue
        claimed.add(existing["id"])
        changed = {
            name: value for name, value in fields.items()
            if name not in cfg["create_only_fields"]                      # set once on create, then owned elsewhere
            and not _same(value, existing["fields"].get(name), fields_by_name.get(name),
                         _key_mode(name, cfg) if name in cfg["key_fields"] + (cfg["fallback_keys"] or []) else None, cfg)
        }
        if not changed:
            report["unchanged"] += 1
            continue
        report["updated"] += 1
        updates.append({"id": existing["id"], "fields": changed})
    if cfg["key_match"] == "name":
        new_set = {k for k, _ in new_names}
        _near_matches([(k, r) for k, r in new_names if k], [k for k in index.name_keys() if k not in new_set], report)

    batch = lambda recs, extra: [                                 # noqa: E731
        {**extra, "typecast": cfg["typecast"], "records": recs[i:i + UPSERT_BATCH]}
        for i in range(0, len(recs), UPSERT_BATCH)
    ]
    upserts = (batch(updates, {}) + batch(plain_creates, {})
               + batch(creates, {"performUpsert": {"fieldsToMergeOn": cfg["key_fields"]}}))
    return {"upserts": upserts, "dry_run": cfg["dry_run"], "report": report}


@mode("attachments")
@reports_errors
def attachments(data: dict) -> dict:
    cfg = _config(data)
    fields_by_name = _schema_fields(data, cfg)
    rows, records = _inputs(data)
    report = _new_report(queued=0, already_attached=0)
    report["warnings"] += cfg["config_warnings"]
    for resp in data.get("upsert_responses") or []:               # Upsert Airtable runs with "continue on error"
        err = (resp or {}).get("error") if isinstance(resp, dict) else None
        if err:
            report["warnings"].append(f"upsert batch failed: {_norm(err.get('message') if isinstance(err, dict) else err)[:300]}")
    _resolve_keys(cfg, fields_by_name, rows, report)
    index = _Index(list(records) + list(data.get("upsert_responses") or []), cfg, report)

    jobs = []
    for row, fields in _rows_by_key(rows, cfg, fields_by_name, index, report).values():
        record = index.find(fields)
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
            for file_id in dict.fromkeys(ids):
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
    if cfg["dry_run"]:                                            # report what would be uploaded, upload nothing
        report["dry_run_not_uploaded"] = len(jobs)
        jobs = []
    return {"attachments": jobs, "dry_run": cfg["dry_run"], "report": report}


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


def run(data: dict) -> dict:
    name = data.get("mode", "plan")
    if name not in MODES:
        raise ScriptError(f"unknown mode {name!r}, use one of {sorted(MODES)}")
    out = MODES[name](data)
    return {"mode": name, "summary": ("DRY RUN, nothing sent: " if out.get("dry_run") else "") + _summary(out["report"]), **out}


if "data" in globals():                    # python-api injects `data`
    result = run(data)                     # noqa: F821
else:                                      # local test: python3 gsheets_to_airtable.py payload.json
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    try:                                   # same shape python-api returns in "result"
        print(json.dumps({"status": "success", "result": run(payload.get("data", payload))}, indent=2, ensure_ascii=False))
    except ScriptError as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        sys.exit(1)
