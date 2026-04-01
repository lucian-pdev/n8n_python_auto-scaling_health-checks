#!/usr/bin/env python3

country_calling_codes = {
    'AL': '355', 'AD': '376', 'AT': '43', 'BE': '32', 'BG': '359', 'HR': '385', 'CY': '357',
    'CZ': '420', 'DK': '45', 'EE': '372', 'FI': '358', 'FR': '33', 'DE': '49', 'GR': '30',
    'HU': '36', 'IE': '353', 'IT': '39', 'LV': '371', 'LT': '370', 'LU': '352', 'MT': '356',
    'NL': '31', 'NO': '47', 'PL': '48', 'PT': '351', 'RO': '40', 'RU': '7', 'SK': '421',
    'SI': '386', 'ES': '34', 'SE': '46', 'CH': '41', 'GB': '44',
    'US': '1', 'CA': '1', 'MX': '52',
    'AR': '54', 'BO': '591', 'BR': '55', 'CL': '56', 'CO': '57', 'EC': '593', 'PY': '595',
    'PE': '51', 'UY': '598', 'VE': '58',
    'CN': '86', 'JP': '81', 'IN': '91', 'ID': '62', 'KR': '82', 'TH': '66', 'PH': '63',
    'SG': '65', 'MY': '60', 'VN': '84', 'IL': '972', 'TR': '90', 'SA': '966', 'IR': '98',
    'DZ': '213', 'EG': '20', 'NG': '234', 'ZA': '27', 'MA': '212', 'KE': '254', 'GH': '233',
    'ET': '251'
}

def normalize_mobile(mobile, country_code):
    if not mobile:
        return mobile

    s = str(mobile).strip()

    # Already normalized
    if s.startswith('+'):
        digits = ''.join(filter(str.isdigit, s))
        return '+' + digits

    # Digits only
    digits = ''.join(filter(str.isdigit, s))
    if not digits:
        return mobile

    # Remove international prefix like 0040...
    if digits.startswith('00'):
        digits = digits[2:]

    country = (str(country_code) or "").upper()
    dialing_code = country_calling_codes.get(country)

    if not dialing_code:
        return '+' + digits

    # Case: digits already start with country code
    if digits.startswith(dialing_code):
        return '+' + digits

    # Remove trunk zero if present
    if digits.startswith('0'):
        digits = digits[1:]

    return '+' + dialing_code + digits

# Process the data from n8n
# Expected structure: data comes from webhook payload
new_items = []

# n8n sends the webhook body directly in data
# Common n8n pattern: body is in data["body"] or directly in data
items = data.get("items", [data]) if isinstance(data, dict) else [{"json": {"body": {"payload": data}}}]

for item in items:
    try:
        # Handle different n8n payload structures
        if "json" in item:
            person = item["json"].get("body", {}).get("payload", {}).get("person", {})
        else:
            # Direct payload structure
            person = item.get("person", item)

        # Title-case names (handle None/empty)
        first_name = str(person.get("first_name") or "").strip()
        middle_name = str(person.get("middle_name") or "").strip()
        last_name = str(person.get("last_name") or "").strip()

        person["first_name"] = first_name.title() if first_name else ""
        person["middle_name"] = middle_name.title() if middle_name else ""
        person["last_name"] = last_name.title() if last_name else ""

        mobile = person.get("mobile")
        phone = person.get("phone")
        country_code = person.get("primary_address", {}).get("country_code", "")

        # Use phone if mobile is empty
        if not mobile or str(mobile).strip() == "":
            mobile = phone

        person["mobile"] = normalize_mobile(mobile, country_code)

        # Update back in item structure
        if "json" in item and "body" in item["json"] and "payload" in item["json"]["body"]:
            item["json"]["body"]["payload"]["person"] = person

    except Exception:
        # Silently continue on individual item errors
        pass

    new_items.append(item)

# MUST define result variable - this is returned to n8n
result = new_items