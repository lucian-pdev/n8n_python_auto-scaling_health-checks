# requires: phonenumbers==9.0.41, geonamescache==3.0.2, disposable-email-domains==0.0.286
"""
spam_score.py - signup spam filter for the gsheets_to_airtable workflow (python-api).
Pure transform: no network, no secrets. n8n does all I/O (Google Sheets, Data Table, Slack);
this script decides WHAT happens to each sheet row. Called once per run via POST /execute:
    {"code_file_name": "spam_score.py",
     "data": {"mode": "score", "config": {...}, "rows": [...], "list1": [...], "list2": [...]}}

Two lists are searched before any scoring, list 1 first:
    list 1  Google Sheet tab of known spam. READ ONLY, never edited here.
    list 2  n8n data table "known_spam". This is where new spam and review requests are stored.

-> rows:           the sheet rows that may go on to the Airtable planner
-> table_writes:   rows to upsert into the data table (by match_key)
-> slack_messages: one per new decision, each sent by the Slack sub-workflow in its own execution:
                   kind "review"  (card with Spam / Not spam buttons, the answer sets `status`)
                   kind "blocked" (one line: score, name, blocked)
-> decisions:      one per row, action pass | drop | hold

What happens to a row (all numbers in Config.spam_filter, nothing is hard-coded):
    list says spam                 -> dropped, nothing sent (it was reported when it was first blocked)
    list says not_spam             -> passes, not scored
    list 2 says pending            -> held (its review card is already out)
    score >= pass_at   (75)        -> passes, nothing stored
    review_at <= score < pass_at   -> stored "pending", review card, held until a person answers
    score < review_at  (50)        -> stored "spam", one-line Slack note, dropped
A person answers a review card: Spam -> status spam, Not spam (or no answer in time) -> not_spam.

Errors: python-api returns HTTP 200 with result.status="error", result.error, result.traceback.
@reports_errors adds the mode: every exception leaves as ScriptError("<mode>: <Type>: <msg> (line N in func)").
"""
import difflib
import functools
import hashlib
import json
import re
import traceback
import unicodedata

import geonamescache
import phonenumbers
from disposable_email_domains import blocklist as DISPOSABLE_DOMAINS
from phonenumbers import geocoder

MODES = {}                                           # filled by @mode
VOWELS = set("aeiouy")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
URL_RE = re.compile(r"https?://|www\.|[a-z0-9-]+\.(com|net|org|ru|xyz|top|info|biz)/", re.I)

# Everything below can be overridden by Config.spam_filter (nested keys are merged, so a partial object is fine).
DEFAULTS = {
    "enabled": True,
    "start": 100,                    # every row starts here, each failed check deducts its points
    "pass_at": 75,                   # score >= this: passes
    "review_at": 50,                 # review_at <= score < pass_at: a person decides; below review_at: blocked as spam
    "review_title": "Gsheet 2 Airtable checkup: Possible Spam in Volunteers",
    "blocked_text": "Spam in Gsheets 2 Airtable, score {score}, Name {name}, blocked.",
    "columns": {                     # sheet headers of the signup sheet
        "first_name": "First Name", "last_name": "Last Name", "email": "Email Address",
        "phone": "Phone Number", "location_1": "Current Location", "location_2": "Country of Origin",
    },
    "list1_columns": {               # headers of the known-spam sheet tab
        "email": "Email", "phone": "Phone", "country": "Country Code", "verdict": "Human Verdict", "zone": "Spam Zone",
    },
    "list1_spam_verdicts": ["spam", "spam_banned", "spam_delete_requested"],
    "list1_ok_verdicts": ["not_spam"],
    "allowed_continents": ["EU"],                                   # geonames codes: EU AS AF NA SA OC
    "allowed_countries": ["US", "CA", "AU", "KR", "CN", "JP"],      # allowed on top of the continents
    "denied_countries": ["RU", "TR", "IL", "MX", "NZ"],             # always denied, beats both lists above
    "phone_default_region": "",                                     # ISO-2 used to read numbers typed without +country code
    "name_email_min_overlap": 0.2,                                  # share of the name's letters found in the email
    "test_names": {"enabled": False,
                   "pattern": r"\b(test|testing|testapi|deleteme|bot|spamtest)\b"},   # on = dropped, nothing stored
    "spam_keywords": ["viagra", "casino", "crypto", "bitcoin", "forex", "backlink", "porn", "whatsapp", "telegram",
                      "click here", "free money", "make money"],       # looked for in the two location fields only
    "max_field_length": 150,
    "points": {                                                     # deducted per failed check, 0 switches a check off
        # geography
        "phone_region_denied": 25,      # phone number belongs to a denied country
        "place_denied": 25,             # the place fields name only denied countries (counted once for both fields)
        "place_unresolved": 5,          # place text given, no country recognised
        "place_blank": 10,              # both place fields empty
        "geo_both_denied": 10,          # extra: phone and place agree on a denied country
        "geo_phone_place_mismatch": 5,  # phone country is not among the countries of the place fields
        # phone
        "phone_missing": 10,
        "phone_unparseable": 25,        # not a phone number at all
        "phone_not_possible": 25,       # wrong length for its country
        "phone_not_valid": 15,          # right length, not a real number range
        "phone_no_country": 5,          # typed without +country code and no region to try
        "phone_repeated_digits": 15,    # 0000000, 1234567
        # email
        "email_missing": 15,
        "email_bad_syntax": 30,
        "email_disposable": 30,
        "email_random_local": 10,
        "name_email_low_overlap": 3,
        # name
        "name_missing": 15,
        "name_bad_chars": 25,           # digits, @, link or emoji in a name
        "name_initials": 10,            # a single letter as first or last name
        "name_same": 10,                # first name = last name
        "name_gibberish": 10,           # no vowels, or a run of 6 consonants
        # content and batch
        "link_or_keyword": 20,
        "field_too_long": 15,
        "duplicate_in_batch": 15,       # same email or phone as an earlier row of this run
        "list1_unreviewed_spam": 10,    # known-spam sheet scored this contact "spam" but no person has decided yet
    },
}


# ─────────────────────────────────────────────
# error reporting (same pattern as gsheets_to_airtable.py)
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
    """Re-raise any exception as one ScriptError line prefixed with the mode."""
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


# ─────────────────────────────────────────────
# small helpers
# ─────────────────────────────────────────────
def _norm(value) -> str:
    return "" if value is None else str(value).strip()


def _fold(text) -> str:
    """Lowercase, accents removed: 'Lișiță' -> 'lisita'."""
    decomposed = unicodedata.normalize("NFKD", _norm(text).lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(text) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(text))


def _merge(base: dict, over: dict) -> dict:
    """Config over defaults, nested dicts merged key by key."""
    out = dict(base)
    for key, value in (over or {}).items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def _settings(data: dict) -> dict:
    cfg = (data.get("config") or {}).get("spam_filter") or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    s = _merge(DEFAULTS, cfg)
    if not s["review_at"] < s["pass_at"] <= s["start"]:
        raise ScriptError(f"spam_filter thresholds must satisfy review_at < pass_at <= start, got "
                          f"{s['review_at']} / {s['pass_at']} / {s['start']}")
    return s


def _items(value) -> list[dict]:
    """A list of n8n items' json; empty placeholder items ({}) are dropped."""
    if not isinstance(value, list):
        raise ScriptError(f"expected a list of rows, got {type(value).__name__}")
    return [v for v in value if isinstance(v, dict) and any(_norm(x) for x in v.values())]


# ─────────────────────────────────────────────
# places: free text -> country codes (offline)
# ─────────────────────────────────────────────
COUNTRY_ALIASES = {
    "usa": "US", "u s a": "US", "u s": "US", "america": "US", "united states of america": "US",
    "uk": "GB", "u k": "GB", "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "holland": "NL", "czechia": "CZ", "korea": "KR",
    "south korea": "KR", "republic of korea": "KR", "russia": "RU", "turkiye": "TR", "uae": "AE",
    "ivory coast": "CI", "burma": "MM", "vietnam": "VN", "macedonia": "MK", "moldova": "MD",
}
CA_PROVINCES = ["Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba", "Saskatchewan", "Nova Scotia",
                "New Brunswick", "Newfoundland and Labrador", "Prince Edward Island"]
AU_STATES = ["New South Wales", "Queensland", "South Australia", "Western Australia", "Tasmania",
             "Northern Territory", "Australian Capital Territory"]
_PLACES = None


def _key(text) -> str:
    return " ".join(_words(text))


def _build_places() -> dict:
    gc = geonamescache.GeonamesCache()
    countries = gc.get_countries()
    names = {_key(c["name"]): iso for iso, c in countries.items()}
    names.update(COUNTRY_ALIASES)
    for state in gc.get_us_states().values():
        names.setdefault(_key(state["name"]), "US")
    for name in CA_PROVINCES:
        names.setdefault(_key(name), "CA")
    for name in AU_STATES:
        names.setdefault(_key(name), "AU")
    biggest = {}                                       # city name -> (population, country): the most populous one wins
    for city in gc.get_cities().values():
        for name in [city["name"]] + [a for a in city["alternatenames"] if a.isascii()]:
            key = _key(name)
            if len(key) >= 4 and city["population"] > biggest.get(key, (-1, ""))[0]:
                biggest[key] = (city["population"], city["countrycode"])
    return {
        "names": names,
        "cities": {k: v[1] for k, v in biggest.items()},
        "codes": {**{iso.lower(): iso for iso in countries}, **{c["iso3"].lower(): iso for iso, c in countries.items()}},
        "continent": {iso: c["continentcode"] for iso, c in countries.items()},
    }


def _places_data() -> dict:
    global _PLACES
    if _PLACES is None:
        _PLACES = _build_places()
    return _PLACES


def resolve_countries(text) -> set[str]:
    """Country codes named in a free-text place ('Cluj-Napoca, Romania' -> {'RO'}).
    Country and state/province names are found anywhere in the text (1-4 word phrases). A city only counts
    when it is a whole comma-separated part ('Nice' alone, not 'somewhere nice'). A bare ISO code only
    counts when it is the whole text."""
    p = _places_data()
    words = _words(text)
    found = set()
    if len(words) == 1 and words[0] in p["codes"]:
        found.add(p["codes"][words[0]])
    for size in range(4, 0, -1):
        for start in range(len(words) - size + 1):
            iso = p["names"].get(" ".join(words[start:start + size]))
            if iso:
                found.add(iso)
    for part in re.split(r"[,;/|()\n]+", _norm(text)):
        key = _key(part)
        if key not in p["names"] and key in p["cities"]:      # 'India' is a country, not the Serbian town
            found.add(p["cities"][key])
    return found


def zone_of(iso: str, s: dict) -> str:
    """'allowed' or 'denied'. Denied countries win over every allow rule; anything not allowed is denied."""
    if iso in s["denied_countries"]:
        return "denied"
    if iso in s["allowed_countries"] or _places_data()["continent"].get(iso) in s["allowed_continents"]:
        return "allowed"
    return "denied"


def classify(countries: set[str], s: dict) -> str:
    """'allowed' if any country is allowed ('Atlanta, Georgia' is the US state), else 'denied', else 'unknown'."""
    zones = {zone_of(iso, s) for iso in countries}
    return "allowed" if "allowed" in zones else "denied" if zones else "unknown"


# ─────────────────────────────────────────────
# phone
# ─────────────────────────────────────────────
def parse_phone(raw, region_hints: list[str]) -> dict:
    """{'state': ..., 'region', 'e164', 'place'}. state is one of: missing, unparseable, no_country,
    not_possible, not_valid, ok. Numbers without +country code are tried against the region hints in order."""
    raw = _norm(raw)
    if not raw:
        return {"state": "missing", "region": "", "e164": "", "place": ""}
    text = "+" + raw[2:] if raw.startswith("00") else raw
    regions = [None] if text.startswith("+") else [r for r in region_hints if r]
    if not regions:
        return {"state": "no_country", "region": "", "e164": "", "place": ""}
    parsed = []
    for region in regions:
        try:
            parsed.append(phonenumbers.parse(text, region))
        except phonenumbers.NumberParseException:
            continue
    if not parsed:
        return {"state": "unparseable", "region": "", "e164": "", "place": ""}
    number = next((n for n in parsed if phonenumbers.is_possible_number(n)), parsed[0])
    region = phonenumbers.region_code_for_number(number) or ""
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    place = geocoder.description_for_number(number, "en")
    if not phonenumbers.is_possible_number(number):
        state = "not_possible"
    elif not phonenumbers.is_valid_number(number):
        state = "not_valid"
    else:
        state = "ok"
    return {"state": state, "region": region, "e164": e164, "place": place}


def _repeated_digits(raw) -> bool:
    digits = re.sub(r"\D", "", _norm(raw))
    if len(digits) < 6:
        return False
    run = digits[-7:]
    steps = {int(b) - int(a) for a, b in zip(run, run[1:])}
    return len(set(digits[-8:])) <= 2 or steps in ({1}, {-1})


# ─────────────────────────────────────────────
# name / email checks
# ─────────────────────────────────────────────
def _is_latin(text) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(ch, "") for ch in letters)


def _gibberish(name) -> bool:
    for token in _words(name):
        if not _is_latin(token) or len(token) < 4:
            continue
        if not VOWELS & set(token) or re.search(r"[^aeiouy0-9]{6,}", token):
            return True
    return False


def _bad_chars(name) -> bool:
    text = _norm(name)
    return bool(re.search(r"\d|@", text) or URL_RE.search(text)
                or any(ord(ch) >= 0x1F000 or unicodedata.category(ch) == "So" for ch in text))


def _initials(name) -> bool:
    letters = re.sub(r"[^a-z]", "", _fold(name))
    return len(letters) == 1


def name_email_overlap(first, last, email) -> float | None:
    """Share of the name's letters that appear in the email's local part (None when there is nothing to compare)."""
    local = re.sub(r"[^a-z]", "", _fold(_norm(email).split("@")[0]))
    tokens = [re.sub(r"[^a-z]", "", t) for t in (_fold(first), _fold(last))]
    tokens = [t for t in tokens if len(t) >= 2]
    if not local or not tokens:
        return None
    matched = 0
    for token in tokens:
        match = difflib.SequenceMatcher(None, local, token).find_longest_match(0, len(local), 0, len(token))
        matched += match.size if match.size >= 2 else 0
    return matched / sum(len(t) for t in tokens)


def _random_local(email) -> bool:
    local = re.sub(r"[^a-z0-9]", "", _fold(_norm(email).split("@")[0]))
    if len(local) < 6:
        return False
    digits = sum(ch.isdigit() for ch in local)
    letters = re.sub(r"\d", "", local)
    return digits / len(local) >= 0.5 or (len(letters) >= 8 and (not VOWELS & set(letters)
                                                                 or bool(re.search(r"[^aeiouy]{6,}", letters))))


# ─────────────────────────────────────────────
# the score of one row
# ─────────────────────────────────────────────
def _fields(row: dict, s: dict) -> dict:
    return {name: _norm(row.get(header)) for name, header in s["columns"].items()}


def _match_key(email_norm: str, phone_e164: str, first: str, last: str) -> str:
    basis = f"{email_norm}|{phone_e164}" if email_norm or phone_e164 else f"name|{_fold(first)} {_fold(last)}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def evaluate(row: dict, s: dict, seen: set, unreviewed_contacts: set) -> dict:
    """Score one row. Returns the facts and the deductions that made the score."""
    f = _fields(row, s)
    pts = s["points"]
    deductions = []

    def deduct(check: str, detail: str = ""):
        if pts.get(check):
            deductions.append({"check": check, "points": pts[check], "detail": detail})

    email = _norm(f["email"]).lower()
    place_countries = resolve_countries(f["location_1"]) | resolve_countries(f["location_2"])
    place_zone = classify(place_countries, s)
    phone = parse_phone(f["phone"], sorted(place_countries) + [s["phone_default_region"]])
    phone_zone = classify({phone["region"]}, s) if phone["region"] else "unknown"
    key = _match_key(email, phone["e164"], f["first_name"], f["last_name"])

    # geography
    if phone_zone == "denied":
        deduct("phone_region_denied", phone["region"])
    if place_zone == "denied":
        deduct("place_denied", ",".join(sorted(place_countries)))
    elif place_zone == "unknown":
        if f["location_1"] or f["location_2"]:
            deduct("place_unresolved", f"{f['location_1']} / {f['location_2']}"[:80])
        else:
            deduct("place_blank")
    if phone_zone == "denied" and place_zone == "denied":
        deduct("geo_both_denied")
    if phone["region"] and place_countries and phone["region"] not in place_countries:
        deduct("geo_phone_place_mismatch", f"{phone['region']} vs {','.join(sorted(place_countries))}")
    # phone
    state = phone["state"]
    if state == "missing":
        deduct("phone_missing")
    elif state in ("unparseable", "no_country", "not_possible", "not_valid"):
        deduct("phone_" + state)
    if state != "missing" and _repeated_digits(f["phone"]):
        deduct("phone_repeated_digits")
    # email
    if not email:
        deduct("email_missing")
    elif not EMAIL_RE.match(email):
        deduct("email_bad_syntax")
    else:
        if email.rsplit("@", 1)[1] in DISPOSABLE_DOMAINS:
            deduct("email_disposable", email.rsplit("@", 1)[1])
        if _random_local(email):
            deduct("email_random_local")
    # name
    names = [f["first_name"], f["last_name"]]
    if not all(names):
        deduct("name_missing")
    if any(_bad_chars(n) for n in names):
        deduct("name_bad_chars")
    if any(_initials(n) for n in names if n):
        deduct("name_initials")
    if all(names) and _fold(names[0]) == _fold(names[1]):
        deduct("name_same")
    if any(_gibberish(n) for n in names):
        deduct("name_gibberish")
    overlap = name_email_overlap(f["first_name"], f["last_name"], email) if EMAIL_RE.match(email) else None
    if overlap is not None and overlap < s["name_email_min_overlap"]:
        deduct("name_email_low_overlap", f"{overlap:.0%}")
    # content
    if any(URL_RE.search(v) for v in f.values()) or any(
            re.search(rf"\b{re.escape(k)}\b", _fold(f"{f['location_1']} {f['location_2']}")) for k in s["spam_keywords"]):
        deduct("link_or_keyword")
    if any(len(v) > s["max_field_length"] for v in f.values()):
        deduct("field_too_long")
    # batch and list hints
    contacts = {c for c in (email, phone["e164"]) if c}
    if contacts & seen:
        deduct("duplicate_in_batch")
    if contacts & unreviewed_contacts:
        deduct("list1_unreviewed_spam")
    seen |= contacts

    score = max(0, s["start"] - sum(d["points"] for d in deductions))
    return {
        "match_key": key, "email_norm": email, "phone_e164": phone["e164"], "phone_region": phone["region"],
        "phone_place": phone["place"], "location_country": ",".join(sorted(place_countries)),
        "score": score, "deductions": deductions,
        "flags": [d["check"] + (f":{d['detail']}" if d["detail"] and d["check"] in FLAG_DETAIL else "") for d in deductions],
        "fields": f,
    }


FLAG_DETAIL = {"phone_region_denied", "place_denied", "email_disposable", "geo_phone_place_mismatch"}


# ─────────────────────────────────────────────
# the two lists
# ─────────────────────────────────────────────
class _Lists:
    """Lookup of a row's contacts in list 1 (sheet) and list 2 (data table)."""

    def __init__(self, list1: list[dict], list2: list[dict], s: dict, report: dict):
        self.s = s
        c1 = s["list1_columns"]
        if list1 and c1["verdict"] not in list1[0]:
            report["warnings"].append(f"known-spam sheet has no column {c1['verdict']!r}: list 1 only counts as hints")
        self.by_contact = {1: {}, 2: {}}     # list number -> contact -> verdict string
        self.unreviewed = set()
        for entry in list1:
            verdict = _fold(entry.get(c1["verdict"])).replace(" ", "_")
            for contact in self._contacts1(entry, c1):
                self.by_contact[1].setdefault(contact, set()).add(verdict)
                if not verdict and _fold(entry.get(c1["zone"])) == "spam":
                    self.unreviewed.add(contact)
        for entry in list2:
            status = _fold(entry.get("status"))
            for contact in (entry.get("match_key"), _norm(entry.get("email_norm")).lower(), _norm(entry.get("phone_e164"))):
                if contact:
                    self.by_contact[2].setdefault(contact, set()).add(status)

    @staticmethod
    def _contacts1(entry: dict, c1: dict) -> list[str]:
        email = _norm(entry.get(c1["email"])).lower()
        hint = _norm(entry.get(c1["country"])).upper()
        phone = parse_phone(entry.get(c1["phone"]), [hint]) if _norm(entry.get(c1["phone"])) else {"e164": ""}
        return [c for c in (email, phone["e164"]) if c]

    def lookup(self, facts: dict) -> tuple[str, str] | None:
        """(action, reason) for the first list that knows this row: spam beats not_spam beats pending."""
        s = self.s
        contacts = [c for c in (facts["match_key"], facts["email_norm"], facts["phone_e164"]) if c]
        for number in (1, 2):
            verdicts = set().union(*(self.by_contact[number].get(c, set()) for c in contacts)) if contacts else set()
            spam = {"spam"} | set(s["list1_spam_verdicts"]) if number == 1 else {"spam"}
            ok = set(s["list1_ok_verdicts"]) if number == 1 else {"not_spam"}
            if verdicts & spam:
                return "drop", f"list{number}:spam"
            if verdicts & ok:
                return "pass", f"list{number}:not_spam"
            if number == 2 and "pending" in verdicts:
                return "hold", "list2:pending"
        return None


# ─────────────────────────────────────────────
# mode
# ─────────────────────────────────────────────
def _count(decisions: list[dict]) -> dict:
    counts = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    return counts


def _summary(report: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())) or "no rows"


def _slack_safe(text) -> str:
    """Typed text inside a Slack message: no formatting or mentions of its own."""
    return _norm(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("*", "")


def _table_row(d: dict, s: dict, status: str) -> dict:
    zone = "clear" if d["score"] >= s["pass_at"] else "edge" if d["score"] >= s["review_at"] else "spam"
    f = d["fields"]
    return {
        "match_key": d["match_key"], "email_norm": d["email_norm"], "phone_e164": d["phone_e164"],
        "first_name": f["first_name"], "last_name": f["last_name"], "location_1": f["location_1"],
        "location_2": f["location_2"], "location_country": d["location_country"],
        "spam_score": str(s["start"] - d["score"]), "zone": zone, "flags": ", ".join(d["flags"]),
        "status": status, "source": "rules", "sheet_row": str(d["row_number"]),
    }


def _mentions(ids) -> str:
    """Config.spam_review_mention_ids -> Slack mentions: U... = a person, S... = a user group."""
    if isinstance(ids, str):
        ids = json.loads(ids) if ids.strip().startswith("[") else ids.replace(",", " ").split()
    out = []
    for i in ids or []:
        i = _norm(i)
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", i):
            out.append(f"<@{i}>")
        elif re.fullmatch(r"S[A-Z0-9]{6,}", i):
            out.append(f"<!subteam^{i}>")
        elif i:
            raise ScriptError(f"spam_review_mention_ids: {i!r} is not a Slack member (U...) or group (S...) id")
    return " ".join(out)


def _message(kind: str, d: dict, s: dict, config: dict) -> dict:
    f = d["fields"]
    name = _slack_safe(f"{f['first_name']} {f['last_name']}") or "(no name)"
    if kind == "review":            # every line bold; mentions on their own line so Slack notifies them
        lines = [s["review_title"], _mentions(config.get("spam_review_mention_ids")),
                 f"Volunteer name: {name} Email: {_slack_safe(f['email']) or '-'} Phone: {_slack_safe(f['phone']) or '-'}"]
        text = "\n".join(f"*{line.strip()}*" for line in lines if _norm(line))
    else:
        text = s["blocked_text"].format(score=d["score"], name=name)
    return {"kind": kind, "text": text, "match_key": d["match_key"], "row_number": d["row_number"],
            "channel": config.get("spam_slack_channel", ""), "table_id": config.get("spam_table_id", "")}


@mode("score")
@reports_errors
def score(data: dict) -> dict:
    s = _settings(data)
    config = data.get("config") or {}
    dry_run = bool(config.get("dry_run"))
    rows = [r for r in data.get("rows") or [] if isinstance(r, dict)]
    report = {"counts": {}, "warnings": [], "reasons": {}}
    if not s["enabled"]:
        decisions = [{"row_number": r.get("row_number"), "action": "pass", "reason": "spam_filter disabled"} for r in rows]
        report["counts"] = _count(decisions)
        return {"rows": rows, "table_writes": [], "slack_messages": [], "decisions": decisions, "dry_run": dry_run,
                "report": report}
    if rows:
        missing = [h for h in s["columns"].values() if not any(h in r for r in rows)]
        if missing:
            report["warnings"].append(f"sheet columns not found, treated as blank: {missing} (set spam_filter.columns)")
    lists = _Lists(_items(data.get("list1") or []), _items(data.get("list2") or []), s, report)
    test_re = re.compile(s["test_names"]["pattern"], re.I) if s["test_names"]["enabled"] else None
    seen, decisions, writes, messages, passed = set(), [], [], [], set()
    for row in rows:
        if not any(_norm(v) for v in _fields(row, s).values()):          # blank row: the planner skips it, pass it on
            decisions.append({"row_number": row.get("row_number"), "action": "pass", "reason": "empty_row"})
            passed.add(row.get("row_number"))
            continue
        facts = evaluate(row, s, seen, lists.unreviewed)
        name = f"{facts['fields']['first_name']} {facts['fields']['last_name']}"
        d = {"row_number": row.get("row_number"), "match_key": facts["match_key"], "fields": facts["fields"],
             "email_norm": facts["email_norm"], "phone_e164": facts["phone_e164"], "phone_region": facts["phone_region"],
             "location_country": facts["location_country"], "score": facts["score"], "flags": facts["flags"],
             "deductions": facts["deductions"]}
        hit = lists.lookup(facts)
        if hit:                                     # known: no new message, nothing written
            action, reason = hit
        elif test_re and test_re.search(name):
            action, reason = "drop", "test_name"
        elif facts["score"] >= s["pass_at"]:
            action, reason = "pass", "score"
        elif facts["score"] >= s["review_at"]:
            action, reason = "hold", "score:review"
            writes.append(_table_row(d, s, "pending"))
            messages.append(_message("review", d, s, config))
        else:
            action, reason = "drop", "score:spam"
            writes.append(_table_row(d, s, "spam"))
            messages.append(_message("blocked", d, s, config))
        if action == "pass":
            passed.add(d["row_number"])
        decisions.append({**d, "action": action, "reason": reason})
    report["counts"] = _count(decisions)
    for d in decisions:
        report["reasons"][d["reason"]] = report["reasons"].get(d["reason"], 0) + 1
    if dry_run:                                                # report what would be stored, store and send nothing
        report["dry_run_not_written"] = {"table_writes": len(writes), "slack_messages": len(messages)}
        writes, messages = [], []
    return {"rows": [r for r in rows if r.get("row_number") in passed], "table_writes": writes,
            "slack_messages": messages, "decisions": decisions, "dry_run": dry_run, "report": report}


def run(data: dict) -> dict:
    name = data.get("mode", "score")
    if name not in MODES:
        raise ScriptError(f"unknown mode {name!r}, use one of {sorted(MODES)}")
    out = MODES[name](data)
    return {"mode": name, "summary": ("DRY RUN, nothing stored: " if out.get("dry_run") else "") + _summary(out["report"]),
            **out}


if "data" in globals():                    # python-api injects `data`
    result = run(data)                     # noqa: F821
elif __name__ == "__main__":               # local test: python3 spam_score.py payload.json
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    try:                                   # same shape python-api returns in "result"
        print(json.dumps({"status": "success", "result": run(payload.get("data", payload))}, indent=2, ensure_ascii=False))
    except ScriptError as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        sys.exit(1)
