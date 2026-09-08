from __future__ import annotations

from asbxlib import cli


def test_main_list_with_no_config_returns_1_and_reports_no_config(config_root, capsys):
    rc = cli.main(["list"])

    assert rc == 1
    assert "no config at" in capsys.readouterr().err
