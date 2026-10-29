import shutil
import subprocess
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra"
BENCHMARK = INFRA / "benchmark"

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


def test_app_scales_to_zero_and_one_replica_with_no_keys():
    app = (INFRA / "app.tf").read_text(encoding="utf-8")
    assert "min_replicas = 0" in app and "max_replicas = 1" in app  # limits.py relies on 1
    # GHCR pull token comes from a sensitive variable, never a literal
    assert 'password_secret_name = "ghcr-pull-token"' in app and "var.ghcr_pull_token" in app
    assert "primary_key" not in app and "api_key" not in app.lower()
    assert 'issuer                    = "https://token.actions.githubusercontent.com"' in app


def test_benchmark_validate():
    init = subprocess.run(
        ["terraform", f"-chdir={BENCHMARK}", "init", "-backend=false", "-input=false"],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert init.returncode == 0, init.stderr
    result = subprocess.run(
        ["terraform", f"-chdir={BENCHMARK}", "validate", "-no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
