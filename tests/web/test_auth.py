import pyotp

from tests.web.conftest import PASSWORD, Env, H


async def test_first_signup_is_admin_then_invite_required(env: Env) -> None:
    r = await env.signup(display_name="Moi")
    assert r.status_code == 201
    assert r.json()["is_admin"] is True
    assert r.json()["onboarding_step"] == "profile"
    assert "kobr4_session" in r.cookies
    env.client.cookies.clear()
    r = await env.signup("autre@example.com")
    assert r.status_code == 403
    r = await env.signup("autre@example.com", invite_code="bienvenue")
    assert r.status_code == 201
    assert r.json()["is_admin"] is False


async def test_signup_validation(env: Env) -> None:
    r = await env.post("/api/auth/signup", {"email": "a@example.com", "password": "court"})
    assert r.status_code == 422
    assert "12 caractères" in r.json()["detail"]
    r = await env.post("/api/auth/signup", {"email": "pas-un-email", "password": PASSWORD})
    assert r.status_code == 422
    await env.signup()
    env.client.cookies.clear()
    r = await env.signup(invite_code="bienvenue")
    assert r.status_code == 409


async def test_csrf_header_required(env: Env) -> None:
    r = await env.client.post(
        "/api/auth/signup", json={"email": "a@example.com", "password": PASSWORD}
    )
    assert r.status_code == 403
    assert "X-Kobr4" in r.json()["detail"]


async def test_login_logout_and_me(env: Env) -> None:
    await env.signup()
    env.client.cookies.clear()
    assert (await env.client.get("/api/auth/me")).status_code == 401
    r = await env.post("/api/auth/login", {"email": "MOI@example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["mfa_required"] is False
    assert (await env.client.get("/api/auth/me")).json()["email"] == "moi@example.com"
    assert (await env.post("/api/auth/logout")).status_code == 200
    env.client.cookies.clear()
    assert (await env.client.get("/api/auth/me")).status_code == 401


async def test_lockout_after_failed_logins(env: Env) -> None:
    await env.signup()
    env.client.cookies.clear()
    for _ in range(5):
        r = await env.post(
            "/api/auth/login", {"email": "moi@example.com", "password": "mauvais mot de passe"}
        )
        assert r.status_code == 401
    r = await env.post("/api/auth/login", {"email": "moi@example.com", "password": PASSWORD})
    assert r.status_code == 423


async def test_mfa_flow_and_recovery_code(env: Env) -> None:
    await env.signup()
    secret, codes = await env.enable_mfa()
    assert len(codes) == 8
    assert (await env.client.get("/api/auth/me")).json()["totp_enabled"] is True
    await env.post("/api/auth/logout")
    env.client.cookies.clear()
    r = await env.post("/api/auth/login", {"email": "moi@example.com", "password": PASSWORD})
    assert r.json()["mfa_required"] is True
    assert (await env.client.get("/api/auth/me")).status_code == 401
    assert (await env.post("/api/auth/mfa", {"code": "000000"})).status_code == 401
    r = await env.post("/api/auth/mfa", {"code": codes[0]})
    assert r.status_code == 200
    assert r.json()["recovery_codes_left"] == 7
    # Un code de secours ne sert qu'une fois.
    await env.post("/api/auth/logout")
    env.client.cookies.clear()
    await env.post("/api/auth/login", {"email": "moi@example.com", "password": PASSWORD})
    assert (await env.post("/api/auth/mfa", {"code": codes[0]})).status_code == 401
    assert (await env.post("/api/auth/mfa", {"code": pyotp.TOTP(secret).now()})).status_code == 200


async def test_password_change_revokes_other_sessions(env: Env) -> None:
    await env.signup()
    other = env.client.cookies.get("kobr4_session")
    await env.post("/api/auth/login", {"email": "moi@example.com", "password": PASSWORD})
    r = await env.post(
        "/api/auth/password",
        {"current_password": PASSWORD, "new_password": "un tout autre secret 42"},
    )
    assert r.status_code == 200
    env.client.cookies.set("kobr4_session", other or "")
    assert (await env.client.get("/api/auth/me")).status_code == 401


async def test_sessions_list(env: Env) -> None:
    await env.signup()
    sessions = (await env.client.get("/api/auth/sessions")).json()
    assert len(sessions) == 1
    assert sessions[0]["current"] is True
    assert (await env.delete("/api/auth/sessions/zzz")).status_code == 404


async def test_disable_mfa_requires_password_and_code(env: Env) -> None:
    await env.signup()
    secret, _ = await env.enable_mfa()
    r = await env.post(
        "/api/auth/2fa/disable", {"password": "faux", "code": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 401
    r = await env.post(
        "/api/auth/2fa/disable", {"password": PASSWORD, "code": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 200
    assert r.json()["totp_enabled"] is False


async def test_security_headers(env: Env) -> None:
    r = await env.client.get("/healthz")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.json()["ok"] is True
    assert (await env.client.get("/metrics")).status_code == 404  # pas de jeton configuré
    assert H


async def test_metrics_require_token(env: Env) -> None:
    from dataclasses import replace

    env.client._transport.app.state.kobr4.config = replace(env.config, metrics_token="secret")  # type: ignore[attr-defined]
    assert (await env.client.get("/metrics")).status_code == 403
    r = await env.client.get("/metrics", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert "kobr4_equity" in r.text
