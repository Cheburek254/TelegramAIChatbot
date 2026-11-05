import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.itmo_login import (
    AUTH,
    CALLBACK,
    TOKEN,
    USERINFO,
    ITMOLogin,
    LoginError,
    callback_code,
    form_action,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/auth/realms/itmo/login-actions/x",
        "http://id.itmo.ru/auth/realms/itmo/login-actions/x",
        "https://id.itmo.ru:8443/auth/realms/itmo/login-actions/x",
        "https://id.itmo.ru@evil.example/auth/realms/itmo/login-actions/x",
        "https://id.itmo.ru/anything",
    ],
)
def test_credentials_cannot_be_posted_outside_fixed_issuer(url):
    with pytest.raises(LoginError):
        form_action(f'<form id="kc-form-login" action="{url}">')


def test_form_context_and_html_entities():
    action = "https://id.itmo.ru/auth/realms/itmo/login-actions/authenticate?a=1&b=2"
    assert form_action(json.dumps({"loginAction": action})) == action
    assert (
        form_action('<form id="kc-form-login" action="' + action.replace("&", "&amp;") + '">')
        == action
    )


@pytest.mark.parametrize(
    "location",
    [
        CALLBACK + "?code=c&state=other",
        CALLBACK + "?state=s",
        CALLBACK + "?code=c&code=d&state=s",
        CALLBACK + "?code=c&state=s&iss=https://evil.example",
        "https://evil.example/login/callback?code=c&state=s",
    ],
)
def test_redirect_state_issuer_and_unique_code(location):
    with pytest.raises(LoginError):
        callback_code(location, "s")


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, _):
        yield self.body.encode() if isinstance(self.body, str) else json.dumps(self.body).encode()


class Session:
    def __init__(self, *, extra_step=False, wrong_state=False):
        self.extra_step = extra_step
        self.wrong_state = wrong_state
        self.calls = []
        self.query = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert kwargs["allow_redirects"] is False
        if url.startswith(AUTH + "?"):
            self.query = parse_qs(urlsplit(url).query)
            return Response(
                '<form id="kc-form-login" action="https://id.itmo.ru/auth/realms/itmo/login-actions/authenticate">'
            )
        assert url == USERINFO
        return Response({"preferred_username": "111111"})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        assert kwargs["allow_redirects"] is False
        if url != TOKEN:
            return Response(
                "extra-step",
                status=200 if self.extra_step else 302,
                headers={
                    "Location": CALLBACK
                    + "?code=one-time-code&state="
                    + ("other" if self.wrong_state else self.query["state"][0])
                },
            )
        verifier = kwargs["data"]["code_verifier"]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert self.query["code_challenge"] == [challenge]
        return Response(
            {
                "access_token": "test-only-access-token-111111",
                "refresh_token": "test-only-refresh-token-111111",
            }
        )


async def test_login_pkce_cookie_isolation_and_verified_account():
    made = []

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        session = Session()
        made.append((kwargs["cookie_jar"], session))
        return session

    client = ITMOLogin(factory)
    assert await client.login("111111", "test-password") == (
        "test-only-refresh-token-111111",
        "111111",
    )
    await client.login("222222", "different-test-password")
    assert made[0][0] is not made[1][0]
    assert made[0][1].calls[1][2]["data"]["password"] == "test-password"


@pytest.mark.parametrize("options", [{"extra_step": True}, {"wrong_state": True}])
async def test_extra_step_or_wrong_state_never_exchanges_code(options):
    session = Session(**options)
    with pytest.raises(LoginError, match="credentials_or_extra_step"):
        await ITMOLogin(lambda **_: session).login("111111", "test-password")
    assert not any(url == TOKEN for _, url, _ in session.calls)
