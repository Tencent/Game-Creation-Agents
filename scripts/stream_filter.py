#!/usr/bin/env python3
"""
Stream filter for Claude Code --output-format stream-json.
Reads JSON lines from stdin, prints human-readable summary to terminal.
Full JSON is written to --log-file (if specified).

Usage:
  claude -p "..." --verbose --output-format stream-json | python3 stream_filter.py
  claude -p "..." --verbose --output-format stream-json | python3 stream_filter.py --log-file /tmp/gca-daemon.log
"""
import sys
import json
import argparse
from datetime import datetime


def format_timestamp():
    return datetime.now().strftime("%H:%M:%S")


def truncate(s, max_len=120):
    s = s.replace("\n", " ").strip()
    return s[:max_len] + "..." if len(s) > max_len else s


def process_line(line, log_file=None):
    """Parse one JSON line and print a concise summary."""
    if log_file:
        log_file.write(line + "\n")
        log_file.flush()

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        # Not JSON, print as-is
        print(line, flush=True)
        return

    event_type = event.get("type", "")
    ts = format_timestamp()

    if event_type == "system" and event.get("subtype") == "init":
        model = event.get("model", "unknown")
        print(f"[{ts}] Session started | model={model}", flush=True)

    elif event_type == "assistant":
        content_list = event.get("message", {}).get("content", [])
        for content in content_list:
            ctype = content.get("type", "")

            if ctype == "text":
                text = content.get("text", "")
                if text.strip():
                    print(f"[{ts}] {truncate(text, 200)}", flush=True)

            elif ctype == "tool_use":
                name = content.get("name", "?")
                inp = content.get("input", {})
                # Show key info per tool type
                if name == "Bash":
                    cmd = inp.get("command", "")
                    print(f"[{ts}] > Bash: {truncate(cmd, 150)}", flush=True)
                elif name == "Read":
                    print(f"[{ts}] > Read: {inp.get('file_path', '?')}", flush=True)
                elif name == "Write":
                    print(f"[{ts}] > Write: {inp.get('file_path', '?')}", flush=True)
                elif name == "Edit":
                    print(f"[{ts}] > Edit: {inp.get('file_path', '?')}", flush=True)
                elif name == "Skill":
                    print(f"[{ts}] > Skill: {inp.get('skill', '?')}", flush=True)
                elif name == "Agent":
                    desc = inp.get("description", inp.get("prompt", "")[:80])
                    print(f"[{ts}] > Agent: {truncate(desc, 100)}", flush=True)
                else:
                    print(f"[{ts}] > {name}: {truncate(json.dumps(inp), 100)}", flush=True)

            # Skip thinking blocks silently

    elif event_type == "user":
        # Tool results - show brief status
        msg = event.get("message", {})
        content_list = msg.get("content", [])
        for content in content_list:
            if isinstance(content, dict) and content.get("type") == "tool_result":
                # Only show errors or notable outputs
                tool_content = content.get("content", "")
                if isinstance(tool_content, str) and "error" in tool_content.lower():
                    print(f"[{ts}]   ERROR: {truncate(tool_content, 150)}", flush=True)

    elif event_type == "result":
        # Final result
        cost = event.get("cost_usd", 0)
        duration = event.get("duration_ms", 0)
        turns = event.get("num_turns", 0)
        print(f"\n[{ts}] Done | turns={turns} cost=${cost:.4f} duration={duration/1000:.0f}s", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-file", help="Path to write full JSON log")
    args = parser.parse_args()

    log_file = None
    if args.log_file:
        log_file = open(args.log_file, "a")

    print(f"[{format_timestamp()}] Streaming agent output...\n", flush=True)

    for line in sys.stdin:
        line = line.rstrip("\n")
        if line:
            process_line(line, log_file)

    if log_file:
        log_file.close()


if __name__ == "__main__":
    main()
