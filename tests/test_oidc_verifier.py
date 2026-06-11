import pytest

from app.events.oidc import (
    GooglePushVerifier,
    PushVerificationError,
    VerifiedPush,
)


def _make_verifier(claims, *, expected_audience, expected_email, raise_exc=None):
    def fake_verify(token, request, audience=None):
        if raise_exc is not None:
            raise raise_exc
        return claims

    return GooglePushVerifier(
        expected_audience=expected_audience,
        expected_email=expected_email,
        _verify_fn=fake_verify,
    )


def test_verify_accepts_valid_token():
    claims = {
        "iss": "https://accounts.google.com",
        "email": "pusher@project.iam.gserviceaccount.com",
        "email_verified": True,
        "aud": "https://app.example/v1/webhooks/google/events",
    }
    v = _make_verifier(
        claims,
        expected_audience="https://app.example/v1/webhooks/google/events",
        expected_email="pusher@project.iam.gserviceaccount.com",
    )
    result = v.verify("Bearer the.jwt.token")
    assert isinstance(result, VerifiedPush)
    assert result.email == "pusher@project.iam.gserviceaccount.com"


def test_verify_rejects_missing_bearer():
    v = _make_verifier({}, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify(None)
    with pytest.raises(PushVerificationError):
        v.verify("token-without-bearer-prefix")


def test_verify_rejects_bad_issuer():
    claims = {"iss": "https://evil.example", "email": "e", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_rejects_unverified_email():
    claims = {"iss": "https://accounts.google.com", "email": "e", "email_verified": False}
    v = _make_verifier(claims, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_rejects_wrong_email():
    claims = {"iss": "https://accounts.google.com", "email": "other@x", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="expected@x")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_propagates_library_failure_as_push_error():
    v = _make_verifier(
        {}, expected_audience="a", expected_email="e", raise_exc=ValueError("bad signature")
    )
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_skips_email_check_when_no_expected_email():
    claims = {"iss": "accounts.google.com", "email": "anyone@x", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="")
    result = v.verify("Bearer x")
    assert result.email == "anyone@x"


class AllowAllVerifier:
    def verify(self, authorization_header):
        return VerifiedPush(email="test@local")


def test_allow_all_verifier_is_usable_in_tests():
    assert AllowAllVerifier().verify("anything").email == "test@local"
