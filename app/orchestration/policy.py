"""Права определяет приложение перед каждым вызовом, а не имя из ответа LLM."""

from app.mcp_client import MCPFailure
from app.orchestration.tracing import span

TOOLS = {
    "router": frozenset(),
    "schedule": frozenset({"get_schedule", "add_reminder"}),
    "tasks": frozenset({"get_tasks"}),
    "knowledge": frozenset({"search_materials"}),
    "lifestyle": frozenset({"get_weather"}),
    "legacy": frozenset(),
}
RESOURCES = {
    "schedule": frozenset({"schedule://current-week"}),
    "knowledge": frozenset({"materials://catalog"}),
}


class PolicyBridge:
    def __init__(self, bridge, component, *, readonly=False, owner=None):
        if component not in TOOLS:
            raise ValueError("unknown_component")
        self.bridge, self.component, self.readonly = bridge, component, readonly
        self.owner = owner
        self.calls, self.results = [], []

    @property
    def available(self):
        return self.bridge.available

    @property
    def tools(self):
        return {
            key: value
            for key, value in self.bridge.tools.items()
            if key in TOOLS[self.component] and not (self.readonly and key == "add_reminder")
        }

    def schemas(self):
        return [
            schema for schema in self.bridge.schemas() if schema["function"]["name"] in self.tools
        ]

    def validate(self, name, arguments):
        if name not in self.tools:
            raise MCPFailure("policy_denied")
        self.bridge.validate(name, arguments)

    async def call(self, name, arguments, **trusted):
        async with span(
            self.component, "mcp", tool_name=name if name in TOOLS[self.component] else "denied"
        ):
            self.validate(name, arguments)
            if self.owner is not None:
                trusted["owner"] = self.owner
            result = await self.bridge.call(name, arguments, **trusted)
            self.calls.append(name)
            self.results.append((name, result))
            return result

    async def resource(self, uri, owner=None):
        async with span(
            self.component,
            "resource",
            resource=uri if uri in RESOURCES.get(self.component, ()) else "denied",
        ):
            discovered = {str(item.uri) for item in self.bridge.resources}
            if uri not in RESOURCES.get(self.component, ()) or uri not in discovered:
                raise MCPFailure("policy_denied")
            result = (
                await self.bridge.read_week(self.owner if self.owner is not None else owner)
                if uri == "schedule://current-week"
                else await self.bridge.read_catalog(self.owner)
            )
            self.results.append((uri, result))
            return result
