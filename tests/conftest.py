import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pcfind.cli import find_rg  # noqa: E402

if not find_rg({}):
    pytest.skip("no ripgrep on PATH or in a known vendored location", allow_module_level=True)


@pytest.fixture
def pcfind(tmp_path):
    def run(*args, cwd=None, config=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONUTF8"] = "1"
        env["PCFIND_CONFIG"] = str(config or tmp_path / "no-config.toml")
        p = subprocess.run([sys.executable, "-m", "pcfind", *map(str, args)],
                           capture_output=True, text=True, encoding="utf-8", env=env,
                           cwd=cwd or tmp_path, timeout=120)
        p.lines = [l for l in p.stdout.splitlines() if l.strip()]
        return p
    return run


class Tree:
    def __init__(self, path):
        self.path = path
        path.mkdir()

    def __truediv__(self, rel):
        return self.path / rel

    def __fspath__(self):
        return str(self.path)

    __str__ = __fspath__

    def put(self, rel, data):
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            f.write_bytes(data)
        else:
            f.write_text(data, encoding="utf-8", newline="")
        return f


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path / "tree")
