import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # sandbox/, so `import asbxlib` works

import copy
import pytest
from asbxlib import config


@pytest.fixture
def cfg():
    return copy.deepcopy(config.DEFAULT_CONFIG)


@pytest.fixture
def config_root(tmp_path, monkeypatch):
    """Points ASBX_CONFIG_DIR at a tmp dir; returns its projects/ path."""
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path))
    projects = tmp_path / "projects"
    projects.mkdir()
    return projects
