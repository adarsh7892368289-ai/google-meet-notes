import pytest
from cryptography.fernet import Fernet

from app.google.oauth_client import TokenBundle
from app.models import Conference, Notes, User
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conference(db_session) -> Conference:
    user = User(email="n@acme.com", name="N", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="n@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-2",
        pipeline_state="transcript_fetched",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_notes_round_trip(db_session):
    conf = await _conference(db_session)
    n = Notes(
        conference_id=conf.id,
        title="Q3 Roadmap Sync",
        summary="We agreed on the roadmap.",
        decisions=["Ship feature X in Q3"],
        action_items=[{"who": "Alice", "what": "Draft spec"}],
        gemini_model="gemini-2.5-flash",
    )
    db_session.add(n)
    await db_session.commit()
    await db_session.refresh(n)
    assert n.id is not None
    assert n.title == "Q3 Roadmap Sync"
    assert n.decisions == ["Ship feature X in Q3"]
    assert n.action_items[0]["who"] == "Alice"
