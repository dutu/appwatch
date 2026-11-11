from __future__ import annotations

import copy
import json
import os
import pwd
import sys
from pathlib import Path

import pytest

from app_watch.cli import main
from app_watch.config import load_config, prepare_config, validate_document
from app_watch.config.runtime import parse_parameters
from app_watch.errors import ConfigurationError


@pytest.fixture
def document(tmp_path):
    return {
        "config_version": 1,
        "job": {"id": "electrum-main"},
        "application": {
            "name": "electrum",
            "data_dir": str(tmp_path),
            "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
        },
        "action": {"argv": [sys.executable, "-c", 'print("hello")']},
    }


def test_defaults_and_preparation_are_detached(document, tmp_path):
    original = copy.deepcopy(document)
    raw = validate_document(document)
    before = raw.model_dump()
    prepared = prepare_config(raw, tmp_path / "job.yaml")
    assert raw.model_dump() == before
    assert document == original
    assert prepared.owner_uid == os.geteuid()
    assert prepared.config.trigger.enabled is False
    assert prepared.config.trigger.poll_seconds == 2
    assert prepared.home == Path(pwd.getpwuid(os.geteuid()).pw_dir)
    assert len(prepared.scope) == 16
    assert len(prepared.fingerprint()) == 64


@pytest.mark.parametrize(
    "field", ["sources", "destination", "archive", "schedule", "retention", "hooks"]
)
def test_backup_and_schedule_settings_rejected(document, field):
    document[field] = {}
    with pytest.raises(ConfigurationError, match=field):
        validate_document(document)


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_schema_version_is_explicit_integer(document, value):
    document["config_version"] = value
    with pytest.raises(ConfigurationError, match="config_version"):
        validate_document(document)


@pytest.mark.parametrize("argv", [[], ["/bin/echo", 2], ["echo"], [""], ["~"], ["/bin/echo", "\0"]])
def test_invalid_command_rejected(document, argv):
    document["action"]["argv"] = argv
    with pytest.raises(ConfigurationError):
        validate_document(document)


@pytest.mark.parametrize(
    "command",
    [
        "tool --option",
        "./tool",
        "tools/notifier",
        "../tool",
        " ",
        "tool\targ",
        "/opt/my notifier",
        "tool\narg",
        "tool\u00a0arg",
        "tool;other",
        "tool|other",
        "tool&",
        "tool<input",
        "tool>output",
        "$(tool)",
        "`tool`",
        "'tool'",
        '"tool"',
        "tool\\name",
        "tool(arg)",
        "tool{arg}",
        "tool[arg]",
        "tool*",
        "tool?",
        "tool!",
        "tool#comment",
        "~/tool",
        "/usr/bin/",
        "/",
        "--tool",
        ".",
        "..",
        "tool\x1b",
    ],
)
def test_external_notifier_rejects_invalid_executable_forms(document, command):
    document["external_notifier"] = {"command": command}
    with pytest.raises(ConfigurationError, match="one executable"):
        validate_document(document)


@pytest.mark.parametrize("command", ["tool", "telegram-notify", "tool.py", "/opt/tools/notifier"])
def test_external_notifier_accepts_path_name_or_absolute_executable(document, command):
    document["external_notifier"] = {"command": command}
    assert validate_document(document).external_notifier.command == command


@pytest.mark.parametrize("value", [0, -1, "2", True, float("inf"), float("nan")])
def test_invalid_timing_rejected(document, value):
    document["trigger"] = {"poll_seconds": value}
    with pytest.raises(ConfigurationError, match="poll_seconds"):
        validate_document(document)


def test_unknown_app_and_environment_predicates_rejected(document):
    document["application"]["name"] = "unknown"
    with pytest.raises(ConfigurationError, match="Unsupported application"):
        validate_document(document)
    document["application"]["name"] = "bisq"
    document["application"]["matcher"]["env_eq"] = {"ELECTRUMDIR": "/data"}
    with pytest.raises(ConfigurationError, match="unsupported matcher environment keys"):
        validate_document(document)


@pytest.mark.parametrize(
    "text",
    [
        "config_version: 1\nconfig_version: 1\n",
        "[]",
        '!!python/object/apply:os.system ["false"]',
        "application: [unterminated",
    ],
)
def test_unsafe_or_invalid_yaml_fails(tmp_path, text):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_duplicate_entrypoints_and_invalid_regex(document):
    matcher = document["application"]["matcher"]
    matcher["entrypoints"] *= 2
    with pytest.raises(ConfigurationError, match="unique"):
        validate_document(document)
    matcher["entrypoints"] = matcher["entrypoints"][:1]
    matcher["cmdline_regex"] = "["
    with pytest.raises(ConfigurationError, match="invalid process regex"):
        validate_document(document)


def test_missing_config_and_invalid_encoding(tmp_path):
    path = tmp_path / "absent.yaml"
    with pytest.raises(ConfigurationError, match="Cannot load"):
        load_config(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="Cannot load"):
        load_config(path)


def test_parameters_render_without_shell_and_cli_overrides_defaults(document, tmp_path):
    document["trigger"] = {"params": {"NETWORK": "mainnet"}}
    document["action"]["argv"] = [sys.executable, "{NETWORK}", "$(id); *", "{{literal}}", ""]
    raw = validate_document(document)
    prepared = prepare_config(raw, tmp_path / "job.yaml", runtime_params={"NETWORK": "testnet"})
    assert prepared.config.action.argv[1:] == ["testnet", "$(id); *", "{literal}", ""]
    assert raw.action.argv[1] == "{NETWORK}"
    another = prepare_config(raw, tmp_path / "job.yaml")
    assert another.scope != prepared.scope


@pytest.mark.parametrize(
    "argument", ["NAME", "=value", "0NAME=value", "NAME=", "NAME=bad/value", "NAME=hi there"]
)
def test_bad_parameter_syntax(argument):
    with pytest.raises(ConfigurationError):
        parse_parameters([argument])


def test_duplicate_parameter_rejected():
    with pytest.raises(ConfigurationError, match="Duplicate"):
        parse_parameters(["NAME=one", "NAME=two"])


def test_recursive_yaml_alias_fails_without_traceback(tmp_path):
    path = tmp_path / "recursive.yaml"
    path.write_text("recursive: &loop [*loop]\n")
    with pytest.raises(ConfigurationError, match="Recursive"):
        load_config(path)


def test_executable_symlink_is_preserved(document, tmp_path):
    link = tmp_path / "python"
    link.symlink_to(sys.executable)
    document["action"]["argv"][0] = str(link)
    prepared = prepare_config(validate_document(document), tmp_path / "job.yaml")
    assert prepared.config.action.argv[0] == str(link)


@pytest.mark.parametrize(
    "template", ["{MISSING}", "{NAME.attr}", "{NAME[0]}", "{NAME!s}", "{NAME:>5}", "{"]
)
def test_invalid_or_missing_templates_rejected(document, tmp_path, template):
    document["action"]["argv"].append(template)
    with pytest.raises(ConfigurationError):
        prepare_config(validate_document(document), tmp_path / "job.yaml")


def test_unused_params_rejected(document, tmp_path):
    with pytest.raises(ConfigurationError, match="Unused"):
        prepare_config(
            validate_document(document), tmp_path / "job.yaml", runtime_params={"NO": "value"}
        )


def test_prepared_identity_and_fingerprint(document, tmp_path):
    path = tmp_path / "job.yaml"
    first = prepare_config(validate_document(document), path)
    document["action"]["argv"].append("changed")
    second = prepare_config(validate_document(document), path)
    assert first.scope == second.scope
    assert first.fingerprint() != second.fingerprint()
    document["job"]["id"] = "other"
    assert prepare_config(validate_document(document), path).scope != first.scope


def test_tilde_uses_account_home_not_environment(document, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "untrusted"))
    document["application"]["data_dir"] = "~/.electrum"
    prepared = prepare_config(
        validate_document(document), tmp_path / "job.yaml", check_runtime=False
    )
    assert prepared.config.application.data_dir == str(prepared.home / ".electrum")


def test_literal_environment_predicate_is_not_canonicalized(document, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    document["application"]["matcher"]["env_eq"] = {"ELECTRUMDIR": str(alias)}
    prepared = prepare_config(validate_document(document), tmp_path / "job.yaml")
    assert prepared.matcher.env_eq["ELECTRUMDIR"] == str(alias)


def test_runtime_directory_and_executable_checks(document, tmp_path):
    document["application"]["data_dir"] = str(tmp_path / "missing")
    with pytest.raises(ConfigurationError, match="data_dir"):
        prepare_config(validate_document(document), tmp_path / "job.yaml")
    document["application"]["data_dir"] = str(tmp_path)
    document["action"]["argv"][0] = str(tmp_path / "missing-program")
    with pytest.raises(ConfigurationError, match="executable is unavailable"):
        prepare_config(validate_document(document), tmp_path / "job.yaml")


def test_examples_validate_without_local_wallet_installations():
    for path in Path("examples").glob("*.yaml"):
        config = load_config(path)
        assert not config.trigger.enabled
        assert config.action.argv[0] == "/usr/bin/printf"
        prepare_config(config, path, check_runtime=False)


def test_cli_validation_and_errors(document, tmp_path, capsys):
    path = tmp_path / "job.yaml"
    path.write_text(json.dumps(document))
    assert main(["--user", "--config", str(path), "validate"]) == 0
    assert "configuration and paths" in capsys.readouterr().out
    document["application"]["data_dir"] = "/nonexistent/app-watch-test"
    path.write_text(json.dumps(document))
    assert main(["--user", "--config", str(path), "validate", "--schema-only"]) == 0
    assert main(["--user", "--config", str(path), "validate"]) == 2
    assert "data_dir" in capsys.readouterr().err


def test_cli_requires_explicit_scope_and_config():
    with pytest.raises(SystemExit) as error:
        main(["validate"])
    assert error.value.code == 2
    with pytest.raises(SystemExit):
        main(["--user", "validate"])
