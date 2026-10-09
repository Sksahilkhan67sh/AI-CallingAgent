"""CP14 (C4) -- one strict phone normalizer, used by every write path and lookup."""

import io
import random
import string

import pytest

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.services.phone import (
    InvalidPhoneError,
    is_dialable_region,
    normalize_phone,
    phone_fingerprint,
    region_of_e164,
)
from tests.phone_helpers import valid_in

IN = ["IN"]
EXPECTED = "+919876543210"

VALID_FORMS = [
    "9876543210",  # bare 10-digit Indian mobile: read as IN, NOT as US (the C4 bug)
    "09876543210",  # leading trunk 0
    "098765 43210",  # the exact C4 example that used to become +09876543210
    "919876543210",  # country code without '+'
    "+919876543210",
    "+91 98765 43210",
    "+91 98765-43210",
    "+91-98765-43210",
    "0091 98765 43210",  # international call prefix
    "00919876543210",
    "98765 43210",
    "98765-43210",
    "  9876543210  ",
    "9876543210\n",
    "９８７６５４３２１０",  # full-width digits
]


@pytest.mark.parametrize("raw", VALID_FORMS)
def test_every_spelling_of_one_number_normalizes_identically(raw):
    assert normalize_phone(raw, "IN", IN).e164 == EXPECTED


def test_normalization_is_idempotent():
    for raw in VALID_FORMS:
        once = normalize_phone(raw, "IN", IN).e164
        assert normalize_phone(once, "IN", IN).e164 == once


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "empty"),
        ("", "empty"),
        ("   ", "empty"),
        ("\t\n", "empty"),
        ("abc", "invalid_number"),
        ("98765", "invalid_number"),
        ("+", "invalid_number"),
        ("++919876543210", "invalid_number"),
        ("98765+43210", "invalid_number"),
        ("+910000000000", "invalid_number"),
        # phonenumbers would ACCEPT these; a dialer must not:
        ("+91 98765 43210 x12", "invalid_number"),  # extension
        ("9876543210 ext 5", "invalid_number"),  # extension
        ("98765#43210", "invalid_number"),
        ("9876543210;1", "invalid_number"),
        ("1-800-FLOWERS", "invalid_number"),  # vanity letters
        ("+1234567890123456", "too_long"),  # 16 digits: not E.164 (the C4 example)
        ("+9198765432101234567", "too_long"),
        ("9" * 20, "too_long"),
        ("9" * 65, "too_long"),
        ("+14155552671", "region_not_allowed"),  # a valid US number, but IN-only dialing
        ("+442071838750", "region_not_allowed"),
    ],
)
def test_rejections_carry_the_documented_reason_code(raw, code):
    with pytest.raises(InvalidPhoneError) as exc:
        normalize_phone(raw, "IN", IN)
    assert exc.value.code == code
    assert exc.value.args == (code,)  # the message is the code: never the number


def test_region_restriction_is_opt_in_and_case_insensitive():
    assert normalize_phone("+14155552671", "IN").region == "US"  # suppression writers
    assert normalize_phone("+14155552671", "IN", ["us", "in"]).e164 == "+14155552671"


def test_only_the_typed_error_ever_escapes():
    rng = random.Random(1405)
    alphabet = string.printable + "０１２٣५⁵²‮\u0000€日本"
    for _ in range(3000):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
        try:
            result = normalize_phone(raw, "IN", IN)
        except InvalidPhoneError as exc:
            assert exc.code in {"empty", "invalid_number", "too_long", "region_not_allowed"}
        else:
            assert result.e164.startswith("+91") and len(result.e164) <= 16
    for odd in (123, 4.5, b"9876543210", object()):
        with pytest.raises(InvalidPhoneError):
            normalize_phone(odd, "IN", IN)  # type: ignore[arg-type]


def test_dial_time_region_recheck_of_stored_numbers():
    assert is_dialable_region(EXPECTED, IN)
    assert not is_dialable_region("+14155552671", IN)  # legacy US row
    assert not is_dialable_region("+19876543210", IN)  # what the old bug stored for 9876543210
    assert not is_dialable_region("+09876543210", IN)  # unparseable legacy junk
    assert region_of_e164("not a number") is None


def test_fingerprint_is_keyed_stable_and_not_the_number():
    a = phone_fingerprint(EXPECTED, "key-one-key-one-key-one-key-one!")
    assert a == phone_fingerprint(EXPECTED, "key-one-key-one-key-one-key-one!")
    assert a != phone_fingerprint(EXPECTED, "key-two-key-two-key-two-key-two!")
    assert a != phone_fingerprint("+919876543211", "key-one-key-one-key-one-key-one!")
    assert "9876543210" not in a and len(a) == 16


# -- write paths ---------------------------------------------------------------------------------


def _campaign(client, **kw):
    response = client.post("/api/v1/campaigns", json={"name": "cp14 phone", **kw})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.parametrize("raw", ["9876543210", "098765 43210", "+91 98765-43210"])
def test_contact_create_stores_canonical_number(client, raw):
    campaign_id = _campaign(client)
    response = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": raw}
    )
    assert response.status_code == 201, response.text
    assert response.json()["normalized_phone_number"] == EXPECTED


def test_equivalent_spellings_are_one_contact(client):
    campaign_id = _campaign(client)
    first = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "9876543210"}
    )
    again = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "+91 98765 43210"}
    )
    assert first.status_code == 201 and again.status_code == 409


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("12345", "invalid_number"),
        ("+14155552671", "region_not_allowed"),
        ("9876543210 x1", "invalid_number"),
        ("9" * 30, "too_long"),
        ("abc", "invalid_number"),
    ],
)
def test_contact_create_rejects_with_422_and_no_number_in_the_message(client, raw, code):
    campaign_id = _campaign(client)
    response = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": raw}
    )
    assert response.status_code == 422
    assert code in response.text
    assert raw not in response.text or raw in ("abc", "12345")  # never echo a real number


def test_contact_create_never_500s_on_hostile_input(client):
    campaign_id = _campaign(client)
    for raw in ["", "   ", "\u0000", "💥" * 40, "9" * 5000, "+" * 100, "ａｂｃ", "x" * 70]:
        response = client.post(
            "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": raw}
        )
        assert response.status_code in (409, 422), (raw[:10], response.status_code)


def test_contact_update_renormalizes_and_rechecks_region(client):
    campaign_id = _campaign(client)
    created = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "9876543210"}
    ).json()
    ok = client.patch(f"/api/v1/contacts/{created['id']}", json={"phone_number": "098765 43211"})
    assert ok.status_code == 200 and ok.json()["normalized_phone_number"] == "+919876543211"
    bad = client.patch(f"/api/v1/contacts/{created['id']}", json={"phone_number": "+14155552671"})
    assert bad.status_code == 422 and "region_not_allowed" in bad.text


def test_association_revalidates_a_legacy_contact(client, db_session):
    source = Campaign(name="legacy", status=CampaignStatus.ACTIVE)
    target = Campaign(name="target", status=CampaignStatus.ACTIVE)
    db_session.add_all([source, target])
    db_session.flush()
    contact = Contact(
        campaign_id=source.id,
        phone_number="9876543210",
        normalized_phone_number="+19876543210",  # what the pre-CP14 bug stored
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    db_session.commit()
    response = client.post(f"/api/v1/campaigns/{target.id}/contacts/{contact.id}")
    assert response.status_code == 409, response.text
    assert "not dialable" in response.text
    db_session.refresh(contact)
    assert contact.campaign_id == source.id  # nothing moved


# -- CSV import -----------------------------------------------------------------------------------


def _upload(client, csv_text: str, name="cp14 import"):
    return client.post(
        "/api/v1/campaigns/import",
        data={"name": name},
        files={"file": ("c.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )


def test_csv_import_reports_each_bad_row_and_still_imports_the_good_ones(client):
    csv_text = "\n".join(
        [
            "phone_number",
            "9876543210",  # 2  ok
            "   ",  # 3  empty (whitespace-only; a truly blank line is skipped)
            "123",  # 4  invalid_number
            "+14155552671",  # 5  region_not_allowed
            "9" * 25,  # 6  too_long
            "09876543210",  # 7  duplicate of row 2 (canonical equality)
            "+91 98765 43299",  # 8  ok
            "98765 43210 x4",  # 9  invalid_number
        ]
    )
    response = _upload(client, csv_text)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["total"], body["created"], body["duplicates"], body["invalid"]) == (8, 2, 1, 5)
    assert {(e["row"], e["reason"]) for e in body["errors"]} == {
        (3, "empty"),
        (4, "invalid_number"),
        (5, "region_not_allowed"),
        (6, "too_long"),
        (9, "invalid_number"),
    }
    numbers = {
        c["normalized_phone_number"]
        for c in client.get(f"/api/v1/campaigns/{body['campaign_id']}/contacts").json()["items"]
    }
    assert numbers == {EXPECTED, "+919876543299"}


def test_csv_row_numbers_are_physical_lines_even_after_a_blank_line(client):
    body = _upload(client, "phone_number\n9876543210\n\n123\n").json()
    assert body["errors"] == [{"row": 4, "reason": "invalid_number"}]


def test_csv_import_counts_suppressed_numbers_but_still_imports_them(client, db_session):
    db_session.add(
        Suppression(phone_number=EXPECTED, source=SuppressionSource.MANUAL_API, reason="dnc")
    )
    db_session.commit()
    body = _upload(client, f"phone_number\n9876543210\n{valid_in(5)}\n").json()
    assert (body["created"], body["suppressed_count"]) == (2, 1)


def test_csv_import_keeps_the_cp11_caps(client):
    too_many = "phone_number\n" + "\n".join(valid_in(i) for i in range(10_001))
    response = _upload(client, too_many)
    assert response.status_code == 422
