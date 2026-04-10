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

from . import tools

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
app = Server("mcp-telegram")


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
        return await tools.tool_runner(args)
    except Exception as e:
        logger.exception("Error running tool: %s", name)
        raise RuntimeError(f"Caught Exception. Error: {e}") from e


# ---------------------------------------------------------------------------
# Listener auto-start
# ---------------------------------------------------------------------------


async def _start_listener_background() -> None:
    """Start the Telegram listener as a background task if enabled."""
    from .listener import TelegramAutoListener
    from .telegram import create_listener_client, get_settings

    settings = get_settings()
    if not settings.listener_enabled:
        return

    if not settings.listener_chats.strip():
        logger.debug("Listener enabled but no chats configured — skipping")
        return

    logger.info("Auto-starting Telegram listener...")
    client = create_listener_client()
    listener = TelegramAutoListener(client)

    try:
        await listener.run()
    except Exception:
        logger.exception("Listener crashed — MCP server continues")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_mcp_server() -> None:
    from mcp.server.stdio import stdio_server

    # Start listener in background (if enabled)
    listener_task = asyncio.create_task(_start_listener_background())

    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        # MCP server closed — clean up listener
        listener_task.cancel()
        try:
            await listener_task
        except (asyncio.CancelledError, Exception):
            pass


def main() -> None:
    asyncio.run(run_mcp_server())
