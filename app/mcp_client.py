"""Изолированная задача владеет SDK context managers и stdio-процессом."""

import asyncio
import logging
import os
import sys
import uuid
from datetime import timedelta

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from app.material_contracts import Catalog
from app.mcp_service.auth import sign
from app.mcp_service.contracts import WeekOutput

logger = logging.getLogger(__name__)
ITMO_ERRORS = {"itmo_auth_required", "itmo_unavailable", "itmo_invalid_response"}


class MCPFailure(Exception):
    pass


class MCPBridge:
    def __init__(self, settings, secret):
        self.settings, self.secret = settings, secret
        self.tools = {}
        self.resources = []
        self.available = False
        self.queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.task = None
        self.protocol = None

    async def start(self):
        self.task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self.started.wait(), 20)
        except TimeoutError:
            await self.close()
        return self.available

    async def _run(self):
        current = None
        env = {
            key: str(getattr(self.settings, key.lower()))
            for key in (
                "POSTGRES_HOST",
                "POSTGRES_PORT",
                "POSTGRES_DB",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "MCP_RESOURCE_TIMEZONE",
            )
        }
        env.update(
            {
                "MCP_CONTEXT_SECRET": self.secret,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "ITMO_STATE_DIR": str(self.settings.itmo_state_dir),
            }
        )
        for key in ("SYSTEMROOT", "TEMP", "TMP", "PATH"):
            if key in os.environ:
                env[key] = os.environ[key]
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "app.mcp_service.server"], env=env
        )
        try:
            async with (
                stdio_client(params) as (reader, writer),
                ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=50)
                ) as session,
            ):
                initialized = await session.initialize()
                self.protocol = initialized.protocolVersion
                listing = await session.list_tools()
                if listing.nextCursor:
                    raise MCPFailure("unexpected_pagination")
                self.tools = {tool.name: tool for tool in listing.tools}
                for tool in self.tools.values():
                    Draft202012Validator.check_schema(tool.inputSchema)
                    if not tool.outputSchema:
                        raise MCPFailure("missing_output_schema")
                    Draft202012Validator.check_schema(tool.outputSchema)
                self.resources = (await session.list_resources()).resources
                self.available = True
                self.started.set()
                while True:
                    kind, payload, current = await self.queue.get()
                    try:
                        if kind == "tool":
                            name, arguments, meta = payload
                            result = await session.call_tool(name, arguments, meta=meta)
                            if result.isError:
                                code = result.content[0].text if result.content else "tool_error"
                                if code not in ITMO_ERRORS | {
                                    "trusted_context_required",
                                    "confirmation_required",
                                    "action_mismatch",
                                    "expired",
                                    "weather_timeout",
                                    "weather_invalid_response",
                                    "weather_unavailable",
                                    "invalid_arguments_or_result",
                                    "unknown_tool",
                                    "tool_unavailable",
                                }:
                                    code = "tool_error"
                                raise MCPFailure(code)
                            data = result.structuredContent
                            Draft202012Validator(self.tools[name].outputSchema).validate(data)
                        else:
                            uri, metadata = payload
                            result = await session.send_request(
                                types.ClientRequest(
                                    types.ReadResourceRequest(
                                        params=types.ReadResourceRequestParams(
                                            uri=AnyUrl(uri), _meta=metadata
                                        )
                                    )
                                ),
                                types.ReadResourceResult,
                            )
                            model = Catalog if uri == "materials://catalog" else WeekOutput
                            data = model.model_validate_json(result.contents[0].text).model_dump()
                        if not current.done():
                            current.set_result(data)
                    except MCPFailure as exc:
                        if not current.done():
                            current.set_exception(exc)
                    except McpError as exc:
                        code = exc.error.message
                        if not current.done():
                            current.set_exception(
                                MCPFailure(code if code in ITMO_ERRORS else "mcp_invalid_result")
                            )
                    except Exception:
                        if not current.done():
                            current.set_exception(MCPFailure("mcp_invalid_result"))
                        raise
                    finally:
                        current = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("mcp_connection status=unavailable")
        finally:
            self.available = False
            self.started.set()
            if current and not current.done():
                current.set_exception(MCPFailure("mcp_unavailable"))
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(MCPFailure("mcp_unavailable"))

    def schemas(self):
        if not self.available:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in self.tools.values()
        ]

    def validate(self, name, arguments):
        if name not in self.tools:
            raise MCPFailure("unknown_tool")
        try:
            Draft202012Validator(self.tools[name].inputSchema).validate(arguments)
        except Exception:
            raise MCPFailure("invalid_arguments") from None

    async def _submit(self, kind, payload):
        if not self.available:
            raise MCPFailure("mcp_unavailable")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((kind, payload, future))
        try:
            return await asyncio.wait_for(future, 55)
        except TimeoutError:
            raise MCPFailure("mcp_timeout") from None

    async def call(self, name, arguments, *, owner=None, action=None):
        self.validate(name, arguments)
        meta = sign(self.secret, owner, action or uuid.uuid4()) if owner is not None else None
        return await self._submit("tool", (name, arguments, meta))

    async def read_week(self, owner=None):
        meta = sign(self.secret, owner, uuid.uuid4()) if owner is not None else None
        return await self._submit("resource", ("schedule://current-week", meta))

    async def read_catalog(self, owner=None):
        if "materials://catalog" not in {str(item.uri) for item in self.resources}:
            raise MCPFailure("unknown_resource")
        meta = sign(self.secret, owner, uuid.uuid4()) if owner is not None else None
        return await self._submit("resource", ("materials://catalog", meta))

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.available = False
