"""Tests for spam_score.py. Run: python -m pytest tests/ (venv with phonenumbers, geonamescache, disposable-email-domains, pytest)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import spam_score as sp  # noqa: E402

GOOD = {"First Name": "Ioana", "Last Name": "Popescu", "Email Address": "ioana.popescu@gmail.com",
        "Phone Number": "+40 722 918 364", "Current Location": "Cluj-Napoca, Romania", "Country of Origin": "Romania"}


def row(n=2, **over):
    return {"row_number": n, **GOOD, **over}


def cfg(**spam):
    return {"config": {"spam_filter": spam}}


def score_rows(rows, list1=None, list2=None, **spam):
    return sp.run({"mode": "score", **cfg(**spam), "rows": rows, "list1": list1 or [], "list2": list2 or []})


def checks(result, i=0):
    return {d["check"] for d in result["decisions"][i]["deductions"]}


# ── places ──
@pytest.mark.parametrize("text,expected", [
    ("Cluj-Napoca, Romania", {"RO"}), ("Bucuresti", {"RO"}), ("Lișiță, Iași", {"RO"}), ("Seoul", {"KR"}),
    ("Delhi, India", {"IN"}), ("USA", {"US"}), ("RO", {"RO"}), ("Toronto, Ontario", {"CA"}),
    ("Mexico City", {"MX"}), ("", set()), ("asdf qwer", set())])
def test_resolve_countries(text, expected):
    assert expected <= sp.resolve_countries(text) if expected else not sp.resolve_countries(text)


def test_atlanta_georgia_is_allowed_because_one_place_is_allowed():
    s = sp._settings({})
    assert sp.classify(sp.resolve_countries("Atlanta, Georgia"), s) == "allowed"


@pytest.mark.parametrize("iso,zone", [
    ("RO", "allowed"), ("DE", "allowed"), ("US", "allowed"), ("KR", "allowed"), ("CN", "allowed"), ("JP", "allowed"),
    ("AU", "allowed"), ("CA", "allowed"), ("IN", "denied"), ("NG", "denied"), ("BR", "denied"), ("TH", "denied"),
    ("RU", "denied"), ("TR", "denied"), ("IL", "denied"), ("MX", "denied"), ("NZ", "denied"), ("GE", "denied")])
def test_zones(iso, zone):
    assert sp.zone_of(iso, sp._settings({})) == zone


# ── phone ──
def test_phone_international_and_national():
    assert sp.parse_phone("+40 721 234 567", [])["state"] == "ok"
    assert sp.parse_phone("0721234567", ["RO"])["e164"] == "+40721234567"
    assert sp.parse_phone("0721234567", [])["state"] == "no_country"
    assert sp.parse_phone("hello", ["RO"])["state"] == "unparseable"
    assert sp.parse_phone("", [])["state"] == "missing"
    assert sp.parse_phone("0040721234567", [])["region"] == "RO"


def test_repeated_digits():
    assert sp._repeated_digits("1234567") and sp._repeated_digits("0000000000")
    assert not sp._repeated_digits("+40 722 918 364")


# ── scoring ──
def test_clean_row_scores_100_and_passes():
    out = score_rows([row()])
    d = out["decisions"][0]
    assert (d["score"], d["action"], d["deductions"]) == (100, "pass", [])


def test_diacritics_are_not_penalised():
    d = score_rows([row(**{"First Name": "Ștefan", "Last Name": "Lișiță", "Email Address": "stefan.lisita@gmail.com"})])["decisions"][0]
    assert d["score"] == 100


def test_denied_phone_and_place_stack():
    d = score_rows([row(**{"Phone Number": "+91 98216 40735", "Current Location": "Delhi", "Country of Origin": "India"})])["decisions"][0]
    assert {"phone_region_denied", "place_denied", "geo_both_denied"} <= {x["check"] for x in d["deductions"]}
    assert d["score"] == 100 - 25 - 25 - 10 and d["action"] == "drop"      # below review_at: blocked


def test_one_allowed_place_cancels_a_denied_one():
    d = score_rows([row(**{"Current Location": "Berlin, Germany", "Country of Origin": "India"})])["decisions"][0]
    assert "place_denied" not in {x["check"] for x in d["deductions"]}


def test_place_blank_and_unresolved():
    assert "place_blank" in checks(score_rows([row(**{"Current Location": "", "Country of Origin": ""})]))
    assert "place_unresolved" in checks(score_rows([row(**{"Current Location": "somewhere nice", "Country of Origin": ""})]))


def test_phone_country_mismatch_is_small():
    d = score_rows([row(**{"Phone Number": "+1 212 555 0100", "Current Location": "Romania"})])["decisions"][0]
    assert "geo_phone_place_mismatch" in {x["check"] for x in d["deductions"]}


def test_email_checks():
    assert "email_disposable" in checks(score_rows([row(**{"Email Address": "ioana.popescu@mailinator.com"})]))
    assert "email_bad_syntax" in checks(score_rows([row(**{"Email Address": "not-an-email"})]))
    assert "email_missing" in checks(score_rows([row(**{"Email Address": ""})]))
    assert "email_random_local" in checks(score_rows([row(**{"Email Address": "x8372649182@gmail.com"})]))


def test_name_checks():
    assert "name_bad_chars" in checks(score_rows([row(**{"First Name": "Buy now http://x.com"})]))
    assert "name_bad_chars" in checks(score_rows([row(**{"Last Name": "Pop3scu"})]))
    assert "name_initials" in checks(score_rows([row(**{"First Name": "J"})]))
    assert "name_same" in checks(score_rows([row(**{"Last Name": "Ioana"})]))
    assert "name_gibberish" in checks(score_rows([row(**{"First Name": "Qwrtzxcvbn"})]))
    assert "name_missing" in checks(score_rows([row(**{"Last Name": ""})]))


def test_name_email_overlap_is_a_small_deduction():
    assert sp.name_email_overlap("Ioana", "Popescu", "ioana.popescu@x.com") == 1
    assert sp.name_email_overlap("Ioana", "Popescu", "jpopescu@x.com") >= 0.2
    d = score_rows([row(**{"Email Address": "gardener77@gmail.com"})])["decisions"][0]
    low = [x for x in d["deductions"] if x["check"] == "name_email_low_overlap"]
    assert low and low[0]["points"] == 3


def test_content_checks():
    assert "link_or_keyword" in checks(score_rows([row(**{"Current Location": "visit www.cheap.xyz/now"})]))
    assert "link_or_keyword" in checks(score_rows([row(**{"Country of Origin": "crypto casino"})]))
    assert "link_or_keyword" not in checks(score_rows([row(**{"First Name": "Seo", "Last Name": "Yun", "Email Address": "seo.yun@gmail.com"})]))
    assert "field_too_long" in checks(score_rows([row(**{"Current Location": "Romania " * 40})]))


def test_duplicate_in_batch_only_hits_later_rows():
    out = score_rows([row(2), row(3, **{"First Name": "Other"})])
    assert "duplicate_in_batch" not in checks(out, 0) and "duplicate_in_batch" in checks(out, 1)


def test_points_zero_switches_a_check_off():
    out = score_rows([row(**{"Email Address": ""})], points={"email_missing": 0})
    assert "email_missing" not in checks(out)


def test_thresholds_are_config():
    r = row(**{"Phone Number": "+91 98216 40735"})                      # 70
    assert score_rows([r])["decisions"][0]["action"] == "hold"
    assert score_rows([r], pass_at=60, review_at=40)["decisions"][0]["action"] == "pass"
    assert score_rows([r], pass_at=90, review_at=80)["decisions"][0]["action"] == "drop"
    with pytest.raises(sp.ScriptError, match="thresholds"):
        score_rows([r], pass_at=40, review_at=50)


def test_start_value_is_config():
    assert score_rows([row()], start=200, pass_at=150, review_at=100)["decisions"][0]["score"] == 200


def test_blank_row_passes_unscored_and_disabled_passes_everything():
    blank = {"row_number": 5}
    assert score_rows([blank])["decisions"][0]["reason"] == "empty_row"
    out = score_rows([row(**{"Email Address": "bad"})], enabled=False)
    assert out["decisions"][0]["reason"] == "spam_filter disabled" and out["slack_messages"] == []


def test_test_name_override_is_off_by_default():
    r = row(**{"First Name": "Angela", "Last Name": "TEST"})
    assert score_rows([r])["decisions"][0]["action"] == "pass"
    assert score_rows([r], test_names={"enabled": True})["decisions"][0]["reason"] == "test_name"


def test_missing_sheet_column_warns():
    out = score_rows([{"row_number": 2, "First Name": "A"}])
    assert any("columns not found" in w for w in out["report"]["warnings"])


# ── lists ──
L1 = {"Email": "bad@x.com", "Phone": "", "Country Code": "", "Human Verdict": "spam"}


def test_list1_spam_drops_without_scoring_by_email_or_phone():
    assert score_rows([row(**{"Email Address": "BAD@x.com "})], list1=[L1])["decisions"][0]["reason"] == "list1:spam"
    by_phone = {"Email": "", "Phone": "0722918364", "Country Code": "RO", "Human Verdict": "spam_banned"}
    assert score_rows([row()], list1=[by_phone])["decisions"][0]["action"] == "drop"


def test_list1_not_spam_passes_even_if_row_is_bad():
    ok = {"Email": "bad@x.com", "Human Verdict": "not_spam"}
    d = score_rows([row(**{"Email Address": "bad@x.com", "Phone Number": "+91 98216 40735", "Current Location": "Delhi"})], list1=[ok])["decisions"][0]
    assert (d["action"], d["reason"]) == ("pass", "list1:not_spam")


def test_spam_beats_not_spam_and_list1_beats_list2():
    both = [{"Email": "bad@x.com", "Human Verdict": "not_spam"}, {"Phone": "+40722918364", "Human Verdict": "spam"}]
    assert score_rows([row(**{"Email Address": "bad@x.com"})], list1=both)["decisions"][0]["action"] == "drop"
    l2 = [{"email_norm": "bad@x.com", "status": "not_spam"}]
    d = score_rows([row(**{"Email Address": "bad@x.com"})], list1=[L1], list2=l2)["decisions"][0]
    assert d["reason"] == "list1:spam"


def test_list2_statuses():
    key = score_rows([row()])["decisions"][0]["match_key"]
    for status, action in (("spam", "drop"), ("not_spam", "pass"), ("pending", "hold")):
        d = score_rows([row()], list2=[{"match_key": key, "status": status}])["decisions"][0]
        assert (d["action"], d["reason"]) == (action, f"list2:{status}")


def test_unreviewed_nb_spam_is_only_a_penalty():
    unrev = {"Email": "ioana.popescu@gmail.com", "Spam Zone": "spam", "Human Verdict": ""}
    d = score_rows([row()], list1=[unrev])["decisions"][0]
    assert d["action"] == "pass" and d["score"] == 90 and "list1_unreviewed_spam" in {x["check"] for x in d["deductions"]}


def test_empty_placeholder_items_in_lists_are_ignored():
    assert score_rows([row()], list1=[{}], list2=[{}])["decisions"][0]["action"] == "pass"


# ── routes and outcomes ──
BAD = row(**{"Phone Number": "+91 98216 40735", "Current Location": "Delhi", "Country of Origin": "India"})   # 100-25-25-10 = 40
MIDDLE = row(**{"Phone Number": "+91 98216 40735"})                       # 100-25(phone IN)-5(vs RO) = 70 -> review band
CONF = {"spam_slack_channel": "C0TEST", "spam_table_id": "tbl1"}


def run_score(rows, list1=None, list2=None, dry_run=False, extra=None, **spam):
    return sp.run({"mode": "score", "config": {"spam_filter": spam, "dry_run": dry_run, **CONF, **(extra or {})},
                   "rows": rows, "list1": list1 or [], "list2": list2 or []})


def test_clean_row_passes_nothing_stored_or_sent():
    out = run_score([row()])
    assert [r["row_number"] for r in out["rows"]] == [2]
    assert out["table_writes"] == [] and out["slack_messages"] == []


def test_review_band_is_pending_with_a_bold_card_and_held():
    out = run_score([MIDDLE], extra={"spam_review_mention_ids": ["U0AAAAAAA", "S0BBBBBBB"]})
    d = out["decisions"][0]
    assert 50 <= d["score"] < 75 and (d["action"], d["reason"]) == ("hold", "score:review")
    assert out["rows"] == []
    w = out["table_writes"][0]
    assert (w["status"], w["source"], w["zone"]) == ("pending", "rules", "edge")
    m = out["slack_messages"][0]
    assert (m["kind"], m["channel"], m["table_id"], m["match_key"]) == ("review", "C0TEST", "tbl1", d["match_key"])
    assert m["text"].splitlines() == [
        "*Gsheet 2 Airtable checkup: Possible Spam in Volunteers*",
        "*<@U0AAAAAAA> <!subteam^S0BBBBBBB>*",
        "*Volunteer name: Ioana Popescu Email: ioana.popescu@gmail.com Phone: +91 98216 40735*"]


def test_review_card_without_mentions_has_no_empty_line():
    assert len(run_score([MIDDLE])["slack_messages"][0]["text"].splitlines()) == 2


def test_mention_ids_accept_a_list_or_text_and_reject_junk():
    assert sp._mentions("U0AAAAAAA, U0CCCCCCC") == "<@U0AAAAAAA> <@U0CCCCCCC>"
    assert sp._mentions('["U0AAAAAAA"]') == "<@U0AAAAAAA>" and sp._mentions([]) == ""
    with pytest.raises(sp.ScriptError, match="not a Slack member"):
        run_score([MIDDLE], extra={"spam_review_mention_ids": ["@Amalia"]})


def test_score_exactly_at_review_at_is_reviewed_not_blocked():
    d = run_score([MIDDLE], review_at=70)["decisions"][0]
    assert d["score"] == 70 and d["action"] == "hold"


def test_below_review_at_is_stored_as_spam_with_a_one_line_note():
    out = run_score([BAD])
    d = out["decisions"][0]
    assert d["score"] < 50 and (d["action"], d["reason"]) == ("drop", "score:spam")
    assert out["rows"] == [] and out["table_writes"][0]["status"] == "spam"
    m = out["slack_messages"][0]
    assert m["kind"] == "blocked" and m["text"] == f"Spam in Gsheets 2 Airtable, score {d['score']}, Name Ioana Popescu, blocked."


def test_typed_text_cannot_format_or_mention_in_slack():
    evil = row(**{"First Name": "<!channel>", "Last Name": "*bold*", "Phone Number": "+91 98216 40735"})
    text = run_score([evil])["slack_messages"][0]["text"]
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text and "*bold*" not in text


def test_known_rows_are_silent_on_later_runs():
    key = run_score([BAD])["decisions"][0]["match_key"]
    for status, action in (("spam", "drop"), ("pending", "hold")):
        out = run_score([BAD], list2=[{"match_key": key, "status": status}])
        assert out["decisions"][0]["action"] == action
        assert out["table_writes"] == [] and out["slack_messages"] == []


def test_person_answers_not_spam_and_row_passes_next_run():
    key = run_score([MIDDLE])["decisions"][0]["match_key"]
    out = run_score([MIDDLE], list2=[{"match_key": key, "status": "not_spam"}])
    assert [r["row_number"] for r in out["rows"]] == [2] and out["table_writes"] == []


def test_list_hits_are_never_copied_into_the_table():
    out = run_score([row(**{"Email Address": "bad@x.com"})], list1=[L1])
    assert out["table_writes"] == [] and out["slack_messages"] == []


def test_dry_run_stores_and_sends_nothing_but_reports():
    out = run_score([BAD, MIDDLE], dry_run=True)
    assert out["table_writes"] == [] and out["slack_messages"] == []
    assert out["report"]["dry_run_not_written"] == {"table_writes": 2, "slack_messages": 2}
    assert out["summary"].startswith("DRY RUN")


def test_filter_off_passes_every_row():
    out = run_score([BAD, MIDDLE], enabled=False)
    assert [r["row_number"] for r in out["rows"]] == [2, 2] and out["slack_messages"] == []


def test_table_row_has_exactly_the_csv_columns():
    csv_header = (Path(__file__).resolve().parent.parent / "known_spam.csv").read_text().strip().split(",")
    assert list(run_score([BAD])["table_writes"][0]) == csv_header


def test_thresholds_must_be_ordered():
    with pytest.raises(sp.ScriptError, match="review_at < pass_at"):
        run_score([row()], review_at=80)


def test_unknown_mode_and_bad_input():
    with pytest.raises(sp.ScriptError, match="unknown mode"):
        sp.run({"mode": "nope"})
    with pytest.raises(sp.ScriptError, match="score: "):
        sp.run({"mode": "score", "rows": [row()], "list1": "oops"})


def test_config_may_arrive_as_a_json_string():
    out = sp.run({"mode": "score", "config": {"spam_filter": json.dumps({"pass_at": 99})}, "rows": [MIDDLE]})
    assert out["decisions"][0]["action"] == "hold"
