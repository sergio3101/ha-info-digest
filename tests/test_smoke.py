"""Дымовой тест: все модули импортируются, а версия в коде совпадает с config.yaml."""

import importlib
import re
from pathlib import Path

import pytest

MODULES = ["app", "app.main", "app.web", "app.db", "app.llm", "app.labeler", "app.classifier",
           "app.taxonomy", "app.telegram_source", "app.util"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_version_matches_config():
    import app

    config = (Path(__file__).parent.parent / "info_digest" / "config.yaml").read_text(encoding="utf-8")
    version = re.search(r'^version:\s*"([^"]+)"', config, re.MULTILINE).group(1)
    assert app.__version__ == version
