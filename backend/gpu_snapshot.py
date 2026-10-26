"""gpu_snapshot.py - local GPU telemetry for the Orchestrator control room.

Fixed-argv subprocess calls (nvidia-smi / ollama ps / Win32 for AMD), NO user
input - no injection surface. Cached ~2s. Classifies each card ai / gaming / idle
so the UI can show "GPU busy with a game, X GB free for AI" vs "AI leased".

This is telemetry only; who *holds* a card comes from the optional GPU lease
broker, joined in server.py. Parsing is split from the subprocess calls
(parse_nvidia / parse_nvidia_apps / parse_amd / parse_ollama) so it can be tested
with canned tool output.
"""
from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import config

_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 2.0

_CFG = config.load()
_VRAM_MB = _CFG["vram_table"]
_AI_PROC = tuple(_CFG["ai_processes"])
_GAMING_IGNORE = frozenset(x.lower() for x in _CFG["gaming_ignore"])


_NVIDIA_GPU_CMD = ["nvidia-smi",
                   "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                   "--format=csv,noheader,nounits"]
_NVIDIA_APPS_CMD = ["nvidia-smi", "--query-compute-apps=process_name,used_memory",
                    "--format=csv,noheader,nounits"]
_AMD_CMD = ["powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Where-Object { $_.Name -match 'AMD|Radeon' } | "
            "Select-Object -ExpandProperty Name"]
_OLLAMA_CMD = ["ollama", "ps"]


def _run(args: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def parse_nvidia(out: str) -> list[dict]:
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            gpus.append({"name": p[0], "vendor": "nvidia", "util": int(float(p[1])),
                         "mem_used": int(float(p[2])), "mem_total": int(float(p[3])),
                         "temp": int(float(p[4])), "procs": []})
        except Exception:
            continue
    return gpus


def parse_nvidia_apps(apps: str) -> list[dict]:
    procs = []
    for line in apps.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if p and p[0]:
            mb = 0
            try:
                mb = int(float(p[1])) if len(p) > 1 else 0
            except Exception:
                mb = 0
            procs.append({"name": p[0].split("\\")[-1], "mem_used": mb})
    return procs


def parse_amd(out: str) -> list[dict]:
    gpus = []
    for name in out.strip().splitlines():
        name = name.strip()
        if not name:
            continue
        low = name.lower()
        total = next((v for k, v in _VRAM_MB.items() if k in low), 0)
        gpus.append({"name": name, "vendor": "amd", "util": None, "mem_used": None,
                     "mem_total": total, "temp": None, "procs": [],
                     "note": "live util needs ROCm (n/a on Windows)"})
    return gpus


def parse_ollama(out: str) -> list[dict]:
    rows = []
    for line in out.strip().splitlines()[1:]:
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 4:
            continue
        rows.append({"name": cols[0], "size": cols[2] if len(cols) > 2 else "",
                     "processor": cols[3] if len(cols) > 3 else "",
                     "until": cols[-1] if len(cols) > 4 else ""})
    return rows


def _classify(gpu: dict) -> dict:
    procs = gpu.get("procs") or []
    used = gpu.get("mem_used") or 0
    total = gpu.get("mem_total") or 0
    free = max(0, total - used) if total else None
    ai_procs = [p for p in procs if any(k in p["name"].lower() for k in _AI_PROC)]
    foreign = [p for p in procs if p not in ai_procs
               and p["name"].lower() not in _GAMING_IGNORE]
    busy = ((gpu.get("util") or 0) >= 12 or used >= max(512, int(total * 0.15))) if total else False
    if ai_procs:
        state, holder = "ai", ai_procs[0]["name"]
    elif foreign and busy:
        state, holder = "gaming", foreign[0]["name"]
    else:
        state, holder = "idle", None
    return {"state": state, "foreign_holder": holder, "free_mb": free,
            "ai_procs": ai_procs, "foreign_procs": foreign}


def snapshot() -> dict:
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    # The four probes are independent subprocess calls; run them concurrently.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_gpu = ex.submit(_run, _NVIDIA_GPU_CMD)
        f_apps = ex.submit(_run, _NVIDIA_APPS_CMD)
        f_amd = ex.submit(_run, _AMD_CMD)
        f_oll = ex.submit(_run, _OLLAMA_CMD)
        nvidia = parse_nvidia(f_gpu.result())
        procs = parse_nvidia_apps(f_apps.result())
        amd = parse_amd(f_amd.result())
        ollama = parse_ollama(f_oll.result())
    if nvidia:
        nvidia[0]["procs"] = procs
    gpus = nvidia + amd
    on_nvidia = any("ollama" in p["name"].lower()
                    for g in nvidia for p in (g.get("procs") or []))
    for g in gpus:
        g["classify"] = _classify(g)
        if g["vendor"] == "amd" and ollama and not on_nvidia:
            g["classify"]["state"] = "ai"
            g["classify"]["foreign_holder"] = None
    data = {"gpus": gpus, "ollama": ollama, "ts": now}
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data
