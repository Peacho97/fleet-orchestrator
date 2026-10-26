"""Settings: defaults, overridden by an optional JSON file, overridden by env vars.

The file is ORCH_CONFIG (default ./config.json next to this module). Keys match
config.example.json. String keys can also be set by the env var of the same name
(an empty env value clears the setting). The structured keys (vram_table,
ai_processes, gaming_ignore) are file-only.
"""
import copy
import json
import os
import pathlib

DEFAULTS = {
    "ORCH_HOST": "127.0.0.1",
    "ORCH_PORT": "8130",
    "GPU_BROKER_URL": "http://127.0.0.1:8106",
    "SESSION_BROKER_URL": "http://127.0.0.1:8077",
    "OLLAMA_URL": "http://127.0.0.1:11434",
    "SUMMARY_MODEL": "llama3.2:3b",
    "GPU_BROKER_LINES": "amd,cuda,host-ram",
    "SESSION_RUNS_DIR": "",
    "SESSION_ARCHIVE_DIR": "",
    "PRELOAD_TIMEOUT_S": "30",
    "SUMMARY_TIMEOUT_S": "20",
    # substring of lower-cased GPU name -> VRAM in MB (AMD cards; WMI cannot report it)
    "vram_table": {"9060 xt": 16384, "9070": 16384, "2080 super": 8192, "2080": 8192},
    # process-name substrings that mark a GPU as held by AI work
    "ai_processes": ["ollama", "python", "comfyui", "stable", "llama", "koboldcpp", "worker"],
    # process names never counted as a foreign (gaming) holder
    "gaming_ignore": ["dwm.exe", "explorer.exe", "csrss.exe", "searchhost.exe",
                      "shellexperiencehost.exe", "startmenuexperiencehost.exe"],
}
_STR_KEYS = [k for k, v in DEFAULTS.items() if isinstance(v, str)]


def load(env=None, path=None) -> dict:
    env = os.environ if env is None else env
    cfg = copy.deepcopy(DEFAULTS)
    p = pathlib.Path(path or env.get("ORCH_CONFIG") or pathlib.Path(__file__).parent / "config.json")
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"config file {p} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"config file {p} must contain a JSON object")
        for k, v in data.items():
            if k in DEFAULTS:
                cfg[k] = v if (v is None or not isinstance(DEFAULTS[k], str)) else str(v)
    for k in _STR_KEYS:
        if k in env:
            cfg[k] = env[k]
    for k in ("PRELOAD_TIMEOUT_S", "SUMMARY_TIMEOUT_S"):
        try:
            float(cfg[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number, got {cfg[k]!r}") from None
    try:
        int(cfg["ORCH_PORT"])
    except (TypeError, ValueError):
        raise ValueError(f"ORCH_PORT must be an integer, got {cfg['ORCH_PORT']!r}") from None
    return cfg
