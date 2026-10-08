# GCA MCP Development Instructions

These rules apply to `gca-mcp/**`. They govern repository development, not the
generated Phaser games under `playground/`.

## Architecture

- Python 3.11+ is the implementation language; the installed entry point is
  `gca_mcp.cli:main`, which launches `server.run_stdio_server`. Tool modules register
  colocated `ToolDef` schema/handler pairs.
- MCP ToolDefs are the public API source of truth. Keep descriptions, schemas,
  handlers, README entries, and agent calls synchronized.
- Preserve `additionalProperties: false`; expose a parameter deliberately rather
  than accepting silent extras.
- Pipeline agents call MCP tools. Repository developers may directly run lint,
  tests, compile checks, and focused scripts for implementation validation.
- Vendored Node/Python code remains behind MCP wrappers; wrappers must anchor paths
  to the active workspace and return structured, current-run evidence.

## Asset and Evidence Contracts

- Generated stills use `produce_static_asset`; supplied/licensed stills use
  `register_existing_static_asset` so pixels are preserved and metadata is sealed.
- Sprite bodies use `produce_validated_sprite_action`; final runtime output uses
  `pack_spritesheet`. Keep bodies separate from effects. `art_directed` uses
  runtime recipes; other profiles may declare a separate FX flipbook.
- Destructive output pipelines require owned subdirectories, run isolation, and
  stale-output cleanup. Never reuse an artifact without hash/run/provenance checks.
- Validators fail closed on missing, duplicate, unknown, contradictory, or stale
  evidence. Distinguish a real content failure from an operational provider outage.
- Bounded visual-provider exhaustion is `status: unavailable`, never a fabricated
  pass; follow the policy in `gca.config.yaml`.
- Every behavior fix requires a regression test that exercises the production path,
  not only a textual/schema assertion.

## Quality Gate

Run from `gca-mcp/` before committing:

```bash
uv run --extra dev ruff check src tests
uv run python -m compileall -q src
uv run --extra dev pytest -q
```

When verify runners or gate semantics change, also run from the repository root:

```bash
node --test gca-mcp/tests/node/**/*.test.mjs
```

Add dependencies with `uv`; do not hand-edit locked versions or remove unrelated
dependencies. Never weaken tests, CI, or validation thresholds merely to get green.
