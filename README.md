# fleet-orchestrator

A single-page control room for a local AI fleet. A small FastAPI backend aggregates
three sources and a Svelte 5 frontend renders them on one page.

| Source | Provided by | Shown as |
|--------|-------------|----------|
| Local GPU telemetry | `backend/gpu_snapshot.py` (`nvidia-smi`, `ollama ps`, a PowerShell query for AMD) | per-card utilization, temperature, free VRAM, ai / gaming / idle state, loaded Ollama models |
| GPU / RAM leases | optional external "GPU lease broker" over HTTP | who holds each card, queue, VRAM budget |
| Agent runs | optional external "session broker" over HTTP | run list, conductor/team/worker tree, transcripts, plan gates, launch, pause, archive |

The lease broker and session broker are separate private daemons that are not part of this repository. Their APIs are documented here only as far as this UI consumes them (see Architecture).

Every upstream is optional. When one is unreachable its panel shows a "down" state and the rest keeps working.

Not included: the lease broker and session broker themselves, API-key management, and
cost/usage tracking. The backend contains no code that reads, stores or sends API keys.
Spend figures that appear in the UI are the `spend` fields a session broker reports per run.

## Architecture

```
browser  ->  backend/server.py (FastAPI, one origin, serves frontend/dist)
                |-- gpu_snapshot.snapshot()          local subprocesses, cached 2 s
                |-- GPU_BROKER_URL     /status?gpu=<line>, /health
                |-- SESSION_BROKER_URL /api/runs, /api/runs/<id>, /tree, /transcript, /gate,
                |                      /api/tasks, /api/launch, /api/budget, /api/pause
                |-- OLLAMA_URL         /api/generate  (model preload/unload, per-node summaries)
```

Backend routes (all under `/api`): `overview`, `sources`, `gpu`, `gpu/preload`, `gpu/unload`,
`sessions`, `sessions/rich`, `sessions/tasks`, `sessions/launch`, `sessions/pause`,
`sessions/{id}/tree|detail|transcript|summaries|gate|archive`. `overview` also returns the configured broker URLs, which the UI shows in its offline messages.

The session and lease broker contracts are whatever the endpoints above return; the backend
only proxies them, so a compatible service must implement those paths.

## gpu_snapshot

`snapshot()` runs, with fixed argv and no user input:

- `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits`
- `nvidia-smi --query-compute-apps=process_name,used_memory --format=csv,noheader,nounits` (processes are attached to the first NVIDIA card only)
- `powershell Get-CimInstance Win32_VideoController` filtered to `AMD|Radeon` names
- `ollama ps`

The four probes run concurrently. Each has a 3 s timeout and returns empty output on any error, so missing tools yield empty lists.
The parsers are pure functions: `parse_nvidia`, `parse_nvidia_apps`, `parse_amd`, `parse_ollama`.
The result is cached for 2 s.

Output shape:

```json
{
  "gpus": [
    {
      "name": "NVIDIA GeForce RTX 2080 SUPER", "vendor": "nvidia",
      "util": 37, "mem_used": 6100, "mem_total": 8192, "temp": 61,
      "procs": [{"name": "game.exe", "mem_used": 5800}],
      "classify": {
        "state": "ai | gaming | idle", "foreign_holder": "game.exe or null",
        "free_mb": 2092, "ai_procs": [], "foreign_procs": []
      }
    }
  ],
  "ollama": [{"name": "llama3.2:3b", "size": "2.8 GB", "processor": "100% GPU", "until": "4 minutes from now"}],
  "ts": 1760000000.0
}
```

AMD entries have `vendor: "amd"`, `util`, `mem_used` and `temp` set to `null`, a `note` field, and
`mem_total` taken from `vram_table` (`0` if the name is not listed).

Classification: a card with a process whose name contains one of the `ai_processes` entries
(default `ollama, python, comfyui, stable, llama, koboldcpp, worker`) is `ai`. Otherwise, if processes outside `gaming_ignore` exist and the card is busy
(util >= 12% or used >= max(512 MB, 15% of total)) it is `gaming` with the first such process as
`foreign_holder`. Otherwise `idle`. An AMD card is marked `ai` when `ollama ps` lists a model and
no NVIDIA process named ollama exists.

## Setup

Requires Python 3.10+ and Node 18+.

```
pip install -r requirements.txt
cd frontend && npm install && npm run build     # again after any UI change
cd ../backend && python server.py               # http://127.0.0.1:8130
```

`run.bat` starts the backend on Windows. Without a built `frontend/dist` the backend runs API-only.
Frontend development: `npm run dev` in `frontend/` (port 5174, proxies `/api` to 8130).

## Configuration

Defaults, then an optional JSON file (`backend/config.json`, or the path in `ORCH_CONFIG`),
then environment variables of the same names (an empty env value clears a setting). See `backend/config.example.json`. Invalid JSON or a non-integer `ORCH_PORT` stops startup with an error message.

| Key | Default | Meaning |
|-----|---------|---------|
| `ORCH_HOST` / `ORCH_PORT` | `127.0.0.1` / `8130` | listen address |
| `GPU_BROKER_URL` | `http://127.0.0.1:8106` | lease broker base URL |
| `SESSION_BROKER_URL` | `http://127.0.0.1:8077` | session broker base URL |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `SUMMARY_MODEL` | `llama3.2:3b` | local model that writes the two-sentence node summaries |
| `GPU_BROKER_LINES` | `amd,cuda,host-ram` | comma-separated line names queried on the lease broker |
| `SESSION_RUNS_DIR` | empty | runs directory of the session broker; archiving is disabled when empty |
| `SESSION_ARCHIVE_DIR` | `<SESSION_RUNS_DIR>/../_archived_runs` | destination for archived runs |
| `PRELOAD_TIMEOUT_S` | `30` | timeout for an Ollama model preload/unload request |
| `SUMMARY_TIMEOUT_S` | `20` | timeout per node for the local summary model |

File-only keys (not settable by env), used by `gpu_snapshot.py`:

| Key | Meaning |
|-----|---------|
| `vram_table` | lower-case GPU-name substring to VRAM in MB; used for AMD cards, whose VRAM WMI cannot report |
| `ai_processes` | process-name substrings that mark a card as held by AI work |
| `gaming_ignore` | process names never counted as a foreign (gaming) holder, e.g. `dwm.exe`, `explorer.exe` |

For the frontend dev server, `VITE_API_PORT` sets the backend port to proxy to (default 8130).

There is no authentication. The server binds to localhost by default and its POST routes can
load models, launch runs and move run directories, so do not expose it to untrusted networks.

## Limitations

- GPU telemetry for AMD cards is Windows-only (PowerShell + WMI) and reports only the name and a table-based VRAM size; live utilization is not available.
- NVIDIA process names are split on `\`, which assumes Windows paths from `nvidia-smi`.
- The VRAM table, AI process list and ignore list are config, but the defaults are a short list of consumer cards and common process names. The model preload buttons are hard-coded in `frontend/src/App.svelte` (`PRELOAD`).
- Sessions and lease panels do nothing without compatible brokers.
- No `.env`/secret handling is needed or provided; no credentials are read.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

`tests/test_gpu_snapshot.py` feeds canned `nvidia-smi` and `ollama ps` output to the parsers and checks the snapshot shape and classification. `tests/test_config.py` checks default, file and environment precedence and error messages. `tests/test_server.py` uses FastAPI's TestClient with a patched HTTP client to check broker-down handling, the run list payload shapes, archive configuration and the overview shape.

## License

MIT, see `LICENSE`.
