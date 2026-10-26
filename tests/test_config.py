import json

import pytest

import config


def test_defaults_file_and_env_precedence(tmp_path):
    assert config.load(env={}, path=tmp_path / "missing.json") == config.DEFAULTS
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"ORCH_PORT": 9000, "SESSION_BROKER_URL": "http://h:1", "BOGUS": "x",
                             "gaming_ignore": ["a.exe"], "SESSION_RUNS_DIR": None}))
    cfg = config.load(env={"ORCH_PORT": "9100"}, path=f)
    assert cfg["ORCH_PORT"] == "9100"                      # env beats file
    assert cfg["SESSION_BROKER_URL"] == "http://h:1"       # file beats default
    assert cfg["OLLAMA_URL"] == config.DEFAULTS["OLLAMA_URL"]
    assert cfg["gaming_ignore"] == ["a.exe"]
    assert cfg["SESSION_RUNS_DIR"] is None                 # JSON null stays None
    assert "BOGUS" not in cfg


def test_empty_env_clears_value(tmp_path):
    assert config.load(env={"SESSION_BROKER_URL": ""}, path=tmp_path / "x.json")["SESSION_BROKER_URL"] == ""


def test_bad_inputs_have_clear_errors(tmp_path):
    with pytest.raises(ValueError, match="ORCH_PORT must be an integer"):
        config.load(env={"ORCH_PORT": "abc"}, path=tmp_path / "x.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        config.load(env={}, path=bad)
