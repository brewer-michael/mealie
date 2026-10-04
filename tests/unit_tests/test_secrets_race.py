"""
Fork: processes starting together on a new DATA_DIR (a web and a recipe card worker container on first boot) must
agree on one secret, or each signs tokens the others reject (mealie/core/settings/settings.py `determine_secrets`).
"""

import multiprocessing
from pathlib import Path

from mealie.core.settings.settings import determine_secrets

PROCESSES = 6


def _secret(args: tuple[str, object]) -> str:
    data_dir, barrier = args
    barrier.wait()  # type: ignore[attr-defined]
    return determine_secrets(Path(data_dir), ".secret", production=True)


def test_processes_starting_together_agree_on_one_secret(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    for round_number in range(5):
        data_dir = tmp_path / f"data-{round_number}"
        with context.Manager() as manager:
            barrier = manager.Barrier(PROCESSES)
            with context.Pool(PROCESSES) as pool:
                found = pool.map(_secret, [(str(data_dir), barrier)] * PROCESSES)
        assert len(set(found)) == 1, f"round {round_number}: {len(set(found))} secrets"
        assert (data_dir / ".secret").read_text() == found[0]
        assert [path.name for path in data_dir.iterdir()] == [".secret"]  # no temporary file left


def test_an_existing_or_empty_secret_is_handled_as_before(tmp_path: Path):
    (tmp_path / ".secret").write_text("existing-secret")
    assert determine_secrets(tmp_path, ".secret", production=True) == "existing-secret"

    (tmp_path / ".secret").write_text("  \n")
    made = determine_secrets(tmp_path, ".secret", production=True)
    assert made and made != "existing-secret"
    assert (tmp_path / ".secret").read_text() == made
    assert [path.name for path in tmp_path.iterdir()] == [".secret"]
