"""scripts/backup.py as a module for the tests (scripts/ is not a package)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

NAME = "commons_backup"
PATH = Path(__file__).resolve().parents[1] / "scripts" / "backup.py"


def load():
    if NAME in sys.modules:
        return sys.modules[NAME]
    spec = importlib.util.spec_from_file_location(NAME, PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module
