"""The gateway file, read and written — with the web UI as the editor.

The file names a command this machine executes. That is fine when a human edits
it in an editor and is a remote-code-execution hole the moment a web page can,
so validation is the load-bearing part of this module rather than a formality
(docs/design/20).
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from bridge.config import (ConfigError, DEFAULT_PROGRAMS, Store, program_warning,
                           validate_ssh)


def _write(tmp_path, document) -> str:
    path = tmp_path / "gateways.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _doc(**over):
    document = {
        "default": "midway5",
        "gateways": {
            "midway5": {
                "base_url": "http://localhost:8787",
                "token_env": "AGENT_BRIDGE_TOKEN",
                "ssh": "ssh -N -L 8787:localhost:8787 midway5",
            },
            "plain": {"base_url": "http://localhost:8788"},
        },
    }
    document.update(over)
    return document


def test_an_ssh_command_becomes_argv_with_quoting_honoured():
    assert validate_ssh('ssh -N -o "ProxyJump=a b" host') == (
        "ssh", "-N", "-o", "ProxyJump=a b", "host")
    assert validate_ssh(["ssh", "-N", "host"]) == ("ssh", "-N", "host")
    assert validate_ssh(None) == ()
    assert validate_ssh("") == ()


@pytest.mark.parametrize("command, expect", [
    ("ssh host && curl evil.example", "shell characters"),
    ("ssh host; rm -rf ~", "shell characters"),
    ("ssh host | tee /tmp/x", "shell characters"),
    ("ssh $(whoami)@host", "shell characters"),
    ("ssh host > /etc/passwd", "shell characters"),
])
def test_anything_that_wanted_a_shell_is_refused(command, expect):
    """It runs without a shell, so these would be passed to ssh as literal
    arguments — quietly meaning something other than what was written."""
    with pytest.raises(ConfigError) as exc:
        validate_ssh(command)
    assert expect in str(exc.value)


@pytest.mark.parametrize("command", [
    "curl http://evil.example/x",
    "/bin/sh -c whoami",
    "python3 /tmp/whatever.py",
    "bash",
])
def test_only_allowlisted_programs_may_be_run(command):
    """The UI can edit this field. Without the allowlist, that makes a loopback
    web page a general command runner on this machine."""
    with pytest.raises(ConfigError) as exc:
        validate_ssh(command)
    assert "must start with one of" in str(exc.value)
    assert "not a general command runner" in str(exc.value)


def test_the_allowlist_is_extensible_for_a_wrapper():
    argv = validate_ssh("my-ssh-wrapper host",
                        programs=DEFAULT_PROGRAMS + ("my-ssh-wrapper",))
    assert argv[0] == "my-ssh-wrapper"


def test_a_path_to_ssh_is_matched_on_its_basename():
    assert validate_ssh("/usr/bin/ssh -N host")[0] == "/usr/bin/ssh"


def test_a_missing_program_is_a_warning_not_a_refusal():
    """A config written on one machine and used on another must stay editable;
    the daemon reports the real failure when it runs it."""
    argv = validate_ssh("ssh -N host")
    assert "not on PATH" in program_warning(argv) or program_warning(argv) == ""


def test_entries_read_the_two_new_keys_and_leave_the_rest_alone(tmp_path):
    store = Store.load(_write(tmp_path, _doc()))
    entries = {entry.name: entry for entry in store.entries()}
    assert entries["midway5"].ssh == (
        "ssh", "-N", "-L", "8787:localhost:8787", "midway5")
    assert entries["midway5"].tunnelled
    assert entries["midway5"].local_port == 8787
    assert entries["midway5"].raw["token_env"] == "AGENT_BRIDGE_TOKEN"
    assert not entries["plain"].tunnelled, "no ssh key is not a tunnel"


def test_a_broken_ssh_value_does_not_hide_the_gateway(tmp_path):
    """A gateway you cannot see is a gateway you cannot fix from the UI."""
    document = _doc()
    document["gateways"]["midway5"]["ssh"] = "ssh host && oops"
    store = Store.load(_write(tmp_path, document))
    entries = {entry.name: entry for entry in store.entries()}
    assert "midway5" in entries
    assert entries["midway5"].ssh == ()
    assert "shell characters" in store.problems()["midway5"]


def test_the_public_view_never_carries_a_token(tmp_path):
    document = _doc()
    document["gateways"]["midway5"]["token"] = "super-secret-value"
    document["gateways"]["midway5"].pop("token_env")
    store = Store.load(_write(tmp_path, document))
    public = store.get("midway5").public()
    assert "super-secret-value" not in json.dumps(public)
    assert public["has_token"] is True
    assert public["token_source"] == "inline"


def test_an_edit_keeps_keys_it_knows_nothing_about(tmp_path):
    """`token_file` is the CLI's, not this daemon's; changing a port must not
    silently drop it."""
    document = _doc()
    document["gateways"]["midway5"] = {
        "base_url": "http://localhost:8787", "token_file": "~/.tok",
        "some_future_key": 42}
    path = _write(tmp_path, document)
    store = Store.load(path)
    store.put("midway5", {"base_url": "http://localhost:9999"})

    written = json.loads(open(path, encoding="utf-8").read())
    entry = written["gateways"]["midway5"]
    assert entry["base_url"] == "http://localhost:9999"
    assert entry["token_file"] == "~/.tok"
    assert entry["some_future_key"] == 42


def test_setting_one_token_source_clears_the_others(tmp_path):
    path = _write(tmp_path, _doc())
    store = Store.load(path)
    store.put("midway5", {"base_url": "http://localhost:8787",
                          "token_file": "~/.config/agent-bridge/midway5.token"})
    entry = json.loads(open(path, encoding="utf-8").read())["gateways"]["midway5"]
    assert entry["token_file"].endswith("midway5.token")
    assert "token_env" not in entry, "two sources set at once is a coin flip"


def test_a_write_keeps_a_backup_and_is_not_world_readable(tmp_path):
    path = _write(tmp_path, _doc())
    store = Store.load(path)
    store.put("newbie", {"base_url": "http://localhost:9000",
                         "ssh": "ssh -N -L 9000:localhost:9000 other"})

    backup = tmp_path / "gateways.json.bak"
    assert backup.is_file(), "a bad edit from a browser must be recoverable"
    assert "newbie" not in backup.read_text()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o077 == 0, f"the file can hold a token; mode was {oct(mode)}"


def test_deleting_the_default_leaves_the_file_loadable_by_ab(tmp_path):
    """`ab` refuses a config with several gateways and no default. An edit that
    made the CLI unusable would be a poor way to find that out."""
    path = _write(tmp_path, _doc(default="midway5"))
    store = Store.load(path)
    store.put("third", {"base_url": "http://localhost:8790"})
    store.delete("midway5")

    from client.abclient import load_gateways
    document = json.loads(open(path, encoding="utf-8").read())
    assert document["default"] in document["gateways"]
    gateways = load_gateways(path)
    assert gateways.default in gateways.gateways


def test_one_gateway_left_needs_no_default(tmp_path):
    path = _write(tmp_path, _doc())
    store = Store.load(path)
    store.delete("plain")
    document = json.loads(open(path, encoding="utf-8").read())
    assert "default" not in document or document["default"] == "midway5"

    from client.abclient import load_gateways
    assert load_gateways(path).default == "midway5"


def test_a_bad_base_url_is_refused(tmp_path):
    store = Store.load(_write(tmp_path, _doc()))
    for bad in ("", "midway5:8787", "ftp://x"):
        with pytest.raises(ConfigError):
            store.put("x", {"base_url": bad})


def test_a_toml_config_is_readable_but_not_writable(tmp_path):
    path = tmp_path / "gateways.toml"
    path.write_text(
        'default = "midway5"\n[gateways.midway5]\n'
        'base_url = "http://localhost:8787"\n'
        'ssh = "ssh -N -L 8787:localhost:8787 midway5"\n', encoding="utf-8")
    store = Store.load(str(path))
    assert store.get("midway5").tunnelled
    assert not store.writable
    with pytest.raises(ConfigError) as exc:
        store.put("midway5", {"base_url": "http://localhost:1"})
    assert "comments" in str(exc.value), "say why, not just no"


def test_a_config_the_cli_already_uses_still_loads_for_the_cli(tmp_path):
    """The two must agree about what `midway5` means, so this daemon manages the
    same file rather than one of its own."""
    path = _write(tmp_path, _doc())
    Store.load(path).put("midway5", {
        "base_url": "http://localhost:8787",
        "ssh": ["ssh", "-N", "-L", "8787:localhost:8787", "midway5"],
        "autostart": True})

    from client.abclient import load_gateways
    client = load_gateways(path).client("midway5", require_token=False)
    assert client.base == "http://localhost:8787"
