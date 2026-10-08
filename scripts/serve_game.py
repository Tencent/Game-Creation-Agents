#!/usr/bin/env python3
"""serve_game.py — safe, collision-free static server for a game under playground/.

The pipeline serves each game's `index.html` over HTTP (Phaser + ES modules can't
run from file://). Historically the agent started `http-server`/`python -m http.server`
ad-hoc on a hardcoded port (3000 / 8080). Two failure modes followed:

  1. A STALE server from a PREVIOUS run (or another game) still owns the port, so
     verify connects to the WRONG game and every screenshot / gate is measured
     against stale code — a silent, expensive false result.
  2. Two concurrent game sessions fight over the same port.

This launcher removes both:

  * `--stop`      : kill THIS game's recorded server (from .playtest/server.json) and exit.
  * start (default):
      1. kill this game's OWN previously-recorded server (insurance — no orphans),
      2. pick a FREE port (scan upward from --base) so concurrent games never collide
         — this is the "detect a new port" safety,
      3. start `python -m http.server` bound to 127.0.0.1 in the game dir (detached),
      4. IDENTITY-VERIFY the port actually serves THIS game (fetch /index.html and
         byte-compare to <game_dir>/index.html) — never hand a stale server to verify,
      5. record {pid, port, url} to <game_dir>/.playtest/server.json,
      6. print `URL=http://127.0.0.1:<port>` to stdout for the orchestrator to thread
         into every verify-agent dispatch.

Only kills a process it is confident is a stale dev server (its recorded PID whose
live command line is a `http.server` / `http-server`), so it never touches another
game's session or an unrelated process.

Stdlib only. Usage:
  python3 scripts/serve_game.py <game_dir> [--base 3000] [--port N] [--host 127.0.0.1]
  python3 scripts/serve_game.py <game_dir> --stop
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def _server_json(game_dir: Path) -> Path:
    return game_dir / ".playtest" / "server.json"


def _read_record(game_dir: Path) -> dict:
    p = _server_json(game_dir)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _proc_cmdline(pid: int) -> str:
    """Best-effort live command line for a pid (empty string if gone)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _looks_like_dev_server(cmdline: str) -> bool:
    c = cmdline.lower()
    return ("http.server" in c) or ("http-server" in c) or ("serve_game.py" in c)


def kill_prior(game_dir: Path, *, verbose: bool = True) -> bool:
    """Kill this game's previously-recorded server iff it's still a dev server.

    Scoped to the PID recorded in server.json for THIS game — never kills another
    game's session. Returns True if something was killed.
    """
    rec = _read_record(game_dir)
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return False
    cmd = _proc_cmdline(pid)
    if not cmd:
        return False  # already gone
    if not _looks_like_dev_server(cmd):
        # PID was recycled to some other program — do NOT kill it.
        if verbose:
            print(f"serve_game: recorded pid {pid} is no longer a dev server; leaving it.", file=sys.stderr)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):  # up to ~2s for graceful exit
            time.sleep(0.1)
            if not _proc_cmdline(pid):
                break
        else:
            os.kill(pid, signal.SIGKILL)
        if verbose:
            print(f"serve_game: stopped prior server (pid {pid}, port {rec.get('port')}).")
        return True
    except ProcessLookupError:
        return False
    except OSError as e:
        if verbose:
            print(f"serve_game: could not kill pid {pid}: {e}", file=sys.stderr)
        return False


def _listening(host: str, port: int) -> bool:
    """True if anything accepts a TCP connection at host:port (across every family
    the name resolves to). A connect-probe (not a bind-test) so it detects a listener
    regardless of address family — an IPv4 bind-test alone MISSES an IPv6 `[::]:port`
    squatter, letting two servers coexist on one port (the browser's localhost then
    hits the wrong one). See the IPv6 stale-server bug."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for af, socktype, proto, _canon, sa in infos:
        try:
            with socket.socket(af, socktype, proto) as s:
                s.settimeout(0.3)
                if s.connect_ex(sa) == 0:
                    return True
        except OSError:
            continue
    return False


def _port_free(host: str, port: int) -> bool:
    """Free only if NOTHING is listening on the port via 127.0.0.1, ::1, OR localhost.
    Checking all three (not just IPv4) prevents coexisting with a stale server bound to
    the other address family — otherwise `localhost:<port>` in a browser can resolve to
    the squatter instead of us."""
    for h in ("127.0.0.1", "::1", "localhost"):
        if _listening(h, port):
            return False
    return True


def pick_port(host: str, base: int, preferred: int | None) -> int:
    if preferred is not None and _port_free(host, preferred):
        return preferred
    for port in range(base, base + 200):
        if _port_free(host, port):
            return port
    raise SystemExit(f"serve_game: no free port in [{base}, {base + 200}).")


def _fetch(url: str, timeout: float = 3.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (localhost only)
            return r.read()
    except Exception:  # noqa: BLE001 — any failure = not-yet-up / not reachable
        return None


def start(game_dir: Path, *, host: str, base: int, preferred: int | None) -> dict:
    index = game_dir / "index.html"
    if not index.is_file():
        raise SystemExit(f"serve_game: {index} not found — nothing to serve.")

    kill_prior(game_dir)
    port = pick_port(host, base, preferred)
    url = f"http://{host}:{port}"

    playtest = game_dir / ".playtest"
    playtest.mkdir(parents=True, exist_ok=True)
    log_path = playtest / "server.log"

    # Detached background server rooted at the game dir. start_new_session so it
    # survives this launcher process exiting.
    log = open(log_path, "ab")  # noqa: SIM115 — handed to the child, closed on exit
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", host],
        cwd=str(game_dir),
        stdout=log,
        stderr=log,
        start_new_session=True,
    )

    # Wait until it serves, then IDENTITY-VERIFY it's THIS game (not a stale server
    # that happened to grab the port in the race).
    own_index = index.read_bytes()
    served = None
    for _ in range(50):  # up to ~5s
        time.sleep(0.1)
        if proc.poll() is not None:
            raise SystemExit(
                f"serve_game: http.server exited early (code {proc.returncode}); see {log_path}."
            )
        served = _fetch(f"{url}/index.html")
        if served is not None:
            break
    if served is None:
        proc.terminate()
        raise SystemExit(f"serve_game: server on {url} never responded; see {log_path}.")
    if served != own_index:
        # Some OTHER server owns this port — do not hand a stale/wrong game to verify.
        proc.terminate()
        raise SystemExit(
            f"serve_game: {url} is serving a DIFFERENT index.html than {index} — "
            f"a stale server owns this port. Aborting so verify never runs against the "
            f"wrong game. Free the port or re-run (a new port will be chosen)."
        )
    # Also check the name a BROWSER uses (localhost) — it may resolve to a different
    # address family (IPv6 ::1) where a stale server squats on the same port. If that
    # resolves to a DIFFERENT game, abort so the human sees the right game too.
    browser_served = _fetch(f"http://localhost:{port}/index.html")
    if browser_served is not None and browser_served != own_index:
        proc.terminate()
        raise SystemExit(
            f"serve_game: localhost:{port} serves a DIFFERENT game than {index} — a stale "
            f"server occupies this port on another address family (e.g. IPv6 [::1]). "
            f"Kill it (`ps aux | grep http.server`) then re-run, or re-run for a new port."
        )

    record = {
        "pid": proc.pid,
        "port": port,
        "url": url,
        "host": host,
        "game_dir": str(game_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "log": str(log_path),
    }
    _server_json(game_dir).write_text(json.dumps(record, indent=2))
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe static server for a playground game.")
    ap.add_argument("game_dir", help="Path to the game directory (contains index.html).")
    ap.add_argument("--base", type=int, default=3000, help="Lowest port to scan from (default 3000).")
    ap.add_argument("--port", type=int, default=None, help="Preferred port; falls back to a free scan if taken.")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    ap.add_argument("--stop", action="store_true", help="Stop this game's recorded server and exit.")
    args = ap.parse_args()

    game_dir = Path(args.game_dir).expanduser().resolve()
    if not game_dir.is_dir():
        print(f"serve_game: {game_dir} is not a directory.", file=sys.stderr)
        return 2

    if args.stop:
        killed = kill_prior(game_dir)
        # Clear the record so a later start doesn't try to re-kill a dead pid.
        rec_path = _server_json(game_dir)
        if rec_path.is_file():
            try:
                rec_path.unlink()
            except OSError:
                pass
        print("serve_game: stopped." if killed else "serve_game: nothing running to stop.")
        return 0

    record = start(game_dir, host=args.host, base=args.base, preferred=args.port)
    # Machine-parseable line for the orchestrator, plus a human summary.
    print(f"URL={record['url']}")
    print(
        f"serve_game: serving {game_dir.name} at {record['url']} "
        f"(pid {record['pid']}, port {record['port']}) — verified it serves this game."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
