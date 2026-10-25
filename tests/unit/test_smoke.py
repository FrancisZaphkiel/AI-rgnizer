from pathlib import Path

import pytest

import organizador
from organizador.cli import main


def test_package_imports() -> None:
    assert organizador.APP_NAME == "organizador"


def test_cli_paths_runs(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["paths"]) == 0
    assert str(isolated_home) in capsys.readouterr().out
