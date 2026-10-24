import shutil
import subprocess
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra"

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")


def terraform(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", f"-chdir={INFRA}", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_fmt():
    result = terraform("fmt", "-check", "-recursive")
    assert result.returncode == 0, f"needs terraform fmt:\n{result.stdout}{result.stderr}"


def test_validate():
    init = terraform("init", "-backend=false", "-input=false")
    assert init.returncode == 0, init.stderr
    result = terraform("validate", "-no-color")
    assert result.returncode == 0, result.stderr
