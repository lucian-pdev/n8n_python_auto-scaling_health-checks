# requires: phonenumbers
"""
standardise_phone.py  —  phonenumbers-based name + phone normaliser for the
`python-api` microservice (github.com/lucian-pdev/n8n_python_auto-scaling_health-checks).

Deploy: commit this file to the service's scripts repo so `GET /scripts` lists it,
then call it via `POST /execute` with body:
    { "data": { ...attributes... }, "code_file_name": "standardise_phone.py" }

The runner injects the request payload as a global `data` and reads back a global
`result`. Input `data` is a flat dict built by the n8n workflow:
    { first_name, middle_name, last_name, mobile_number, phone_number, country_code, zip }

Output `result` mirrors the JS normalizer's fields so it is a drop-in for the PATCH:
    { first_name, middle_name, last_name,
      mobile_raw, mobile_number, mobile_e164, mobile_valid,
      phone_country, phone_type, phone_country_source, phone_flags }

This is the "library" side of the JS-vs-phonenumbers comparison: region RESOLUTION
(RUF-priority + trusted address country + NANP shape) stays here as business logic,
but PARSE / VALIDATE / FORMAT / number-typing are delegated to libphonenumber.
"""
import re
import phonenumbers as pn
from phonenumbers import PhoneNumberType as T

# RUF base first (Romania / Moldova / Ukraine / EU / UK), diaspora next, spam-risk last.
# Used to disambiguate a bare national number that is valid in more than one country.
PRIORITY = [
    'RO', 'MD', 'UA', 'GB', 'IE', 'DE', 'FR', 'IT', 'ES', 'NL', 'BE', 'AT', 'CH',
    'PL', 'HU', 'CZ', 'SK', 'HR', 'SE', 'NO', 'DK', 'FI', 'PT', 'GR', 'BG',
    'US', 'CA', 'AU',
    'NG', 'IN', 'PK', 'LK', 'BD', 'NP', 'EG', 'DZ', 'MA', 'KE', 'GH', 'ZA',
    'SA', 'IR', 'TR', 'IL',
]

_MOBILEISH = (T.MOBILE, T.FIXED_LINE_OR_MOBILE)
_NANP = re.compile(r'^1?[2-9]\d{2}[2-9]\d{6}$')


def title_case(v):
    """Unicode-aware: capitalise the first letter of each word (start or after space/-/'/.)."""
    if not v:
        return v
    return re.sub(r"(^|[\s\-'.])(\w)", lambda m: m.group(1) + m.group(2).upper(), str(v).lower(), flags=re.UNICODE)


def _type_name(p):
    return {T.MOBILE: 'mobile', T.FIXED_LINE: 'fixed_line',
            T.FIXED_LINE_OR_MOBILE: 'fixed_or_mobile'}.get(pn.number_type(p), 'other')


def resolve_phone(raw, stated_country):
    """Return dict: country, e164, valid, ptype, source, flags. Never raises.

    (zip is unused: libphonenumber validates against the region directly, so the
    JS zip-heuristic layer isn't needed here.)"""
    raw = (raw or '').strip()
    if not raw:
        return dict(country=None, e164=None, valid=None, ptype=None,
                    source='unknown', flags=['phone_missing'])

    digits = re.sub(r'\D', '', raw)
    st = (stated_country or '').upper()

    # Layer 1: explicitly international (+ or 00) — region-independent, authoritative.
    if raw.startswith('+') or raw.startswith('00'):
        candidate = raw if raw.startswith('+') else '+' + digits
        try:
            p = pn.parse(candidate, None)
            valid = pn.is_valid_number(p)
            return dict(country=pn.region_code_for_number(p),
                        e164=pn.format_number(p, pn.PhoneNumberFormat.E164),
                        valid=valid, ptype=_type_name(p),
                        source='phone', flags=[] if valid else ['phone_invalid'])
        except Exception:
            return dict(country=None, e164=None, valid=False, ptype=None,
                        source='unknown', flags=['phone_unparseable'])

    # Layer 2/3: bare national number — build a prioritised candidate-region list.
    order = []
    if st and st not in ('US', 'CA'):            # trust a real (non-default) stated country first
        order.append(st)
    if st in ('US', 'CA', '') and _NANP.match(digits):   # bare NANP shape → try US early
        order.append('US')
    for r in PRIORITY:
        if r not in order:
            order.append(r)

    for region in order:
        try:
            p = pn.parse(raw, region)
            if pn.is_valid_number(p) and pn.number_type(p) in _MOBILEISH:
                src = 'address' if region == st else 'phone'
                return dict(country=region,
                            e164=pn.format_number(p, pn.PhoneNumberFormat.E164),
                            valid=True, ptype=_type_name(p), source=src, flags=[])
        except Exception:
            continue

    # Undetermined — keep raw, do NOT fabricate a "+digits".
    return dict(country=None, e164=None, valid=None, ptype=None,
                source='unknown', flags=['phone_country_unknown'])


def standardise(d):
    d = d or {}
    raw = d.get('mobile_number') or ''
    sourced_from_phone = False
    if not str(raw).strip():
        raw = d.get('phone_number') or ''
        sourced_from_phone = bool(str(raw).strip())

    r = resolve_phone(raw, d.get('country_code'))

    return {
        'first_name':  title_case(d.get('first_name')),
        'middle_name': title_case(d.get('middle_name')),
        'last_name':   title_case(d.get('last_name')),
        'mobile_raw':  (raw or None),
        # Only write a normalised number when the library validated it; else keep raw.
        'mobile_number': r['e164'] if r['valid'] else (raw or None),
        'mobile_e164':  r['e164'],
        'mobile_valid': r['valid'],
        'phone_country': r['country'],
        'phone_type': r['ptype'],
        'phone_country_source': r['source'],
        'phone_flags': r['flags'],
        'phone_sourced_from_phone_field': sourced_from_phone,
        'engine': 'phonenumbers/' + pn.__version__,
    }


# --- microservice entrypoint: read global `data`, set global `result` ---
# try:
result = standardise(data)          # noqa: F821  (`data` injected by the runner)
# except Exception as e:                  # never crash the worker; surface the error
    # result = {'error': str(e), 'engine': 'phonenumbers'}


# --- local self-test: `python3 standardise_phone.py` (no runner needed) ---
if __name__ == '__main__':
    import json
    cases = [
        {'first_name': 'ROMITA', 'last_name': 'iucu', 'mobile_number': '0723188897', 'country_code': 'US'},
        {'first_name': 'shindara', 'last_name': 'OPE-shodunke', 'mobile_number': '07759063797', 'country_code': 'US'},
        {'last_name': 'x', 'mobile_number': '0169791627', 'country_code': 'BJ'},      # Benin
        {'last_name': 'x', 'mobile_number': '03369682901', 'country_code': 'US'},     # Pakistan
        {'last_name': 'x', 'mobile_number': '7735608488', 'country_code': 'US'},      # Chicago NANP
        {'last_name': 'x', 'mobile_number': '08031234567', 'country_code': 'US'},     # Nigeria
    ]
    for c in cases:
        print(c.get('mobile_number'), '->', json.dumps(standardise(c)))
