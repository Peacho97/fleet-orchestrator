"""Orchestrator - standalone control-room server.

A small FastAPI app that lays OVER the existing fleet without living inside any of
it. It aggregates, on one origin, three sources:

  * GPU/RAM lease broker   - optional external HTTP daemon (leases, queue, VRAM budget)
  * Session broker         - optional external HTTP daemon (agent runs + live tree)
  * Local GPU telemetry    - gpu_snapshot (nvidia-smi / ollama ps), run here

Every outbound call is degrade-safe: if a source is down the UI still renders and
that panel shows a "down" state.

Run:  python server.py        (serves the built frontend + API; see config.example.json)
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import shutil
import time

import httpx
import uvicorn
from fastapi import APIRouter, Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import gpu_snapshot

_C = config.load()
HOST = _C["ORCH_HOST"]
PORT = int(_C["ORCH_PORT"])
GPU_BROKER_URL = _C["GPU_BROKER_URL"]
SESSION_BROKER_URL = _C["SESSION_BROKER_URL"]
OLLAMA_URL = _C["OLLAMA_URL"]
# Archiving a run MOVES its dir out of the broker's runs folder (reversible).
# Disabled unless SESSION_RUNS_DIR is set.
SESSION_RUNS_DIR = pathlib.Path(_C["SESSION_RUNS_DIR"]) if _C["SESSION_RUNS_DIR"] else None
SESSION_ARCHIVE_DIR = (pathlib.Path(_C["SESSION_ARCHIVE_DIR"]) if _C["SESSION_ARCHIVE_DIR"]
                       else SESSION_RUNS_DIR.parent / "_archived_runs" if SESSION_RUNS_DIR else None)
# Lease-broker "lines" queried via GET /status?gpu=<line>.
_GPU_LINES = [x.strip() for x in (_C["GPU_BROKER_LINES"] or "").split(",") if x.strip()]
PRELOAD_TIMEOUT = float(_C["PRELOAD_TIMEOUT_S"])
SUMMARY_TIMEOUT = float(_C["SUMMARY_TIMEOUT_S"])
_LINE_FOR_VENDOR = {"amd": "amd", "nvidia": "cuda"}

app = FastAPI(title="Orchestrator")
api = APIRouter(prefix="/api")


# ── degrade-safe HTTP helpers ─────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, base: str, path: str,
               params: dict | None = None, timeout: float = 2.5):
    try:
        r = await client.get(base + path, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def _post(base: str, path: str, body: dict | None = None, timeout: float = 3.0):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(base + path, json=body or {}, timeout=timeout)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


# ── GPU / RAM: telemetry (local) joined with broker leases ────────────

async def _gpu_lines(client: httpx.AsyncClient):
    results = await asyncio.gather(
        *[_get(client, GPU_BROKER_URL, "/status", {"gpu": g}) for g in _GPU_LINES])
    lines = {g: s for g, s in zip(_GPU_LINES, results) if s is not None}
    return (len(lines) > 0), lines


def _card(g: dict) -> dict:
    total = g.get("mem_total") or 0
    used = g.get("mem_used")
    cl = g.get("classify") or {}
    free = cl.get("free_mb")
    if free is None and total and used is not None:
        free = max(0, total - used)
    return {"name": g.get("name"), "vendor": g.get("vendor"),
            "line": _LINE_FOR_VENDOR.get(g.get("vendor")),
            "total_mb": total, "used_mb": used, "free_mb": free,
            "util": g.get("util"), "temp": g.get("temp"),
            "state": cl.get("state", "idle"), "foreign_holder": cl.get("foreign_holder"),
            "note": g.get("note")}


@api.get("/gpu")
async def gpu():
    try:
        snap = await asyncio.to_thread(gpu_snapshot.snapshot)
    except Exception as e:
        snap = {"gpus": [], "ollama": [], "error": str(e)}
    async with httpx.AsyncClient() as client:
        broker_up, lines = await _gpu_lines(client)
    cards = []
    for g in snap.get("gpus", []):
        c = _card(g)
        ls = lines.get(c["line"] or "")
        if ls:
            holders = ls.get("holders") or ([{"who": ls.get("holder")}] if ls.get("holder") else [])
            c["lease"] = {"holders": holders, "queue": ls.get("queue") or [],
                          "waiting": ls.get("waiting", 0), "presence": ls.get("presence") or []}
        cards.append(c)
    host = lines.get("host-ram")
    host_ram = None
    if host:
        host_ram = {"budget_mb": host.get("budget_mb"), "reserved_mb": host.get("reserved_mb"),
                    "capacity_mb": host.get("capacity_mb"), "used_mb": host.get("used_mb"),
                    "free_mb": host.get("free_mb"), "holders": host.get("holders") or []}
    return {"broker_up": broker_up, "cards": cards, "host_ram": host_ram,
            "ollama": snap.get("ollama", [])}


# ── Preload / unload a local model into GPU VRAM (Ollama keep-alive) ──────────
# An empty prompt with keep_alive loads (or evicts, keep_alive:0) the model
# without generating. Degrade-safe: Ollama down -> ok:false + clear error.

async def _ollama_keepalive(model: str, keep_alive):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                OLLAMA_URL + "/api/generate",
                json={"model": model, "prompt": "", "keep_alive": keep_alive},
                timeout=PRELOAD_TIMEOUT)
            if r.status_code == 200:
                return True, None
            return False, f"ollama HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"ollama unreachable at {OLLAMA_URL}: {e}"


@api.post("/gpu/preload")
async def gpu_preload(model: str = Body(..., embed=True),
                      keep_alive: str = Body("30m", embed=True)):
    if not model:
        return JSONResponse(status_code=400, content={"ok": False, "error": "model required"})
    loaded, err = await _ollama_keepalive(model, keep_alive or "30m")
    return {"ok": loaded, "model": model, "loaded": loaded, "error": err}


@api.post("/gpu/unload")
async def gpu_unload(model: str = Body(..., embed=True)):
    if not model:
        return JSONResponse(status_code=400, content={"ok": False, "error": "model required"})
    loaded, err = await _ollama_keepalive(model, 0)
    return {"ok": err is None, "model": model, "loaded": False, "error": err}


# ── Session broker ────────────────────────────────────────────────────

def _unwrap_runs(runs) -> list:
    """The broker may return a bare list or {"runs": [...]} / {"items": [...]}."""
    if isinstance(runs, dict):
        runs = runs.get("runs") or runs.get("items") or []
    return runs if isinstance(runs, list) else []


@api.get("/sessions")
async def sessions():
    async with httpx.AsyncClient() as client:
        runs, budget, pause = await asyncio.gather(
            _get(client, SESSION_BROKER_URL, "/api/runs"),
            _get(client, SESSION_BROKER_URL, "/api/budget"),
            _get(client, SESSION_BROKER_URL, "/api/pause"))
    up = runs is not None
    runs = _unwrap_runs(runs)
    return {"up": up, "runs": runs, "budget": budget or None,
            "paused": bool((pause or {}).get("paused")) if isinstance(pause, dict) else None}


@api.get("/sessions/{run_id}/tree")
async def session_tree(run_id: str):
    async with httpx.AsyncClient() as client:
        tree = await _get(client, SESSION_BROKER_URL, f"/api/runs/{run_id}/tree")
    return {"up": tree is not None, "tree": tree}


@api.post("/sessions/pause")
async def session_pause(paused: bool = Body(True, embed=True)):
    res = await _post(SESSION_BROKER_URL, "/api/pause", {"paused": paused})
    if res is None:
        return JSONResponse(status_code=502, content={"ok": False, "error": "session broker unreachable"})
    return {"ok": True, "result": res}


# ── Sessions tab: launch runs + drive gates (all proxy the session broker) ─────────────────
# Static path declared before the /{run_id}/* routes so "tasks" isn't captured.

@api.get("/sessions/tasks")
async def sessions_tasks():
    """Available task templates the broker can launch (from its TASKS dir)."""
    async with httpx.AsyncClient() as client:
        t = await _get(client, SESSION_BROKER_URL, "/api/tasks")
    return {"up": t is not None, "tasks": t or []}


@api.post("/sessions/launch")
async def sessions_launch(body: dict = Body(...)):
    """Kick off a new run. Body is forwarded verbatim to the session broker /api/launch -
    either {"task_file": "x.json"} or {"task_spec": {"goal", "repo_path",
    "target_file"}}. Broker validates (needs a goal); we surface its status+body."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(SESSION_BROKER_URL + "/api/launch", json=body, timeout=15.0)
        ctype = r.headers.get("content-type", "")
        payload = r.json() if ctype.startswith("application/json") else {"error": r.text[:300]}
        return JSONResponse(status_code=r.status_code, content=payload)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"session broker unreachable: {e}"})


@api.get("/sessions/{run_id}/detail")
async def session_detail(run_id: str):
    async with httpx.AsyncClient() as client:
        d = await _get(client, SESSION_BROKER_URL, f"/api/runs/{run_id}")
    return {"up": d is not None, "run": d}


@api.get("/sessions/{run_id}/transcript")
async def session_transcript(run_id: str, path: str):
    """A node's actual transcript/output - the content behind 'proposed change'.
    The broker returns PlainText; we wrap it as JSON for a uniform frontend."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{SESSION_BROKER_URL}/api/runs/{run_id}/transcript",
                                  params={"path": path}, timeout=6.0)
        if r.status_code == 200:
            return {"up": True, "text": r.text}
        return {"up": True, "text": "", "error": f"broker HTTP {r.status_code}"}
    except Exception as e:
        return {"up": False, "text": "", "error": str(e)}


# ── Enriched run list (adds target/tier/spend/recommendation for the row meta) ──
_rich_cache = {"ts": 0.0, "data": None}


@api.get("/sessions/rich")
async def sessions_rich():
    """Per-run summary fields for the Workflows list - pulled from each run's tree
    summary (target file, worker tier, ~spend, review recommendation, tests).
    Cached ~15s so polling stays cheap."""
    now = time.time()
    if _rich_cache["data"] is not None and now - _rich_cache["ts"] < 15:
        return _rich_cache["data"]
    async with httpx.AsyncClient() as client:
        raw = await _get(client, SESSION_BROKER_URL, "/api/runs")
        if raw is None:
            return {"up": False, "runs": []}   # failures are never cached
        runs = _unwrap_runs(raw)

        async def one(r):
            rid = r.get("id") or r.get("run_id")
            t = await _get(client, SESSION_BROKER_URL, f"/api/runs/{rid}/tree")
            sm = (t or {}).get("summary") or {}
            return {"id": rid, "status": r.get("status"),
                    "goal": r.get("goal") or r.get("slug"),
                    "target": sm.get("target"), "tier": sm.get("worker_tier"),
                    "spend": sm.get("spend"), "recommendation": sm.get("recommendation"),
                    "tests": sm.get("tests"), "branch": sm.get("branch")}

        rows = await asyncio.gather(*[one(r) for r in runs]) if runs else []
    data = {"up": True, "runs": list(rows)}
    _rich_cache["ts"] = now
    _rich_cache["data"] = data
    return data


@api.post("/sessions/{run_id}/archive")
async def session_archive(run_id: str):
    """Move a run OUT of the broker's runs folder (reversible) so it leaves the
    list - used to clear test runs. Guards against path traversal."""
    if not re.fullmatch(r"[\w.\-]+", run_id or ""):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad run id"})
    if SESSION_RUNS_DIR is None:
        return JSONResponse(status_code=501, content={"ok": False, "error": "SESSION_RUNS_DIR not configured"})
    src = SESSION_RUNS_DIR / run_id
    if src.resolve().parent != SESSION_RUNS_DIR.resolve() or not src.is_dir():
        return JSONResponse(status_code=404, content={"ok": False, "error": "no such run dir"})
    try:
        SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dst = SESSION_ARCHIVE_DIR / run_id
        await asyncio.to_thread(shutil.move, str(src), str(dst))
        _rich_cache["data"] = None  # bust cache so the list updates
        return {"ok": True, "archived": run_id, "to": str(dst)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Per-node "what did this agent do" summaries (local model, cached) ─────────
# Each workflow node has a transcript but no plain-language summary. We read each
# transcript and have a LOCAL model boil it to ~2 sentences, so the workflow tree
# reads as "who did what" without opening every transcript. Finished-run
# transcripts are immutable -> cache per run_id, summarize once.

SUMMARY_MODEL = _C["SUMMARY_MODEL"]
_summary_cache: dict[str, dict] = {}


def _flatten_nodes(tree: dict) -> list:
    out = []
    if tree.get("conductor"):
        out.append(tree["conductor"])
    for team in tree.get("teams", []):
        if team.get("lead"):
            out.append(team["lead"])
        out.extend(team.get("workers", []) or [])
    for dep in tree.get("lateral", []):
        out.append(dep)
        out.extend(dep.get("members") or dep.get("workers") or dep.get("reviewers") or [])
    return [n for n in out if n]


def _role_fallback(name: str, doing: str) -> str:
    if (name or "").lower().startswith("conductor"):
        return (f"Planned the goal, split it into worker tasks and delegated to the team. "
                f"Final outcome: {doing or 'done'}.")
    return f"Recorded status '{doing or 'done'}'; no transcript was captured for this step."


async def _summarize(client: httpx.AsyncClient, name: str, doing: str, text: str) -> str:
    text = (text or "")[:3200]
    prompt = (
        f"An AI agent called '{name}' just finished a step in a coding workflow "
        f"(its status label: {doing}). Its full transcript is below. In EXACTLY two "
        f"plain-English sentences, say what this agent did and what it produced or decided. "
        f"Be concrete but concise. Do NOT paste file contents, code, or markdown from the "
        f"transcript - describe them. No preamble, no bullet points, no headings.\n\n"
        f"TRANSCRIPT:\n{text}")
    try:
        r = await client.post(
            OLLAMA_URL + "/api/generate",
            json={"model": SUMMARY_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.2, "num_predict": 90}},
            timeout=SUMMARY_TIMEOUT)
        if r.status_code == 200:
            return (r.json().get("response") or "").strip()
    except Exception:
        pass
    return ""


@api.get("/sessions/{run_id}/summaries")
async def session_summaries(run_id: str):
    """Two-sentence 'what it did' per workflow node. Cached per run (immutable)."""
    if run_id in _summary_cache:
        return {"up": True, "cached": True, "model": SUMMARY_MODEL,
                "summaries": _summary_cache[run_id]}
    async with httpx.AsyncClient() as client:
        tree = await _get(client, SESSION_BROKER_URL, f"/api/runs/{run_id}/tree", timeout=5.0)
    if not tree:
        return {"up": False, "summaries": {}}
    nodes = _flatten_nodes(tree)

    async def one(n):
        nid, tpath = n.get("id"), n.get("transcript")
        doing = n.get("doing") or n.get("status")
        if not tpath:
            return nid, _role_fallback(n.get("name"), doing)
        # broker returns the transcript as PLAIN TEXT (not JSON) - read r.text
        text = ""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{SESSION_BROKER_URL}/api/runs/{run_id}/transcript",
                                     params={"path": tpath}, timeout=6.0)
                if r.status_code == 200:
                    text = r.text
        except Exception:
            pass
        async with httpx.AsyncClient() as client:
            s = await _summarize(client, n.get("name"), doing, text) if text.strip() else ""
        return nid, s or _role_fallback(n.get("name"), doing)

    results = await asyncio.gather(*[one(n) for n in nodes])
    summaries = {nid: s for nid, s in results if s}
    if summaries:
        _summary_cache[run_id] = summaries
    return {"up": True, "model": SUMMARY_MODEL, "summaries": summaries}


@api.get("/sessions/{run_id}/gate")
async def get_gate(run_id: str):
    async with httpx.AsyncClient() as client:
        g = await _get(client, SESSION_BROKER_URL, f"/api/runs/{run_id}/gate")
    return {"up": g is not None, "gate": g or {"pending": None}}


@api.post("/sessions/{run_id}/gate")
async def post_gate(run_id: str, body: dict = Body(...)):
    """Approve/reject a pending gate. Body: {gate: plan|rnd|design, approved: bool, notes?}."""
    res = await _post(SESSION_BROKER_URL, f"/api/runs/{run_id}/gate", body)
    if res is None:
        return JSONResponse(status_code=502, content={"ok": False, "error": "session broker unreachable"})
    return {"ok": True, "result": res}


@api.get("/overview")
async def overview():
    g, s = await asyncio.gather(gpu(), sessions())
    return {"gpu": g, "sessions": s,
            "config": {"gpu_broker_url": GPU_BROKER_URL, "session_broker_url": SESSION_BROKER_URL}}


@api.get("/sources")
async def sources():
    """Health of the optional upstreams - powers the daemon dots in the header."""
    async with httpx.AsyncClient() as client:
        gbroker, sbroker = await asyncio.gather(
            _get(client, GPU_BROKER_URL, "/health", None, 1.5),
            _get(client, SESSION_BROKER_URL, "/api/runs", None, 2.0))
    return {"gpu_broker": gbroker is not None, "session_broker": sbroker is not None}


app.include_router(api)

# Serve the built frontend last (so /api/* wins). Missing dist → API-only mode.
_DIST = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")


if __name__ == "__main__":
    print(f"Orchestrator control room -> http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
