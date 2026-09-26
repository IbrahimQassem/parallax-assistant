"""Check object-bound effects independently of model claims and page prose."""
import json

import pytest

from parallax.assistant.effect_checks import EffectChecks, parse_expectation


URL = "https://example.com/orders"


def condition(subject="ORDER-42"):
    return json.dumps({"description": "Create the requested order", "url": URL,
                       "subject": subject, "outcome": f"{subject} confirmed"})


def page(*texts, url=URL, frame=URL, main=True):
    return {"url": url, "evidence_blocks": [{"text": text, "frame": frame, "main": main} for text in texts]}


@pytest.mark.parametrize("snapshot", [
    page("ORDER-43 confirmed"),
    page("ORDER-42", "confirmed"),
    page("ORDER-42 not confirmed"),
    page("ORDER-420 confirmed"),
    page("ORDER-42 confirmed", "ORDER-42 confirmed"),
    page("ORDER-42 confirmed", "ORDER-42 pending"),
    page("ORDER-42 confirmed", url=URL + "/other"),
    page("ORDER-42 confirmed", frame="https://other.example/orders"),
    page("ORDER-42 confirmed", main=False),
    {"url": URL, "text": "ORDER-42 confirmed"},
])
def test_wrong_ambiguous_separate_or_unscoped_evidence_cannot_verify(snapshot):
    checks = EffectChecks()
    checks.register("order", condition(), page("No order yet"))
    checks.sent("attempt")
    checks.observe(snapshot, "S1")
    assert checks.missing()
    assert checks.summaries()[0]["status"] == "unverified"


def test_checks_are_predeclared_immutable_and_match_each_operation_independently():
    checks = EffectChecks()
    checks.register("first", condition(), page())
    with pytest.raises(ValueError):
        checks.register("first", condition("ORDER-43"), page())
    preview = checks.preview(page())
    preview["outcome"] = "forged"
    checks.sent("prepare")
    checks.sent("submit")
    checks.observe(page(" ORDER-42\n  confirmed "), "S1")
    assert not checks.missing()
    assert checks.context()["active"] is None
    checks.register("second", condition("ORDER-43"), page())
    checks.sent("other")
    checks.observe(page("ORDER-42 confirmed", "ORDER-43 pending"), "S1")
    assert checks.missing()
    assert [item["status"] for item in checks.summaries()] == ["matched", "unverified"]
    checks.observe(page("ORDER-42 confirmed", "ORDER-43 confirmed"), "S1")
    assert not checks.missing()
    assert checks.summaries()[0]["attempt_count"] == 2
    assert "ORDER-42" not in json.dumps(checks.summaries())


def test_completed_condition_cannot_be_reused_to_cover_another_write():
    checks = EffectChecks()
    checks.register("order", condition(), page())
    checks.sent("first")
    checks.observe(page("ORDER-42 confirmed"), "S1")
    checks.sent("second")
    checks.observe(page("ORDER-42 confirmed"), "S1")
    assert checks.missing()
    assert checks.summaries()[-1]["status"] == "no_condition"


def test_matching_baseline_prevents_resending_and_old_text_is_not_post_action_evidence():
    checks = EffectChecks()
    checks.register("order", condition(), page())
    checks.observe(page("ORDER-42 confirmed"), "S1")
    assert checks.summaries() == []
    with pytest.raises(ValueError, match="ظاهرة بالفعل"):
        checks.preview(page("ORDER-42 confirmed"))
    checks.sent("attempt")
    assert checks.missing()
    checks.observe(page("ORDER-42 confirmed"), "S1")
    assert not checks.missing()
    checks.observe(page("ORDER-42 pending"), "S1")
    assert checks.missing()


def test_changed_user_instruction_and_destination_require_new_condition():
    checks = EffectChecks()
    checks.register("order", condition(), page())
    with pytest.raises(ValueError, match="وجهة"):
        checks.preview(page(url="https://other.example"))
    checks.invalidate()
    assert checks.preview(page()) is None
    checks.sent("uncovered")
    checks.register("later", condition(), page())
    checks.observe(page("ORDER-42 confirmed"), "S1")
    assert checks.missing()
    assert checks.summaries()[0]["status"] == "no_condition"


@pytest.mark.parametrize("change", [
    {"subject": "", "outcome": "Done"}, {"outcome": "confirmed"}, {"outcome": "ORDER-42"},
    {"url": "javascript:alert(1)"}, {"selector": "#result"}, {"outcome": 42},
])
def test_invalid_condition_is_rejected_without_echoing_its_contents(change):
    raw = json.loads(condition())
    raw.update(change)
    with pytest.raises(ValueError, match="شرط التحقق غير صالح"):
        parse_expectation("order", json.dumps(raw))


def record_condition(**changes):
    return json.dumps({"description": "Reserve the selected service", "url": URL,
                       "subject": {"Service": "Blue", "Account": "Demo"},
                       "outcome": {"Status": "Confirmed", "Price": "5 USD"}, **changes})


def records(*values, url=URL, frame=URL, main=True):
    return {"url": url, "evidence_records": [{"fields": value, "frame": frame, "main": main} for value in values]}


def test_generated_fields_do_not_require_a_guessed_identifier_or_complete_record():
    checks = EffectChecks()
    checks.register("reservation", record_condition(), records())
    checks.sent("attempt")
    result = {"Service": "Blue", "Account": "Demo", "Status": "Confirmed", "Price": "5 USD", "Receipt": "GENERATED-8724"}
    checks.observe(records(result), "S1")
    assert not checks.missing()
    assert checks.summaries()[0]["status"] == "matched"
    assert "GENERATED" not in json.dumps(checks.summaries())


@pytest.mark.parametrize("wrong", [
    {"Account": "Other"}, {"Service": "Blue Plus"}, {"Status": "Not Confirmed"}, {"Price": "50 USD"}, {"Status": "confirmed"},
])
def test_each_identity_and_outcome_field_must_match_exactly(wrong):
    checks = EffectChecks()
    checks.register("reservation", record_condition(), records())
    checks.sent("attempt")
    checks.observe(records({"Service": "Blue", "Account": "Demo", "Status": "Confirmed", "Price": "5 USD", **wrong}), "S1")
    assert checks.missing()


def test_matching_fields_cannot_be_assembled_from_separate_or_ambiguous_records():
    checks = EffectChecks()
    checks.register("reservation", record_condition(), records())
    checks.sent("attempt")
    good = {"Service": "Blue", "Account": "Demo", "Status": "Confirmed", "Price": "5 USD"}
    for snapshot in [
        records({"Service": "Blue", "Account": "Demo"}, {"Status": "Confirmed", "Price": "5 USD"}),
        records(good, {**good, "Status": "Pending"}),
        records(good, main=False), records(good, frame=URL+"/other"), records(good, url=URL+"/other"),
    ]:
        checks.observe(snapshot, "S1")
        assert checks.missing()


@pytest.mark.parametrize("changes", [
    {"subject": {}}, {"outcome": {"Service": "Blue"}}, {"subject": "Blue"},
    {"outcome": {"Status": 4}}, {"outcome": {"Status": ""}},
    {"subject": {"Account": "Demo", " Account ": "Other"}},
])
def test_malformed_or_conflicting_record_conditions_are_rejected(changes):
    with pytest.raises(ValueError):
        parse_expectation("reservation", record_condition(**changes))


def test_predeclared_label_aliases_match_one_record_without_changing_expected_values():
    checks = EffectChecks()
    raw = record_condition(label_aliases={"Service": ["Product"], "Status": ["State"]})
    checks.register("reservation", raw, records())
    preview = checks.preview(records())
    preview["label_aliases"]["Status"].append("Other")
    checks.sent("attempt")
    good = {"Product": "Blue", "Account": "Demo", "State": "Confirmed", "Price": "5 USD"}
    checks.observe(records(good), "S1")
    assert not checks.missing()
    for snapshot in [
        records({**good, "State": "Pending"}),
        records({**good, "Service": "Blue"}),
        records({**good, "Status": "Pending"}),
        records(good, {**good, "State": "Pending"}),
        records(good, {**good, "Service": "Other"}),
        records({"Product": "Blue", "Account": "Demo"}, {"State": "Confirmed", "Price": "5 USD"}),
        records({"Product": "Blue", "Account": "Demo", "Other": "Confirmed", "Price": "5 USD"}),
        records(good, frame=URL + "/other"),
    ]:
        checks.observe(snapshot, "S1")
        assert checks.missing()
    with pytest.raises(ValueError):
        checks.register("reservation", record_condition(label_aliases={"Status": ["Other"]}), records())
    assert "Product" not in json.dumps(checks.summaries())


@pytest.mark.parametrize("aliases", [
    {}, {"Unknown": ["State"]}, {"Status": "State"}, {"Status": []},
    {"Status": ["State", "State"]}, {"Status": [" Status "]},
    {"Status": ["Price"]}, {"Status": ["Value"], "Price": ["Value"]},
    {"Status": ["a", "b", "c", "d"]}, {"Status": [42]}, {"Status": [""]},
])
def test_aliases_cannot_merge_fields_or_expand_without_bounds(aliases):
    with pytest.raises(ValueError):
        parse_expectation("reservation", record_condition(label_aliases=aliases))


def test_text_conditions_do_not_accept_field_aliases():
    raw = json.loads(condition())
    raw["label_aliases"] = {"Status": ["State"]}
    with pytest.raises(ValueError):
        parse_expectation("order", json.dumps(raw))


def test_origin_scope_follows_result_pages_and_keeps_evidence_across_unrelated_pages():
    checks = EffectChecks()
    checks.register("reservation", record_condition(url_scope="origin", casefold_outcome=["Status"]), records())
    preview = checks.preview(records())
    preview["url_scope"] = "page"
    preview["casefold_outcome"].clear()
    with pytest.raises(ValueError):
        checks.register("reservation", record_condition(url_scope="page"), records())
    receipt = URL + "/receipt/827"
    good = {"Service": "Blue", "Account": "Demo", "Status": "CONFIRMED", "Price": "5 USD"}
    checks.observe(records(good, url=receipt, frame=receipt), "S2")
    assert not checks.summaries(), "A page read before sending is not effect evidence"
    with pytest.raises(ValueError, match="ظاهرة بالفعل"):
        checks.preview(records(good, url=receipt, frame=receipt))
    checks.sent("attempt")
    checks.observe(records(good, url=receipt, frame=receipt), "S2")
    assert checks.summaries()[0]["status"] == "matched"
    other = URL + "/other"
    checks.observe(records({**good, "Account": "Other"}, url=other, frame=other), "S3")
    assert checks.summaries()[0]["source_id"] == "S2"
    checks.observe(records({**good, "Status": "Pending"}, url=other, frame=other), "S3")
    assert checks.missing(), "An updated record of the same identity must invalidate old proof"
    checks.observe(records(good, url=receipt, frame=receipt), "S4")
    assert not checks.missing()
    checks.observe(records(url=receipt, frame=receipt), "S5")
    assert checks.missing(), "Disappearing from the evidence page must invalidate old proof"


@pytest.mark.parametrize("change", [
    {"url": "https://other.example/orders", "frame": "https://other.example/orders"},
    {"url": "http://example.com/orders", "frame": "http://example.com/orders"},
    {"url": "https://example.com:444/orders", "frame": "https://example.com:444/orders"},
    {"url": "https://example.com:0/orders", "frame": "https://example.com:0/orders"},
    {"frame": URL + "/frame"}, {"main": False},
])
def test_origin_scope_does_not_cross_origin_or_frame(change):
    checks = EffectChecks()
    checks.register("reservation", record_condition(url_scope="origin"), records())
    checks.sent("attempt")
    checks.observe(records({"Service": "Blue", "Account": "Demo", "Status": "Confirmed", "Price": "5 USD"}, **change), "S2")
    assert checks.missing()


def test_casefold_applies_only_to_predeclared_outcome_values():
    checks = EffectChecks()
    checks.register("reservation", record_condition(casefold_outcome=["Status"], label_aliases={"Status": ["State"]}), records())
    checks.sent("attempt")
    good = {"Service": "Blue", "Account": "Demo", "State": "confirmed", "Price": "5 USD"}
    checks.observe(records(good), "S1")
    assert not checks.missing()
    for wrong in [{"Account": "demo"}, {"Service": "blue"}, {"Price": "5 usd"},
                  {"State": "Unconfirmed"}, {"State": "Not confirmed"}, {"State": "Confirmed later"}]:
        checks.observe(records({**good, **wrong}), "S1")
        assert checks.missing()
    checks.observe(records(good, good), "S1")
    assert checks.missing()


@pytest.mark.parametrize("change", [
    {"url_scope": "any"}, {"url_scope": None}, {"casefold_outcome": []},
    {"casefold_outcome": "Status"}, {"casefold_outcome": ["Account"]},
    {"casefold_outcome": ["Status", "Status"]}, {"casefold_outcome": [42]},
    {"url_scope": "origin", "subject": "ORDER-42", "outcome": "ORDER-42 confirmed"},
    {"casefold_outcome": ["Status"], "subject": "ORDER-42", "outcome": "ORDER-42 confirmed"},
])
def test_invalid_verification_scopes_and_comparators_are_rejected(change):
    with pytest.raises(ValueError):
        parse_expectation("reservation", record_condition(**change))
