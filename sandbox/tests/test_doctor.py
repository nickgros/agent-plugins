"""Tests for asbxlib.doctor."""
from __future__ import annotations

from fake_incus import FakeIncus

from asbxlib import doctor


def test_cmd_doctor_missing_config_returns_1_and_reports_fail(monkeypatch, capsys):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "tailscale_status", lambda: {"BackendState": "Running"})

    rc = doctor.cmd_doctor(FakeIncus(), None, fix=False)

    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL config present" in out
