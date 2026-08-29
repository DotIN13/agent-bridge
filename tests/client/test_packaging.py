from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest

from client.abclient import ConfigError, load_gateways


def test_gateway_config_rejects_invalid_or_ambiguous_default(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"default": "typo", "gateways": {
        "one": {"base_url": "http://one", "token": "x"}}}))
    with pytest.raises(ConfigError, match="configured default gateway"):
        load_gateways(str(invalid))

    ambiguous = tmp_path / "ambiguous.json"
    ambiguous.write_text(json.dumps({"gateways": {
        "one": {"base_url": "http://one", "token": "x"},
        "two": {"base_url": "http://two", "token": "x"}}}))
    with pytest.raises(ConfigError, match="multiple gateways but no default"):
        load_gateways(str(ambiguous))

    single = tmp_path / "single.json"
    single.write_text(json.dumps({"gateways": {
        "only": {"base_url": "http://only", "token": "x"}}}))
    assert load_gateways(str(single)).default == "only"


def test_explicit_missing_client_config_does_not_fall_back(tmp_path, monkeypatch):
    (tmp_path / "gateways.json").write_text(json.dumps({
        "default": "fallback", "gateways": {
            "fallback": {"base_url": "http://fallback", "token": "x"}}}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="explicit gateway config not found"):
        load_gateways(str(tmp_path / "missing.json"))


def test_client_config_environment_variable(tmp_path, monkeypatch):
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"gateways": {
        "only": {"base_url": "http://only", "token": "x"}}}))
    monkeypatch.setenv("AGENT_BRIDGE_CLIENT_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)
    assert load_gateways().default == "only"


def test_gateway_launcher_fails_closed_for_explicit_missing_config(tmp_path, capsys):
    from gateway.__main__ import main
    assert main(["--config", str(tmp_path / "missing.toml")]) == 2
    assert "config not found" in capsys.readouterr().err


def test_console_scripts_and_dynamic_version_are_declared():
    root = Path(__file__).parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    assert config["project"]["scripts"] == {
        "ab": "client.ab:main",
        "agent-bridge": "gateway.__main__:main",
        "ab-notify": "client.ab_notify:main",
        "ab-monitor": "client.ab_monitor:main",
    }
    assert config["tool"]["setuptools"]["dynamic"]["version"]["attr"] == \
        "client._version.__version__"


def test_console_scripts_install_and_run_offline(tmp_path):
    root = Path(__file__).parents[2]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "agent_bridge_version.py"):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / "client", source / "client",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(root / "gateway", source / "gateway",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    subprocess.run([
        str(scripts / ("python.exe" if os.name == "nt" else "python")),
        "-m", "pip", "install", "-e", str(source), "--no-deps",
        "--no-build-isolation"], check=True, capture_output=True, text=True,
        timeout=120)
    suffix = ".exe" if os.name == "nt" else ""
    for command in ("ab", "agent-bridge", "ab-notify", "ab-monitor"):
        result = subprocess.run([str(scripts / f"{command}{suffix}"), "--help"],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
