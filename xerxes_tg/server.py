from __future__ import annotations

import asyncio
import inspect
import logging
import typing as t
from collections.abc import Sequence
from functools import cache

from mcp.server import Server
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    Prompt,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

from . import audit, ratelimit, tools
from .instructions import INSTRUCTIONS

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
logger.warning("xerxes-tg loading tools from %s", getattr(tools, "__file__", "<unknown>"))
app = Server("xerxes-tg", instructions=INSTRUCTIONS)


@cache
def enumerate_available_tools() -> t.Generator[tuple[str, Tool], t.Any, None]:
    for _, tool_args in inspect.getmembers(tools, inspect.isclass):
        if issubclass(tool_args, tools.ToolArgs) and tool_args != tools.ToolArgs:
            logger.debug("Found tool: %s", tool_args)
            description = tools.tool_description(tool_args)
            yield description.name, description


mapping: dict[str, Tool] = dict(enumerate_available_tools())


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """List available prompts."""
    return []


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available resources."""
    return []


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return list(mapping.values())


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    """List available resource templates."""
    return []


@app.progress_notification()
async def progress_notification(pogress: str | int, p: float, s: float | None) -> None:
    """Progress notification."""


@app.call_tool()
async def call_tool(name: str, arguments: t.Any) -> Sequence[TextContent | ImageContent | EmbeddedResource]:  # noqa: ANN401
    """Handle tool calls for command line run."""

    if not isinstance(arguments, dict):
        raise TypeError("arguments must be dictionary")

    tool = mapping.get(name)
    if not tool:
        raise ValueError(f"Unknown tool: {name}")

    try:
        args = tools.tool_args(tool, **arguments)
    except Exception as e:
        audit.write(name, arguments, ok=False, error=f"bad_args: {e}")
        raise RuntimeError(f"Invalid arguments for {name}: {e}") from e

    # Local rate-limit (pre-flight). Gets logged via audit on rejection.
    action = ratelimit.classify_tool(name)
    if action:
        did = getattr(args, "dialog_id", None)
        key: int | None
        if isinstance(did, str):
            key = hash(did.lower())
        elif isinstance(did, int):
            key = did
        else:
            key = None
        try:
            ratelimit.check_and_consume(action, key)
        except ratelimit.RateLimitExceeded as e:
            audit.write(name, arguments, ok=False, error=f"rate_limit: {e}")
            raise RuntimeError(str(e)) from e

    try:
        result = await tools.tool_runner(args)
    except Exception as e:
        audit.write(name, arguments, ok=False, error=f"{type(e).__name__}: {e}")
        logger.exception("Error running tool: %s", name)
        raise RuntimeError(f"Caught Exception. Error: {e}") from e

    preview = ""
    try:
        if isinstance(result, list) and result:
            first_text = getattr(result[0], "text", "")
            if name == "LaunchMiniApp":
                preview = "[authenticated Mini App URL redacted]"
            else:
                preview = first_text[:200]
    except Exception:
        preview = ""
    audit.write(name, arguments, ok=True, result_preview=preview)
    return result


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_mcp_server() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    asyncio.run(run_mcp_server())
