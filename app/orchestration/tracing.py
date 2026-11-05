"""Только технические атрибуты; контекст трассы изолирован между asyncio-задачами."""

import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

current_trace = ContextVar("study_trace", default=None)
parent_span = ContextVar("study_span", default=None)
ATTRIBUTES = {
    "tool_name",
    "model_name",
    "input_tokens",
    "output_tokens",
    "error_code",
    "target",
    "resource",
    "document_ids",
    "intent",
}


class Trace:
    def __init__(self, store, owner, *, clock=None, kind="user_query"):
        self.store, self.owner = store, owner
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id, self.request = uuid.uuid4(), uuid.uuid4()
        self.kind = kind

    async def __aenter__(self):
        self.base_time = self.clock()
        self.origin = time.monotonic()
        await self.store.begin_trace(self.owner, self.id, self.request, self.kind, self.base_time)
        self.token = current_trace.set(self)
        return self

    def stamp(self):
        return self.base_time + timedelta(seconds=time.monotonic() - self.origin)

    async def __aexit__(self, exc_type, exc, tb):
        current_trace.reset(self.token)
        await self.store.finish_trace(
            self.id, "failed" if exc else getattr(self, "status", "success")
        )


@asynccontextmanager
async def span(component, operation, **attributes):
    trace = current_trace.get()
    attrs = {key: value for key, value in attributes.items() if key in ATTRIBUTES}
    if not trace:
        yield attrs
        return
    identity, parent, start = uuid.uuid4(), parent_span.get(), trace.stamp()
    token = parent_span.set(identity)
    status = "success"
    try:
        yield attrs
    except BaseException:
        status = "failed"
        attrs["error_code"] = "stage_failed"
        raise
    finally:
        parent_span.reset(token)
        await trace.store.span(
            identity,
            trace.id,
            parent,
            component,
            operation,
            start,
            trace.stamp(),
            status,
            {k: v for k, v in attrs.items() if k in ATTRIBUTES},
        )


class TracedLLM:
    def __init__(self, client, component):
        self.client, self.component = client, component

    async def complete(self, *args, **kwargs):
        async with span(self.component, "llm") as attrs:
            result = await self.client.complete(*args, **kwargs)
            attrs.update(
                model_name=result.model,
                input_tokens=result.usage.get("prompt_tokens"),
                output_tokens=result.usage.get("completion_tokens"),
            )
            return result
