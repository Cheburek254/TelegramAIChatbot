"""Единственное место чтения и проверки настроек приложения."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values


class ConfigError(ValueError):
    """Ошибка настройки без секретных значений в сообщении."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    postgres_password: str = field(repr=False)
    telegram_proxy_url: str = field(default="", repr=False)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "bot"
    postgres_user: str = "bot"
    log_level: str = "INFO"
    health_port: int = 8080
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "nvidia/nemotron-3.5-super-vl:free"
    groq_api_key: str = field(default="", repr=False)
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"
    llm_provider: str = "openrouter"
    llm_timeout: int = 30
    llm_max_tokens: int = 1600
    history_max_messages: int = 20
    history_max_tokens: int = 6000
    mcp_enabled: bool = True
    mcp_resource_timezone: str = "Europe/Moscow"
    itmo_state_dir: str = "/run/itmo"
    itmo_auth_public_url: str = ""
    itmo_auth_port: int = 8081
    supervisor_enabled: bool = True
    scheduler_interval: int = 30
    briefing_timeout: int = 50

    def validate_llm(self) -> None:
        if not self.openrouter_model.endswith(":free"):
            raise ConfigError("OPENROUTER_MODEL: разрешены только явные модели с суффиксом :free.")
        key = self.openrouter_api_key if self.llm_provider == "openrouter" else self.groq_api_key
        if not key:
            raise ConfigError(
                f"{self.llm_provider.upper()}_API_KEY: задайте ключ выбранного сервиса."
            )

    @classmethod
    def load(
        cls, env_file: Path | str = ".env", *, environ: Mapping[str, str] | None = None
    ) -> "Settings":
        values = {
            **dotenv_values(env_file, interpolate=False),
            **(os.environ if environ is None else environ),
        }

        def value(key: str, default: str = "") -> str:
            return values.get(key) or default

        def port(key: str, default: str) -> int:
            try:
                result = int(value(key, default))
                if not 1 <= result <= 65535:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужен номер порта от 1 до 65535.") from None

        token = value("BOT_TOKEN")
        try:
            validate_token(token)
        except TokenValidationError:
            raise ConfigError("BOT_TOKEN: укажите токен, полученный у BotFather.") from None
        password = value("POSTGRES_PASSWORD")
        if not password:
            raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")
        proxy = value("TELEGRAM_PROXY_URL")
        if proxy:
            try:
                parsed = urlsplit(proxy)
                if (
                    parsed.scheme not in {"http", "socks5"}
                    or not parsed.hostname
                    or not parsed.port
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
            except ValueError:
                raise ConfigError(
                    "TELEGRAM_PROXY_URL: нужен http://host:port или socks5://host:port; "
                    "при необходимости добавьте user:password@."
                ) from None
        level = value("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL: используйте DEBUG, INFO, WARNING, ERROR или CRITICAL.")

        def integer(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(value(key, str(default)))
                if not minimum <= number <= maximum:
                    raise ValueError
                return number
            except ValueError:
                raise ConfigError(f"{key}: нужно целое число от {minimum} до {maximum}.") from None

        def base_url(key: str, default: str) -> str:
            address = value(key, default).rstrip("/")
            try:
                parsed = urlsplit(address)
            except ValueError:
                raise ConfigError(f"{key}: некорректный адрес API.") from None
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ConfigError(f"{key}: нужен HTTPS-адрес API без учётных данных.")
            return address

        provider = value("LLM_PROVIDER", "openrouter")
        if provider not in {"openrouter", "groq"}:
            raise ConfigError("LLM_PROVIDER: допустимы openrouter и groq.")
        return cls(
            bot_token=token,
            postgres_password=password,
            telegram_proxy_url=proxy,
            postgres_host=value("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=port("POSTGRES_PORT", "5432"),
            postgres_db=value("POSTGRES_DB", "bot"),
            postgres_user=value("POSTGRES_USER", "bot"),
            log_level=level,
            health_port=port("HEALTH_PORT", "8080"),
            openrouter_api_key=value("OPENROUTER_API_KEY", value("OPENROUTER_APIKEY")),
            openrouter_base_url=base_url("OPENROUTER_BASE_URL", cls.openrouter_base_url),
            openrouter_model=value("OPENROUTER_MODEL", cls.openrouter_model),
            groq_api_key=value("GROQ_API_KEY"),
            groq_base_url=base_url("GROQ_BASE_URL", cls.groq_base_url),
            groq_model=value("GROQ_MODEL", cls.groq_model),
            llm_provider=provider,
            llm_timeout=integer("LLM_TIMEOUT", 30, 1, 120),
            llm_max_tokens=integer("LLM_MAX_TOKENS", 1600, 64, 8192),
            history_max_messages=integer("HISTORY_MAX_MESSAGES", 20, 1, 100),
            history_max_tokens=integer("HISTORY_MAX_TOKENS", 6000, 512, 64000),
            mcp_enabled=value("MCP_ENABLED", "1") == "1",
            mcp_resource_timezone=value("MCP_RESOURCE_TIMEZONE", "Europe/Moscow"),
            itmo_state_dir=value("ITMO_STATE_DIR", cls.itmo_state_dir),
            itmo_auth_public_url=value("ITMO_AUTH_PUBLIC_URL").rstrip("/"),
            itmo_auth_port=port("ITMO_AUTH_PORT", "8081"),
            supervisor_enabled=value("SUPERVISOR_ENABLED", "1") == "1",
            scheduler_interval=integer("SCHEDULER_INTERVAL", 30, 5, 300),
            briefing_timeout=integer("BRIEFING_TIMEOUT", 50, 1, 60),
        )
