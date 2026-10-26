import gpu_snapshot as g

NVSMI = "NVIDIA GeForce RTX 2080 SUPER, 37, 6100, 8192, 61\n"
BS = chr(92)
NL = chr(10)
NVSMI_APPS = ("C:" + BS + "Windows" + BS + "System32" + BS + "dwm.exe, 300" + NL
              + "C:" + BS + "Games" + BS + "game.exe, 5800" + NL)
OLLAMA_PS = (
    "NAME           ID            SIZE     PROCESSOR    UNTIL\n"
    "llama3.2:3b    a80c4f17acd5  2.8 GB   100% GPU     4 minutes from now\n"
)


def test_parse_nvidia():
    assert g.parse_nvidia(NVSMI) == [{
        "name": "NVIDIA GeForce RTX 2080 SUPER", "vendor": "nvidia", "util": 37,
        "mem_used": 6100, "mem_total": 8192, "temp": 61, "procs": []}]
    assert g.parse_nvidia("") == [] and g.parse_nvidia("garbage, x") == []


def test_parse_nvidia_apps():
    assert g.parse_nvidia_apps(NVSMI_APPS) == [
        {"name": "dwm.exe", "mem_used": 300}, {"name": "game.exe", "mem_used": 5800}]


def test_parse_amd():
    (card,) = g.parse_amd("AMD Radeon RX 9060 XT\n")
    assert card["vendor"] == "amd" and card["mem_total"] == 16384
    assert card["util"] is None and card["mem_used"] is None and "note" in card


def test_parse_ollama():
    assert g.parse_ollama(OLLAMA_PS) == [{
        "name": "llama3.2:3b", "size": "2.8 GB", "processor": "100% GPU",
        "until": "4 minutes from now"}]
    assert g.parse_ollama("NAME ID SIZE PROCESSOR UNTIL\n") == []


def _fake_run(args, timeout=3.0):
    if args[0] == "nvidia-smi":
        return NVSMI_APPS if "--query-compute-apps=process_name,used_memory" in args else NVSMI
    if args[0] == "powershell":
        return "AMD Radeon RX 9060 XT" + NL
    return OLLAMA_PS


def test_snapshot_shape_and_classification(monkeypatch):
    monkeypatch.setattr(g, "_run", _fake_run)
    g._CACHE.update(ts=0.0, data=None)
    snap = g.snapshot()
    assert set(snap) == {"gpus", "ollama", "ts"}
    nvidia, amd = snap["gpus"]
    assert set(nvidia["classify"]) == {"state", "foreign_holder", "free_mb", "ai_procs", "foreign_procs"}
    assert nvidia["classify"]["state"] == "gaming"
    # dwm.exe is on the ignore list, so the game is the holder
    assert nvidia["classify"]["foreign_holder"] == "game.exe"
    assert [p["name"] for p in nvidia["classify"]["foreign_procs"]] == ["game.exe"]
    assert nvidia["classify"]["free_mb"] == 8192 - 6100
    # ollama is loaded but not on the NVIDIA card -> the AMD card is marked ai
    assert amd["classify"]["state"] == "ai"
    assert snap["ollama"][0]["name"] == "llama3.2:3b"


def test_ignored_process_alone_is_idle():
    gpu = {"procs": [{"name": "dwm.exe", "mem_used": 600}], "mem_used": 900, "mem_total": 8192, "util": 30}
    c = g._classify(gpu)
    assert c["state"] == "idle" and c["foreign_procs"] == []
