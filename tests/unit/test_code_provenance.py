"""Exercise validation-driver provenance against actual Git state."""

import subprocess
from pathlib import Path

import pytest

from cryolens.eval.cohort import file_digest
from cryolens.eval.tracking import code_provenance


@pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
def test_validation_driver_changes_affect_source_hashes_and_git_dirty(
    tmp_path: Path, change: str
) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    driver = tmp_path / "validation/track_report.py"
    driver.parent.mkdir()
    driver.write_text("print('original driver')\n")
    source = tmp_path / "src/cryolens/preprocess/fixture.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "fixture")
    clean = code_provenance(tmp_path)
    assert clean["git_dirty"] is False
    assert clean["source_files_sha256"]["validation/track_report.py"] == file_digest(driver)
    if change == "untracked":
        driver = tmp_path / "validation/nested/new_driver.py"
        driver.parent.mkdir()
    driver.write_text("print('changed driver')\n")
    if change == "staged":
        git("add", "validation")
    changed = code_provenance(tmp_path)
    assert changed["git_dirty"] is True
    assert changed["git_commit"] == clean["git_commit"]
    assert changed["code_sha256"] != clean["code_sha256"]
    assert changed["preprocessing_sha256"] == clean["preprocessing_sha256"]
    assert changed["source_files_sha256"][driver.relative_to(tmp_path).as_posix()] == file_digest(
        driver
    )
