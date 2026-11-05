import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.compose
@pytest.mark.skipif(not shutil.which("docker"), reason="Docker CLI не установлен")
def test_compose_deployment_preserves_private_ports_and_storage(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / ".env"
    config.write_text(
        "BOT_TOKEN=synthetic\nPOSTGRES_PASSWORD=synthetic\n"
        "POSTGRES_PORT=55432\nITMO_AUTH_PUBLIC_URL=https://bot.example.com\n",
        encoding="utf-8",
    )
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POSTGRES_", "BOT_TOKEN", "COMPOSE_", "ITMO_"))
    }
    base = ["docker", "compose", "--env-file", str(config), "-f", str(root / "compose.yaml")]

    def services(*args):
        rendered = subprocess.check_output(
            [*base, *args, "config", "--format", "json"], env=environ, text=True, timeout=30
        )
        return json.loads(rendered)["services"]

    standard = services()
    assert set(standard) == {"bot", "db"}
    assert all(not service.get("ports") for service in standard.values())
    assert not standard["bot"].get("profiles")
    assert standard["bot"]["environment"]["POSTGRES_HOST"] == "db"
    assert standard["bot"]["volumes"][0]["source"] == "itmo_state"
    assert standard["db"]["volumes"][0]["source"] == "postgres_data"
    assert standard["bot"]["depends_on"]["db"]["condition"] == "service_healthy"

    local = services("-f", str(root / "compose.local.yaml"))
    assert local["db"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert local["db"]["ports"][0]["published"] == "55432"

    itmo = services("-f", str(root / "compose.itmo.yaml"))
    assert set(itmo) == {"bot", "db", "itmo-https"}
    assert {port["target"] for port in itmo["itmo-https"]["ports"]} == {80, 443}
