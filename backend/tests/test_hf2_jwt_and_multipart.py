"""HF2 2.1 -- behavior that must survive the PyJWT / python-multipart / starlette upgrade.

JWT: the verifier pins HS256 and requires the claims it relies on. Multipart: a malformed
upload is a clean 4xx, never a 500, and never creates anything. (Valid and oversized imports
are covered by test_bulk_import.py and test_cp11_abuse_limits.py.)
"""

import base64
import json
import time

import jwt
import pytest

from app.core.config import get_settings

_PATH = "/api/v1/campaigns"


def _claims(**overrides):
    now = int(time.time())
    claims = {"sub": "tester", "role": "admin", "iat": now, "exp": now + 3600}
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _signed(claims, algorithm="HS256"):
    return jwt.encode(claims, get_settings().jwt_signing_key, algorithm=algorithm)


def _status(anon_client, token):
    return anon_client.get(_PATH, headers={"Authorization": f"Bearer {token}"}).status_code


def test_a_correctly_signed_token_is_accepted(anon_client):
    assert _status(anon_client, _signed(_claims())) == 200


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_algorithm_is_pinned_to_hs256(anon_client, algorithm):
    """Signed with the REAL key but another HMAC algorithm: still rejected."""
    assert _status(anon_client, _signed(_claims(), algorithm=algorithm)) == 401


def test_tampered_payload_with_the_original_signature_is_rejected(anon_client):
    header, payload, signature = _signed(_claims(role="operator")).split(".")
    padded = payload + "=" * (-len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded))
    decoded["role"] = "admin"
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(decoded, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    assert _status(anon_client, f"{header}.{forged_payload}.{signature}") == 401


@pytest.mark.parametrize("missing", ["sub", "role", "exp"])
def test_a_token_missing_a_required_claim_is_rejected(anon_client, missing):
    token = _signed(_claims(**{missing: None}))
    assert _status(anon_client, token) == 401


# --- malformed multipart on the import routes ------------------------------------------------

_MALFORMED = {
    "no-boundary": (b"--x\r\n", "multipart/form-data"),
    "truncated": (
        b'--B\r\nContent-Disposition: form-data; name="name"\r\n\r\nabc',
        "multipart/form-data; boundary=B",
    ),
    "garbage": (b"\x00\xff not multipart at all", "multipart/form-data; boundary=B"),
    "no-file-part": (
        b'--B\r\nContent-Disposition: form-data; name="name"\r\n\r\nabc\r\n--B--\r\n',
        "multipart/form-data; boundary=B",
    ),
}


@pytest.mark.parametrize("kind", sorted(_MALFORMED))
@pytest.mark.parametrize("route", ["/api/v1/campaigns/import", "/api/v1/admin/suppressions/import"])
def test_malformed_multipart_is_a_clean_4xx(client, route, kind):
    body, content_type = _MALFORMED[kind]
    response = client.post(route, content=body, headers={"content-type": content_type})
    assert response.status_code in (400, 422), (route, kind, response.status_code)
    assert "Traceback" not in response.text


def test_malformed_multipart_creates_no_campaign(client):
    before = client.get(_PATH).json()["total"]
    body, content_type = _MALFORMED["truncated"]
    client.post(
        "/api/v1/campaigns/import", content=body, headers={"content-type": content_type}
    )
    assert client.get(_PATH).json()["total"] == before
