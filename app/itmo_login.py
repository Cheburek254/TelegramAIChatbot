"""PKCE-вход через форму ITMO.ID; credentials/cookies живут только в одной попытке."""

import asyncio
import base64
import hashlib
import html
import json
import secrets
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

ISSUER = "https://id.itmo.ru/auth/realms/itmo"
AUTH = ISSUER + "/protocol/openid-connect/auth"
TOKEN = ISSUER + "/protocol/openid-connect/token"
USERINFO = ISSUER + "/protocol/openid-connect/userinfo"
CALLBACK = "https://my.itmo.ru/login/callback"
CLIENT = "student-personal-cabinet"


class LoginError(ValueError):
    pass


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and values.get("id") == "kc-form-login":
            self.action = values.get("action")


def form_action(body):
    form = LoginForm()
    form.feed(body)
    action = form.action
    if not action:
        # Current Keycloakify pages embed the form action in a JSON context.
        import re

        match = re.search(r'"loginAction"\s*:\s*("(?:[^"\\]|\\.)*")', body)
        if match:
            action = json.loads(match.group(1))
    parts = urlsplit(html.unescape(action or ""))
    if (
        parts.scheme != "https"
        or parts.hostname != "id.itmo.ru"
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
        or not parts.path.startswith("/auth/realms/itmo/login-actions/")
    ):
        raise LoginError("unexpected_login_page")
    return html.unescape(action)


def callback_code(location, state):
    parts = urlsplit(location)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        parts.scheme != "https"
        or parts.netloc != "my.itmo.ru"
        or parts.path != "/login/callback"
        or parts.fragment
        or query.get("state") != [state]
        or len(query.get("code", [])) != 1
        or not query["code"][0]
        or query.get("iss", [ISSUER]) != [ISSUER]
    ):
        raise LoginError("credentials_or_extra_step")
    return query["code"][0]


async def bounded_body(response):
    raw = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw) > 512000:
            raise LoginError("invalid_response")
    return raw


class ITMOLogin:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory or aiohttp.ClientSession

    async def login(self, username, password):
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        query = urlencode(
            {
                "protocol": "oauth2",
                "response_type": "code",
                "client_id": CLIENT,
                "redirect_uri": CALLBACK,
                "scope": "openid profile",
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        try:
            async with (
                asyncio.timeout(35),
                self.session_factory(
                    trust_env=False,
                    cookie_jar=aiohttp.CookieJar(),
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as session,
            ):
                async with session.get(AUTH + "?" + query, allow_redirects=False) as response:
                    if response.status != 200:
                        raise LoginError("provider_unavailable")
                    action = form_action((await bounded_body(response)).decode("utf-8"))
                async with session.post(
                    action,
                    data={"username": username, "password": password, "rememberMe": "on"},
                    allow_redirects=False,
                ) as response:
                    if response.status not in (302, 303):
                        raise LoginError("credentials_or_extra_step")
                    code = callback_code(response.headers.get("Location", ""), state)
                async with session.post(
                    TOKEN,
                    data={
                        "code": code,
                        "client_id": CLIENT,
                        "redirect_uri": CALLBACK,
                        "response_type": "code",
                        "grant_type": "authorization_code",
                        "code_verifier": verifier,
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise LoginError("credentials_or_extra_step")
                    tokens = json.loads(await bounded_body(response))
                access, refresh = tokens["access_token"], tokens["refresh_token"]
                if not all(
                    isinstance(value, str) and 20 <= len(value) <= 16000
                    for value in (access, refresh)
                ):
                    raise LoginError("invalid_response")
                async with session.get(
                    USERINFO, headers={"Authorization": "Bearer " + access}, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        raise LoginError("provider_unavailable")
                    profile = json.loads(await bounded_body(response))
                account = profile.get("preferred_username") or profile.get("sub")
                if not isinstance(account, str) or not 1 <= len(account) <= 128:
                    raise LoginError("invalid_response")
                return refresh, account
        except LoginError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise LoginError("provider_unavailable") from None
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise LoginError("invalid_response") from None
