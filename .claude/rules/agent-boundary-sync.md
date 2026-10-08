---
paths:
  - ".claude/agents/**/*.md"
  - ".claude/skills/game-pipeline/SKILL.md"
  - ".claude/skills/game-pipeline/dispatch-contract.md"
  - ".claude/loop.md"
  - ".claude/settings.json"
  - ".claude/hooks/session/guard-agent-boundary.py"
  - ".claude/hooks/session/bind-pipeline-session.py"
  - ".claude/hooks/session/pipeline_session.py"
---

# Pipeline Agent Boundary Sync Rules

This Rule provides non-blocking synchronization guidance only. It does not add
runtime permissions, hooks, or gate decisions.

Apply these checks only when changing a pipeline agent's registration, name, role,
tools, dispatch contract, or owned artifacts.

- An agent specification declares intended responsibilities and available tools. Its
  effective authority is the intersection of that tool list, session binding, hook
  policy, MCP availability, active workspace, and artifact ownership.
- Adding, renaming, or removing a pipeline agent requires synchronizing the agent
  file, every dispatch caller, the dispatch contract, `PIPELINE_AGENTS`, the
  `SubagentStart` matcher, boundary logic, and focused tests.
- Changing an agent's writable artifacts requires updating its documented ownership
  and `guard-agent-boundary.py`. Check single-writer, concurrency, and active-verify
  constraints before expanding authority.
- Changing an agent's access to an existing MCP tool requires updating its tool list
  and affected dispatch or caller contracts. The existing ToolDef and handler remain
  unchanged unless the public MCP API also changes.
- Adding, renaming, or removing a public MCP tool requires synchronizing the canonical
  ToolDef and handler, agent tool lists and callers, documentation, and focused tests.
- Rules and prose never grant tools, filesystem authority, or permission to bypass
  hooks. Enforcement remains in tool allowlists, settings, hooks, MCP handlers, and
  workspace isolation.
- After changing a canonical agent name, tool, or ownership term, search the
  repository for stale `subagent_type`, matcher, registry, dispatch, and path-policy
  references.
