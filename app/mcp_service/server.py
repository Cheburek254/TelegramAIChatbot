"""Запуск: python -m app.mcp_service.server. stdout занят протоколом MCP."""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime

import aiohttp
import asyncpg
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, ValidationError

from app.agent_store import ActionError, AgentStore
from app.db import ScopedPool, owner_scope
from app.itmo import ITMOClient, ITMOError
from app.mcp_service.auth import verify
from app.mcp_service.contracts import DESCRIPTIONS, INPUTS, OUTPUTS
from app.mcp_service.materials import Materials
from app.mcp_service.weather import ToolFailure, Weather
from app.planner_store import PlannerStore
from app.schedule_source import ScheduleSource
from app.schedule_store import ScheduleStore


def create_server(
    schedule,
    weather,
    reminder_handler,
    secret,
    clock=None,
    timezone="Europe/Moscow",
    city_handler=None,
    materials_handler=None,
    tasks_handler=None,
):
    server = Server("ai-study-assistant", version="2.0.0")
    clock = clock or (lambda: datetime.now(UTC))
    materials = Materials()

    @server.list_tools()
    async def tools():
        return [
            types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                inputSchema=model.model_json_schema(),
                outputSchema=OUTPUTS[name].model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=name != "add_reminder",
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=name in {"get_weather", "get_schedule"},
                ),
            )
            for name, model in INPUTS.items()
        ]

    @server.list_resources()
    async def resources():
        return [
            types.Resource(
                uri=AnyUrl("materials://catalog"),
                name="Каталог учебных материалов",
                mimeType="application/json",
            ),
            types.Resource(
                uri=AnyUrl("schedule://current-week"),
                name="Расписание текущей недели",
                description=(
                    "Личное расписание владельца с понедельника по воскресенье; "
                    "нужен доверенный контекст"
                ),
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def resource(uri):
        if str(uri) == "materials://catalog":
            meta = server.request_context.meta
            owner = verify(secret, meta.model_dump())[0] if meta else None
            with owner_scope(owner):
                catalog = (
                    await materials_handler(owner, None, 5)
                    if owner is not None and materials_handler
                    else materials.catalog()
                )
            from app.material_contracts import Catalog

            return [
                ReadResourceContents(
                    content=Catalog.model_validate(catalog).model_dump_json(),
                    mime_type="application/json",
                )
            ]
        if str(uri) != "schedule://current-week":
            raise ValueError("unknown_resource")
        meta = server.request_context.meta
        owner, _ = verify(secret, meta.model_dump() if meta else {})
        try:
            with owner_scope(owner):
                data = (await schedule.week(owner, clock(), timezone)).model_dump_json()
        except ITMOError as exc:
            raise McpError(types.ErrorData(code=-32000, message=str(exc))) from None
        return [ReadResourceContents(content=data, mime_type="application/json")]

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        try:
            if name not in INPUTS:
                raise ToolFailure("unknown_tool")
            payload = INPUTS[name].model_validate(arguments).model_dump()
            if name == "get_weather":
                meta = server.request_context.meta
                if meta and city_handler:
                    owner, _ = verify(secret, meta.model_dump())
                    with owner_scope(owner):
                        result = await city_handler(owner, payload["city"], weather)
                else:
                    result = await weather.current(payload["city"])
            elif name == "search_materials":
                meta = server.request_context.meta
                owner = verify(secret, meta.model_dump())[0] if meta else None
                with owner_scope(owner):
                    result = (
                        await materials_handler(owner, payload["query"], payload["limit"])
                        if owner is not None and materials_handler
                        else materials.search(**payload)
                    )
            elif name == "get_schedule":
                meta = server.request_context.meta
                owner, _ = verify(secret, meta.model_dump() if meta else {})
                with owner_scope(owner):
                    result = await schedule.day(owner, payload["date"])
            elif name == "get_tasks":
                meta = server.request_context.meta
                owner, _ = verify(secret, meta.model_dump() if meta else {})
                if tasks_handler is None:
                    raise ToolFailure("tool_unavailable")
                with owner_scope(owner):
                    result = await tasks_handler(owner, payload["scope"], clock(), timezone)
            else:
                meta = server.request_context.meta
                owner, action = verify(secret, meta.model_dump() if meta else {})
                with owner_scope(owner):
                    result = await reminder_handler(owner, action, payload, clock())
            output = (
                OUTPUTS[name]
                .model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
                .model_dump()
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(output, ensure_ascii=False))
                ],
                structuredContent=output,
            )
        except ValidationError:
            code = "invalid_arguments_or_result"
        except (ToolFailure, ActionError, ITMOError) as exc:
            code = str(exc)
        except (OSError, ValueError, TypeError, KeyError, asyncpg.PostgresError):
            code = "tool_unavailable"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=code)], isError=True
        )

    return server


async def main():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.CRITICAL)
    pool = None

    async def get_pool():
        nonlocal pool
        if pool is None:
            pool = await asyncpg.create_pool(
                host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                port=int(os.getenv("POSTGRES_PORT", "5432")),
                database=os.getenv("POSTGRES_DB", "bot"),
                user=os.getenv("POSTGRES_USER", "bot"),
                password=os.getenv("POSTGRES_PASSWORD", ""),
                min_size=1,
                max_size=2,
                timeout=5,
                command_timeout=10,
            )
            if os.getenv("POSTGRES_RLS") == "1":
                pool = ScopedPool(pool)
        return pool

    async def reminder(owner, action, payload, now):
        return await AgentStore(await get_pool()).create_reminder(owner, action, payload, now)

    async def tasks(owner, scope, now, timezone):
        return await PlannerStore(await get_pool()).read_tasks(owner, scope, now, timezone)

    async def city_weather(owner, query, weather):
        from app.orchestration.contracts import City
        from app.orchestration.geography import Geography
        from app.orchestration.store import AssistantStore, decode

        profile = await AssistantStore(await get_pool()).profile(owner)
        if profile and profile["city"]:
            city = City.model_validate(decode(profile["city"]))
            if query == city.name + ", " + city.country:
                return await Geography(weather).current(city.model_dump())
        return await weather.current(query)

    async def personal_materials(owner, query, limit):
        from app.material_service import personal_catalog
        from app.material_store import MaterialStore

        store = MaterialStore(await get_pool())
        if query is None:
            return await personal_catalog(store, owner)
        return Materials().search(query, limit, await store.documents(owner))

    class PersonalSchedule:
        async def day(self, owner, value):
            return await ScheduleStore(await get_pool()).day(owner, value)

        async def week(self, owner, now, timezone):
            return await ScheduleStore(await get_pool()).week(owner, now, timezone)

    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            server = create_server(
                ScheduleSource(
                    PersonalSchedule(),
                    ITMOClient(
                        os.getenv("ITMO_STATE_DIR", "/run/itmo"), os.getenv("ITMO_VAULT_KEY_FILE")
                    ),
                ),
                Weather(session),
                reminder,
                os.getenv("MCP_CONTEXT_SECRET", ""),
                timezone=os.getenv("MCP_RESOURCE_TIMEZONE", "Europe/Moscow"),
                city_handler=city_weather,
                materials_handler=personal_materials,
                tasks_handler=tasks,
            )
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())
    finally:
        if pool:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
