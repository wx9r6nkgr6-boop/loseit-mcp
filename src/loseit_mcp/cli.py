"""Read-only command line and MCP server launcher."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .auth import save_token
from .config import ConfigError, load_settings
from .readonly_service import ReadOnlyLoseItService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loseit-mcp",
        description="Read-only access to Lose It! food, diary, and weight data.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument("--config", type=Path, help="Optional JSON configuration path.")
    parser.add_argument("--env-file", type=Path, help="Optional environment file path.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the read-only MCP server.")
    serve.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--path", default="/mcp")

    sub.add_parser("import-token", help="Securely save a liauth session token (hidden prompt).")
    sub.add_parser("whoami", help="Show the authenticated account.")
    sub.add_parser("status", help="Test read-only server authentication and connectivity.")

    search = sub.add_parser("search", help="Search foods without logging anything.")
    search.add_argument("query")
    search.add_argument("-n", "--limit", type=int, default=10)

    describe = sub.add_parser("describe", help="Read nutrition detail for a food.")
    describe.add_argument("food_id")

    diary = sub.add_parser("diary", help="Read one diary day.")
    diary.add_argument("date", nargs="?")

    diary_range = sub.add_parser("diary-range", help="Read up to 31 inclusive diary days.")
    diary_range.add_argument("start_date")
    diary_range.add_argument("end_date")

    weights = sub.add_parser("weights", help="Read weight history.")
    weights.add_argument("--start")
    weights.add_argument("--end")
    weights.add_argument("--days", type=int, default=30)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _print_logged(result: dict[str, Any]) -> None:
    """Legacy formatter retained for upstream test compatibility; unreachable."""
    prefix = "DRY RUN — would log" if result.get("dry_run") else "Logged"
    food = result.get("food") or {}
    calories = result.get("calories")
    suffix = f" ({calories:.0f} cal)" if isinstance(calories, int | float) else ""
    print(
        f"{prefix}: {food.get('name')} — {result.get('portion_size')} "
        f"{result.get('measure_unit')} to {result.get('meal')} on {result.get('date')}{suffix}"
    )


def _add_health_route(mcp: Any) -> None:
    """Dependency-free process health endpoint for local HTTP deployments."""
    from starlette.responses import JSONResponse

    from . import build_info
    from .throttle import client_key

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "read_only": True,
                "build": build_info(),
                "client": {"resolved": client_key(_request.scope, 1), "trusted_proxies": 1},
            }
        )


def _settings(args: argparse.Namespace):
    return load_settings(config_file=args.config, env_file=args.env_file)


def _serve(args: argparse.Namespace) -> int:
    from .server import build_server

    settings = _settings(args)
    settings.require_credentials()
    mcp = build_server(settings)
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        _add_health_route(mcp)
        mcp.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    return 0


def _status(service: ReadOnlyLoseItService) -> dict[str, Any]:
    identity = service.whoami()
    service.search_food("water", limit=1, detail=False)
    return {
        "ok": True,
        "read_only": True,
        "account": {
            "user_name": identity.get("user_name"),
            "email": identity.get("email"),
            "hours_from_gmt": identity.get("hours_from_gmt"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import-token":
            settings = _settings(args)
            token = getpass.getpass("Paste Lose It! liauth token (input hidden): ")
            save_token(token, settings.token_file)
            print(f"Saved an owner-only token at {settings.token_file}")
            return 0
        if args.command == "serve":
            return _serve(args)

        with ReadOnlyLoseItService(_settings(args)) as service:
            if args.command == "whoami":
                result = service.whoami()
            elif args.command == "status":
                result = _status(service)
            elif args.command == "search":
                result = service.search_food(args.query, limit=args.limit)
            elif args.command == "describe":
                result = service.describe_food(args.food_id)
            elif args.command == "diary":
                result = service.get_diary(args.date)
            elif args.command == "diary-range":
                result = service.get_diary_range(args.start_date, args.end_date)
            elif args.command == "weights":
                result = service.get_weight_history(
                    start=args.start, end=args.end, days=args.days
                )
            else:  # pragma: no cover - argparse owns command validation
                raise AssertionError(args.command)
        _print(result)
        return 0
    except (ConfigError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
