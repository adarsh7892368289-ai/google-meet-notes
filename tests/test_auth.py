async def test_register_returns_token(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "new@acme.com", "name": "New", "password": "password123"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_register_duplicate_email_conflicts(client):
    payload = {"email": "dup@acme.com", "name": "Dup", "password": "password123"}
    first = await client.post("/v1/auth/register", json=payload)
    assert first.status_code == 201
    second = await client.post("/v1/auth/register", json=payload)
    assert second.status_code == 409


async def test_login_success(client):
    await client.post(
        "/v1/auth/register",
        json={"email": "log@acme.com", "name": "Log", "password": "password123"},
    )
    resp = await client.post(
        "/v1/auth/login", json={"email": "log@acme.com", "password": "password123"}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_wrong_password_unauthorized(client):
    await client.post(
        "/v1/auth/register",
        json={"email": "w@acme.com", "name": "W", "password": "password123"},
    )
    resp = await client.post(
        "/v1/auth/login", json={"email": "w@acme.com", "password": "wrong"}
    )
    assert resp.status_code == 401


async def test_me_requires_auth_and_returns_user(client):
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "me@acme.com", "name": "Me", "password": "password123"},
    )
    token = reg.json()["access_token"]
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@acme.com"

    unauth = await client.get("/v1/auth/me")
    assert unauth.status_code in (401, 403)


async def test_me_with_malformed_token_returns_401(client):
    from app.security import create_access_token

    bad_token = create_access_token(subject="not-a-uuid")
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {bad_token}"})
    assert resp.status_code == 401
