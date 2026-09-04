"""Explicitly allowlisted, read-only MCP tools for Lose It!."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from . import __version__
from .config import Settings
from .errors import translate
from .observability import account_tag, current_request_id, install_tool_logging
from .readonly_service import ReadOnlyLoseItService
from .sealed import UrlSealer
from .tenancy import SessionResolver, resolve_or_raise

logger = logging.getLogger(__name__)

READ_ONLY_TOOL_ALLOWLIST = frozenset(
    {
        "search_food",
        "describe_food",
        "get_diary",
        "get_diary_range",
        "get_weight_history",
        "server_status",
        "whoami",
    }
)

INSTRUCTIONS = """\
Read-only access to the user's Lose It! food diary, food database, and weight
history. This server cannot log, update, or delete anything. Entry IDs are
informational source identifiers only. Missing nutrients are unknown, never
zero and never estimated by the retrieval layer. Compare user-submitted search
candidates; protein*4 + carb*4 + fat*9 should be reasonably close to calories.
"""


def _cell(value: Any) -> str:
    """Flatten untrusted food text before placing it in a delimited row."""
    flattened = " ".join(str(value).split())
    return flattened.replace("|", "/")


def _gram(nutrients: dict[str, Any], key: str) -> str:
    value = nutrients.get(key)
    if not isinstance(value, int | float):
        return "?"
    if 0 < abs(value) < 1:
        return f"{value:.2g}"
    return f"{round(value):g}"


def _format_search(query: str, hits: list[dict[str, Any]]) -> str:
    safe_query = _cell(query)
    if not hits:
        return f'No matches for "{safe_query}". Try a shorter or more common name.'
    lines = [
        f'{len(hits)} matches for "{safe_query}". Values are per the serving shown.',
        "name (brand) | cal | protein/carb/fat g | serving | food_id",
    ]
    for index, hit in enumerate(hits, 1):
        name = _cell(hit.get("name") or "(unnamed)")
        brand = hit.get("brand")
        label = f"{name} ({_cell(brand)})" if brand else name
        food_id = _cell(hit.get("food_id") or "?")
        nutrients = hit.get("nutrients_per_serving") or {}
        if not hit.get("nutrition_available") or not nutrients:
            lines.append(
                f"{index}. {label} | ? | ?/?/? | ? | {food_id}  (nutrition unavailable)"
            )
            continue
        serving = hit.get("primary_serving") or {}
        amount = serving.get("native_qty_per_serving")
        unit = _cell(serving.get("unit") or "serving")
        portion = f"{round(amount, 3):g} {unit}" if isinstance(amount, int | float) else unit
        lines.append(
            f'{index}. {label} | {_gram(nutrients, "calories")} | '
            f'{_gram(nutrients, "protein_g")}/{_gram(nutrients, "carb_g")}'
            f'/{_gram(nutrients, "total_fat_g")} | {portion} | {food_id}'
        )
    return "\n".join(lines)


def build_server(
    settings: Settings,
    *,
    multi_tenant: bool = False,
    sealer: UrlSealer | None = None,
) -> MCPServer:
    """Build a server whose discovered surface is exactly the allowlist."""
    shared = None if multi_tenant else ReadOnlyLoseItService(settings)
    resolver = SessionResolver(settings) if multi_tenant else None

    @contextmanager
    def acquire(ctx: Context) -> Iterator[ReadOnlyLoseItService]:
        try:
            if resolver is None:
                assert shared is not None
                yield shared
                return
            delegate, creds = resolve_or_raise(resolver, ctx.headers or {}, sealer)
            logger.info(
                "account=%s id=%s resolved",
                account_tag(getattr(creds, "email", None)),
                current_request_id() or "-",
            )
            service = ReadOnlyLoseItService(settings, delegate=delegate)
            try:
                yield service
            finally:
                service.close()
        except Exception as exc:
            translated = translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if shared is not None:
                shared.close()

    mcp = MCPServer(
        name="loseit-readonly",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @mcp.tool(
        description=(
            "Search Lose It!'s user-submitted food database. Returns sanitized "
            "candidate rows and informational food IDs; it never logs food."
        ),
        structured_output=False,
    )
    def search_food(
        ctx: Context,
        query: Annotated[str, Field(description="Food name to search for.")],
        limit: Annotated[int, Field(description="Maximum results.", ge=1, le=50)] = 10,
    ) -> str:
        with acquire(ctx) as svc:
            return _format_search(query, svc.search_food(query, limit=limit))

    @mcp.tool(description="Read complete source-provided nutrition and serving details for a food.")
    def describe_food(
        ctx: Context,
        food_id: Annotated[str, Field(description="32-character hexadecimal food ID.")],
    ) -> dict[str, Any]:
        with acquire(ctx) as svc:
            return svc.describe_food(food_id)

    @mcp.tool(
        description=(
            "Read one diary day, including entries, source-provided nutrients, and "
            "informational entry IDs. No update or delete operation exists."
        )
    )
    def get_diary(
        ctx: Context,
        date: Annotated[
            str | None,
            Field(description="Day: YYYY-MM-DD, today, or yesterday."),
        ] = None,
    ) -> dict[str, Any]:
        with acquire(ctx) as svc:
            return svc.get_diary(date)

    @mcp.tool(
        description=(
            "Read an inclusive diary range of at most 31 calendar days. Each day "
            "contains all entries, source nutrients, totals, and coverage metadata."
        )
    )
    def get_diary_range(
        ctx: Context,
        start_date: Annotated[str, Field(description="Inclusive start, YYYY-MM-DD.")],
        end_date: Annotated[str, Field(description="Inclusive end, YYYY-MM-DD.")],
    ) -> dict[str, Any]:
        with acquire(ctx) as svc:
            return svc.get_diary_range(start_date, end_date)

    @mcp.tool(description="Read recorded weight history and summary; never records a weigh-in.")
    def get_weight_history(
        ctx: Context,
        start: Annotated[str | None, Field(description="First day, YYYY-MM-DD.")] = None,
        end: Annotated[str | None, Field(description="Last day, YYYY-MM-DD.")] = None,
        days: Annotated[int, Field(description="Window when start is omitted.", ge=1, le=365)] = 30,
    ) -> dict[str, Any]:
        with acquire(ctx) as svc:
            return svc.get_weight_history(start=start, end=end, days=days)

    @mcp.tool(description="Diagnose server build, authentication, and read connectivity status.")
    def server_status(ctx: Context) -> dict[str, Any]:
        from . import build_info

        status: dict[str, Any] = {
            "server": "loseit-readonly",
            "build": build_info(),
            "read_only": True,
            "tool_allowlist": sorted(READ_ONLY_TOOL_ALLOWLIST),
            "mode": "multi-tenant" if resolver is not None else "single-account",
        }
        try:
            with acquire(ctx) as svc:
                identity = svc.whoami()
                status["authenticated"] = True
                status["account"] = {
                    "user_name": identity.get("user_name"),
                    "email": identity.get("email"),
                    "hours_from_gmt": identity.get("hours_from_gmt"),
                }
                try:
                    svc.search_food("water", limit=1, detail=False)
                    status["loseit_reachable"] = True
                except Exception as exc:  # noqa: BLE001
                    status["loseit_reachable"] = False
                    status["loseit_error"] = str(translate(exc))[:400]
        except Exception as exc:  # noqa: BLE001
            status["authenticated"] = False
            status["auth_error"] = str(translate(exc))[:400]
        status["ok"] = bool(status.get("authenticated") and status.get("loseit_reachable"))
        return status

    @mcp.tool(description="Show the account identity associated with the session token.")
    def whoami(ctx: Context) -> dict[str, Any]:
        with acquire(ctx) as svc:
            return svc.whoami()

    actual = frozenset(tool.name for tool in mcp._tool_manager.list_tools())
    if actual != READ_ONLY_TOOL_ALLOWLIST:
        raise RuntimeError(
            "Refusing to start: MCP tool registry differs from the reviewed read-only "
            f"allowlist (unexpected={sorted(actual - READ_ONLY_TOOL_ALLOWLIST)}, "
            f"missing={sorted(READ_ONLY_TOOL_ALLOWLIST - actual)})."
        )

    install_tool_logging(mcp)
    return mcp
