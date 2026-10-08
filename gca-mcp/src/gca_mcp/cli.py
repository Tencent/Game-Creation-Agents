"""CLI entry point for the GCA MCP server.

Two launch modes (mirrors video-creation-mcp):

**Eager** — pass ``--workspace-root`` and the server opens the workspace
before accepting any MCP messages::

    gca-mcp --workspace-root ./playground/my-game

**Lazy** — omit ``--workspace-root`` and the server boots with no
workspace. The agent's first move is ``init_workspace(path=..., mode=...)``
MCP tool. Suitable when the agent picks the playground subfolder per
session::

    gca-mcp

The repo's ``.env`` file is auto-loaded. Provider credentials are read from
``OPENAI_API_KEY`` and ``ARK_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import Config, load_dotenv
from .workspace import open_workspace


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gca-mcp",
        description="MCP server for Game Creative Agent: image/video gen + verification.",
    )
    p.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Optional. Game directory to bind eagerly. When omitted the "
            "agent must call init_workspace(path=..., mode=...) first."
        ),
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Path to the gca repo root (where .claude/skills/ lives). "
            "Defaults to the cwd when the server starts."
        ),
    )
    p.add_argument(
        "--workspace-mode",
        choices=("create", "resume"),
        default="resume",
        help="Eager workspace intent. Defaults to resume.",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Friendly workspace name. Defaults to folder basename.",
    )
    p.add_argument(
        "--init-only",
        action="store_true",
        help="Open the workspace and exit (no MCP server).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 1. Load .env from cwd / parents before validating provider credentials.
    load_dotenv()
    args = _parse_args(argv)

    # 2. Decide repo_root.
    #
    # Priority:
    #   1. Explicit --repo-root.
    #   2. The parent of the gca-mcp package itself when it sits inside the
    #      gca repo (i.e. <repo>/gca-mcp/src/gca_mcp/cli.py → <repo>).
    #   3. Process cwd (last resort).
    if args.repo_root is not None:
        repo_root = args.repo_root.resolve()
    else:
        cli_path = Path(__file__).resolve()
        # cli.py → gca_mcp → src → gca-mcp → <repo>
        candidate = cli_path.parents[3] if len(cli_path.parents) >= 4 else None
        if candidate and (candidate / ".claude" / "skills").is_dir():
            repo_root = candidate
        else:
            repo_root = Path.cwd().resolve()

    if args.init_only and args.workspace_root is None:
        print(
            "ERROR: --init-only requires --workspace-root.",
            file=sys.stderr,
        )
        return 2

    workspace = None
    if args.workspace_root is not None:
        try:
            workspace = open_workspace(
                args.workspace_root,
                repo_root=repo_root,
                mode=args.workspace_mode,
                name=args.name,
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: failed to open workspace: {e}", file=sys.stderr)
            return 2
        print(
            f"[gca-mcp] workspace ready: name={workspace.name!r} root={workspace.root}",
            file=sys.stderr,
        )
    else:
        print(
            "[gca-mcp] no --workspace-root; lazy mode. Agent must call "
            "init_workspace(path=…, mode=…) before workspace-gated tools.",
            file=sys.stderr,
        )

    if args.init_only:
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "ERROR: OPENAI_API_KEY is required (set it in .env). "
            "See .env.example.",
            file=sys.stderr,
        )
        return 2

    config = Config.from_env(config_path=repo_root / "gca.config.yaml")

    from .server import run_stdio_server

    asyncio.run(
        run_stdio_server(
            config=config,
            workspace=workspace,
            repo_root=repo_root,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
