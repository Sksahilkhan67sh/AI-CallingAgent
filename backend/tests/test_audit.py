"""Audit logging for contact/campaign management operations --
Checkpoint 02 Step 27."""

from sqlalchemy import select

from app.models.audit_log import AuditLog


def test_contact_creation_is_audited(client, db_session):
    campaign = client.post("/api/v1/campaigns", json={"name": "Audit campaign"}).json()
    contact = client.post(
        "/api/v1/contacts",
        json={"campaign_id": campaign["id"], "phone_number": "555-300-0001"},
    ).json()

    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "contact",
            AuditLog.action == "contact.created",
        )
    ).scalars().all()

    assert any(str(r.entity_id) == contact["id"] for r in rows)


def test_campaign_creation_is_audited(client, db_session):
    campaign = client.post("/api/v1/campaigns", json={"name": "Audited campaign"}).json()

    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "campaign",
            AuditLog.action == "campaign.created",
        )
    ).scalars().all()

    assert any(str(r.entity_id) == campaign["id"] for r in rows)


def test_campaign_membership_change_is_audited(client, db_session):
    campaign_a = client.post("/api/v1/campaigns", json={"name": "Membership A"}).json()
    campaign_b = client.post("/api/v1/campaigns", json={"name": "Membership B"}).json()
    contact = client.post(
        "/api/v1/contacts",
        json={"campaign_id": campaign_a["id"], "phone_number": "555-300-0002"},
    ).json()

    client.post(f"/api/v1/campaigns/{campaign_b['id']}/contacts/{contact['id']}")

    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "contact",
            AuditLog.action == "campaign.contact_added",
        )
    ).scalars().all()

    assert any(str(r.entity_id) == contact["id"] for r in rows)
