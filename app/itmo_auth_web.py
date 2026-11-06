"""Минимальный HTTPS backend, без access log и без передачи credentials в Telegram/LLM."""

import asyncio
import hashlib
import hmac
import secrets
import time
from pathlib import Path

from aiohttp import web

from app.itmo_auth_web_messages import login_message
from app.itmo_login import LoginError
from app.miniapp import MINIAPP_CSP

ASSETS = Path("assets/itmo-auth")


def create_auth_app(service, *, miniapp=None):
    cookie_key = service.cipher.encrypt(b"cookie-signing-key")
    cookie_key = hashlib.sha256(cookie_key).digest()

    @web.middleware
    async def secure(request, handler):
        is_miniapp = miniapp is not None and request.path.startswith("/miniapp/")
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except Exception:
            response = web.json_response(
                {
                    "error": "Сервис временно недоступен. Повторите позже."
                    if is_miniapp
                    else "Не удалось выполнить вход. Повторите позже."
                },
                status=503,
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        if is_miniapp:
            response.headers["Content-Security-Policy"] = MINIAPP_CSP
        else:
            response.headers["X-Frame-Options"] = "DENY"
        if isinstance(response, web.HTTPException):
            raise response
        return response

    def signature(value):
        return hmac.new(cookie_key, value.encode(), hashlib.sha256).hexdigest()

    async def page(_request):
        value = str(int(time.time()) + 600) + "." + secrets.token_hex(16)
        response = web.Response(
            body=await asyncio.to_thread(ASSETS.joinpath("login.html").read_bytes),
            content_type="text/html",
        )
        response.set_cookie(
            "__Host-study-itmo",
            value + "." + signature(value),
            secure=True,
            httponly=True,
            samesite="Strict",
            max_age=600,
            path="/",
        )
        return response

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"auth.js", "auth.css"}:
            raise web.HTTPNotFound()
        return web.Response(
            body=await asyncio.to_thread(ASSETS.joinpath(name).read_bytes),
            content_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    async def login(request):
        try:
            value, mac = request.cookies.get("__Host-study-itmo", "").rsplit(".", 1)
            expires = int(value.split(".")[0])
            if (
                request.headers.get("Origin") != service.public_url
                or not hmac.compare_digest(signature(value), mac)
                or not int(time.time()) < expires <= int(time.time()) + 605
            ):
                raise ValueError
            if request.content_type != "application/json":
                raise ValueError
        except (ValueError, TypeError):
            return web.json_response(
                {"error": "Откройте страницу заново по ссылке из бота."}, status=403
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"ticket", "username", "password"}:
                raise LoginError("invalid_input")
            account = await service.sign_in(
                payload["ticket"], payload["username"], payload["password"]
            )
            return web.json_response({"account": account})
        except LoginError as exc:
            return web.json_response({"error": login_message(str(exc))}, status=400)
        except (ValueError, TypeError):
            return web.json_response({"error": "Проверьте поля формы."}, status=400)

    app = web.Application(middlewares=[secure], client_max_size=8192)
    app.router.add_get("/itmo/login", page)
    app.router.add_get("/itmo/{name:auth[.](?:js|css)}", asset)
    app.router.add_post("/itmo/session", login)
    if miniapp is not None:
        miniapp.register(app)

    async def reap():
        while True:
            try:
                await service.cleanup()
            except Exception:
                pass
            await asyncio.sleep(60)

    async def lifecycle(_app):
        task = asyncio.create_task(reap())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    return app


async def start_auth_server(service, port, *, miniapp=None):
    runner = web.AppRunner(create_auth_app(service, miniapp=miniapp), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
