from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.itmo import CLI, ITMOClient, ITMOError
from app.presentation import main_keyboard, section_keyboard, settings_keyboard
from app.schedule_contracts import ScheduleRepeat

RULE = {
    "date": "2026-12-28",
    "until": "2027-01-18",
    "weekday": 1,
    "parity": "odd",
    "anchor": "2026-12-30",
    "start": "10:00",
    "end": "11:30",
    "title": "Тест",
    "location": "A",
}


@pytest.mark.parametrize(
    "parity,expected",
    [
        ("odd", ["2026-12-28", "2027-01-11"]),
        ("even", ["2027-01-04", "2027-01-18"]),
        ("all", ["2026-12-28", "2027-01-04", "2027-01-11", "2027-01-18"]),
    ],
)
def test_academic_parity_does_not_reset_at_new_year(parity, expected):
    rule = ScheduleRepeat.model_validate({**RULE, "parity": parity})
    assert [d.isoformat() for d in rule.dates()] == expected
    assert [row["date"] for row in rule.lessons()] == expected


@pytest.mark.parametrize(
    "patch",
    [
        {"weekday": 0},
        {"weekday": 8},
        {"parity": "iso"},
        {"until": "2026-12-01"},
        {"until": "2028-01-01"},
        {"anchor": "2027-01-04"},
        {"until": "2026-12-28", "weekday": 2},
    ],
)
def test_invalid_or_empty_repeating_series_rejected(patch):
    with pytest.raises(ValidationError):
        ScheduleRepeat.model_validate({**RULE, **patch})


def test_root_menu_hides_nested_actions_and_back_targets():
    root = [button.callback_data for row in main_keyboard().inline_keyboard for button in row]
    assert "schedule:menu" in root and "agent:tools" in root
    assert not {"reset", "agent:timezone", "agent:why", "schedule:put", "schedule:get"} & set(root)
    settings = [b for row in settings_keyboard().inline_keyboard for b in row]
    assert {"reset", "menu"} <= {b.callback_data for b in settings}
    assert any(b.copy_text and b.copy_text.text == "/timezone " for b in settings)
    for name, target in [
        ("schedule", "menu"),
        ("tools", "menu"),
        ("put", "schedule:menu"),
        ("get", "schedule:menu"),
        ("repeat", "schedule:put"),
    ]:
        assert section_keyboard(name).inline_keyboard[-1][0].callback_data == target


RAW = {
    "code": 0,
    "data": [
        {
            "date": "2026-11-02",
            "lessons": [
                {
                    "subject": "Синтетический предмет",
                    "time_start": "10:00",
                    "time_end": "11:30",
                    "building": "Учебный корпус",
                    "room": "301",
                    "teacher_name": "Не сохраняется",
                    "zoom_password": "Не сохраняется",
                    "unknown": "Игнорируется",
                }
            ],
        }
    ],
}


def test_itmo_exact_fields_and_no_teacher_or_meeting_secrets():
    result = ITMOClient.parse(RAW, date(2026, 11, 2), date(2026, 11, 8))
    assert result == [
        {
            "date": "2026-11-02",
            "start": "10:00",
            "end": "11:30",
            "title": "Синтетический предмет",
            "location": "Учебный корпус, 301",
        }
    ]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"code": 1, "data": []},
        {"code": 0, "data": {}},
        {"code": 0, "data": [{"date": "2026-11-01", "lessons": []}]},
        {"code": 0, "data": [{"date": "2026-11-02", "lessons": [{}]}]},
    ],
)
def test_itmo_invalid_response_never_imported(raw):
    with pytest.raises(ITMOError, match="itmo_invalid_response"):
        ITMOClient.parse(raw, date(2026, 11, 2), date(2026, 11, 8))


async def test_itmo_session_is_owner_scoped_and_results_are_normalized(tmp_path):
    directory = tmp_path / "42"
    directory.mkdir()
    (directory / "state.json").write_text("{}")
    client = ITMOClient(tmp_path)
    body = {
        "range": {"from": "2026-11-02", "to": "2026-11-08"},
        "days": [
            {
                "date": "2026-11-02",
                "lessons": [
                    {
                        "time": "10:00-11:30",
                        "subject": "Тест",
                        "teacher": "Не передаётся",
                        "online_password": "Не передаётся",
                    }
                ],
            }
        ],
    }
    client._exchange = AsyncMock(return_value=body)
    with pytest.raises(ITMOError, match="itmo_not_connected"):
        await client.fetch(73, date(2026, 11, 2), date(2026, 11, 8))
    client._exchange.assert_not_called()
    rows = await client.fetch(42, date(2026, 11, 2), date(2026, 11, 8))
    assert rows == [
        {
            "date": "2026-11-02",
            "start": "10:00",
            "end": "11:30",
            "title": "Тест",
            "location": "Место не указано ИТМО",
        }
    ]
    runtime, first, last = client._exchange.await_args.args
    assert runtime != directory and not runtime.exists()
    assert (first, last) == (date(2026, 11, 2), date(2026, 11, 8))
    assert (directory / "state.enc").exists() and not (directory / "state.json").exists()
    client._exchange.return_value = {"range": {"from": "2026-11-01", "to": "2026-11-08"}}
    with pytest.raises(ITMOError, match="itmo_invalid_response"):
        await client.fetch(42, date(2026, 11, 2), date(2026, 11, 8))


@pytest.mark.skipif(not Path(CLI).is_file(), reason="itmo-mcp установлен внутри Docker")
async def test_real_itmo_mcp_interop_is_read_only_and_denies_without_account(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="node",
        args=[CLI, "--stdio"],
        env={"ITMO_MCP_STATE_DIR": str(tmp_path), "ITMO_MCP_ENABLE_WRITES": "false"},
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert "itmo_get_schedule" in {tool.name for tool in tools}
        assert all(tool.annotations.readOnlyHint for tool in tools)
        result = await session.call_tool(
            "itmo_get_schedule", {"date_from": "2026-11-02", "date_to": "2026-11-08"}
        )
        assert result.isError
    client = ITMOClient(tmp_path)
    with pytest.raises(ITMOError, match="itmo_auth_required"):
        await client._exchange(tmp_path, date(2026, 11, 2), date(2026, 11, 8))


async def test_connection_helper_writes_private_refresh_state_without_overwriting(tmp_path):
    import os

    token = "synthetic-refresh-token-for-test-only"
    client = ITMOClient(tmp_path)
    async with client.session_lock(42):
        await client.install(42, token, "one", replace=False, bot_issued=False)
    target = tmp_path / "42" / "state.enc"
    assert token.encode() not in target.read_bytes()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    async with client.session_lock(42):
        with pytest.raises(ValueError, match="already_connected"):
            await client.install(42, "other-synthetic-refresh-token", "two", replace=False)
    assert client.vault.load(42)["sdk"]["refreshToken"] == token
