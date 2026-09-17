"""Bulk CSV contact import -- Checkpoint 02 Steps 17-19, FR-1.1-FR-1.4."""

import io


def _upload(client, name: str, csv_text: str):
    return client.post(
        "/api/v1/campaigns/import",
        data={"name": name},
        files={"file": ("contacts.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )


def test_valid_csv_import_creates_campaign_and_contacts(client):
    csv_text = "phone_number\n555-200-0001\n555-200-0002\n555-200-0003\n"

    response = _upload(client, "Valid Import", csv_text)

    assert response.status_code == 201
    body = response.json()
    assert body["campaign_id"] is not None
    assert body["total"] == 3
    assert body["created"] == 3
    assert body["duplicates"] == 0
    assert body["invalid"] == 0

    contacts = client.get(f"/api/v1/campaigns/{body['campaign_id']}/contacts").json()
    assert contacts["total"] == 3
    assert all(c["status"] == "Pending" for c in contacts["items"])


def test_invalid_rows_are_rejected_and_reported(client):
    csv_text = "phone_number\n555-200-0010\nnot-a-real-number\n"

    response = _upload(client, "Invalid rows", csv_text)

    assert response.status_code == 201
    body = response.json()
    assert body["created"] == 1
    assert body["invalid"] == 1
    assert body["errors"][0]["reason"] == "invalid_phone_number"
    assert body["errors"][0]["row"] == 3  # header=1, row 2 valid, row 3 invalid


def test_duplicate_rows_within_file_create_only_one_contact(client):
    csv_text = "phone_number\n555-200-0020\n555-200-0020\n555-200-0020\n"

    response = _upload(client, "Dup rows", csv_text)

    assert response.status_code == 201
    body = response.json()
    assert body["created"] == 1
    assert body["duplicates"] == 2


def test_empty_valid_set_creates_no_campaign(client):
    csv_text = "phone_number\nnot-valid\nalso-not-valid\n"

    response = _upload(client, "Should not exist", csv_text)

    assert response.status_code == 201
    body = response.json()
    assert body["campaign_id"] is None
    assert body["created"] == 0

    campaigns = client.get("/api/v1/campaigns?limit=200").json()
    assert "Should not exist" not in [c["name"] for c in campaigns["items"]]


def test_missing_phone_number_column_is_rejected(client):
    csv_text = "name\nAlex\n"

    response = _upload(client, "Bad columns", csv_text)

    assert response.status_code == 422


def test_import_summary_never_returns_full_contact_list(client):
    csv_text = "phone_number\n555-200-0030\n555-200-0031\n"

    response = _upload(client, "Summary only", csv_text)

    body = response.json()
    assert "contacts" not in body
    assert "items" not in body
