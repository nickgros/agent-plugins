from __future__ import annotations

from asbxlib import cli


def test_main_list_with_no_config_returns_1_and_reports_no_config(config_root, capsys):
    rc = cli.main(["list"])

    assert rc == 1
    assert "no config at" in capsys.readouterr().err


def test_settings_sync_command_is_registered_with_an_optional_group():
    parser = cli.build_parser()

    assert parser.parse_args(["settings-sync"]).group is None
    assert parser.parse_args(["settings-sync", "demo"]).group == "demo"


def test_settings_sync_unknown_group_reports_missing_manifest(config_root, capsys):
    (config_root.parent / "config.yaml").write_text("")

    rc = cli.main(["settings-sync", "nope"])

    assert rc == 1
    assert "no manifest at" in capsys.readouterr().err
