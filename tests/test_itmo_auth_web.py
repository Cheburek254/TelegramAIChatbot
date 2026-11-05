import secrets
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from app.itmo_auth_web import create_auth_app


async def test_secure_page_assets_origin_cookie_and_no_password_echo():
    service = Mock(
        cipher=Fernet(Fernet.generate_key()),
        public_url="https://study.example",
        sign_in=AsyncMock(return_value="111111"),
    )
    client = TestClient(TestServer(create_auth_app(service)))
    await client.start_server()
    try:
        page = await client.get("/itmo/login")
        assert page.status == 200 and page.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        cookie = page.cookies["__Host-study-itmo"]
        assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict"
        for asset in ("auth.js", "auth.css"):
            assert (await client.get("/itmo/" + asset)).status == 200
        body = {
            "ticket": secrets.token_urlsafe(32),
            "username": "111111",
            "password": "fixture-secret",
        }
        response = await client.post(
            "/itmo/session", json=body, headers={"Origin": "https://evil.example"}
        )
        assert response.status == 403
        service.sign_in.assert_not_called()
        headers = {"Origin": service.public_url, "Cookie": "__Host-study-itmo=" + cookie.value}
        response = await client.post("/itmo/session", json={**body, "owner": 73}, headers=headers)
        assert response.status == 400
        service.sign_in.assert_not_called()
        response = await client.post("/itmo/session", json=body, headers=headers)
        assert response.status == 200 and await response.json() == {"account": "111111"}
        service.sign_in.assert_awaited_once_with(body["ticket"], "111111", "fixture-secret")
        assert "fixture-secret" not in await response.text()
    finally:
        await client.close()
