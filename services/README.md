# RMBG-2.0 Background-Removal Service

Multi-GPU, single-port (`:8080`) FastAPI service. One model replica per GPU; each
request is auto-dispatched to the least-busy GPU. The GPU set auto-adapts to the
host — by default every visible CUDA device is used (scales from 1 to N cards);
set `RMBG_GPU_IDS` to pin a subset. Serves the `mcp__gca__rmbg2_*` tools (default
client endpoint: `http://localhost:8080/v1/images/remove-background`).

## Python env

Run with your project's Python environment. Point the launcher at it via the
`RMBG_PYTHON` env var (defaults to `python` on `PATH`):

```
export RMBG_PYTHON=/path/to/your/env/bin/python
```

## Scripts

| Script | What it does |
|---|---|
| `start_rmbg2.sh` | Idempotent launcher. No-op if already healthy. `--force` to restart. Detaches via `setsid`; waits up to 120s for a multi-GPU cold start. |
| `stop_rmbg2.sh` | Stops the watchdog **and** the server. |
| `watchdog_rmbg2.sh` | Keeps the service alive (for nodes without systemd). Polls `/health` every 30s, restarts via `start_rmbg2.sh` if down. Single-instance via `flock`. |

## Bring the service up (and keep it up)

```bash
cd services   # this directory

# 0. prerequisites — point at your Python env and (local) model dir.
#    RMBG_PYTHON must have torch / transformers / fastapi / uvicorn installed.
#    RMBG_MODEL_PATH defaults to the HF hub id (needs network); set it to a
#    local model dir to run offline.
export RMBG_PYTHON=/path/to/your/env/bin/python
export RMBG_MODEL_PATH=/path/to/local/RMBG-2.0   # optional; omit to pull from HF hub

# 1. start the server (idempotent)
bash start_rmbg2.sh

# 2. start the watchdog, detached so it survives your shell
setsid bash watchdog_rmbg2.sh >> /tmp/rmbg2_logs/watchdog.log 2>&1 < /dev/null &
```

Running just the watchdog is enough — it will start the server itself if it's
down. After a **node restart**, re-run both lines above (this node has no init
system to auto-launch them).

## Stop

```bash
bash stop_rmbg2.sh
```

## Health / logs

```bash
curl -s http://localhost:8080/health        # {"status":"ok","gpus":<N>,...}
tail -f /tmp/rmbg2_logs/server.log           # server log
tail -f /tmp/rmbg2_logs/watchdog.log         # watchdog log
```

## Env overrides

| Var | Default | Meaning |
|---|---|---|
| `RMBG_GPU_IDS` | all visible | e.g. `"0,1,2,3"` to limit GPUs |
| `RMBG_SLOTS_PER_GPU` | `4` | concurrent inference slots per GPU |
| `RMBG_PORT` | `8080` | listen port |
| `RMBG_MODEL_PATH` | `briaai/RMBG-2.0` | HF hub id or local model dir |
| `RMBG_WATCHDOG_INTERVAL` | `30` | watchdog poll seconds |

## Matting pipeline (color-aug branch, when `bg_hex` is supplied)

1. RMBG-2.0 soft-alpha segmentation
2. `rmbg_fg_cutoff` — harden low-confidence edge alpha
3. color-aug rescue — recover wispy tendrils/particles RMBG would clip
4. **decontaminate** (unmultiply) — solve `F=(C-(1-a)*bg)/a` on semi-transparent edges
5. **despill** — clamp the residual bg-hue fringe unmultiply can't remove

`decontaminate` and `despill` auto-enable whenever `bg_hex` is given;
`despill_limit` (default 20 = gentle) tunes despill strength (0 = fully neutral).
