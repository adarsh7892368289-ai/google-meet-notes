import pytest

from app.security import (
    InvalidStateError,
    create_oauth_state,
    verify_oauth_state,
)


def test_state_roundtrip():
    state = create_oauth_state("user-123")
    assert verify_oauth_state(state) == "user-123"


def test_verify_rejects_garbage():
    with pytest.raises(InvalidStateError):
        verify_oauth_state("not-a-real-token")


def test_verify_rejects_token_with_wrong_purpose():
    # an access token (no oauth_state purpose) must not be accepted as state
    from app.security import create_access_token

    token = create_access_token(subject="user-123")
    with pytest.raises(InvalidStateError):
        verify_oauth_state(token)
