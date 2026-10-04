"""
Fork: the code generators give the same output on every machine and run (docs/ai/PHASE2.md §18): the schema
`__init__.py` files keep their `__all__` order, and the recipe card eval fixtures keep their file names.
"""

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

CODE_GENERATION_DIR = Path(__file__).parents[2] / "dev" / "code-generation"
SCHEMA_DIR = Path(__file__).parents[2] / "mealie" / "schema"


def _import(monkeypatch: pytest.MonkeyPatch, name: str) -> Iterator[ModuleType]:
    """Imports a generator without leaking its top-level `utils` package into `sys.modules`"""
    monkeypatch.syspath_prepend(str(CODE_GENERATION_DIR))
    try:
        yield importlib.import_module(name)
    finally:
        for module in list(sys.modules):
            if module == name or module == "utils" or module.startswith("utils."):
                del sys.modules[module]


@pytest.fixture
def schema_exports(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    yield from _import(monkeypatch, "gen_py_schema_exports")


@pytest.fixture
def data_paths(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    yield from _import(monkeypatch, "gen_py_pytest_data_paths")


def test_existing_exports_keep_their_order_and_new_ones_follow_sorted(schema_exports: ModuleType):
    order = schema_exports.export_order(["Zeta", "Alpha", "Gone", "Mid"], ["Alpha", "New", "Mid", "Zeta", "Beta"])
    assert order == ["Zeta", "Alpha", "Mid", "Beta", "New"]
    assert schema_exports.export_order([], ["b", "a"]) == ["a", "b"]


@pytest.mark.parametrize("module", sorted(path.parent.name for path in SCHEMA_DIR.glob("*/__init__.py")))
def test_generating_the_exports_again_changes_nothing(schema_exports: ModuleType, module: str):
    """Each checked-in `__all__` is what the generator writes for the module's classes (no reordering)"""
    directory = SCHEMA_DIR / module
    if directory.name in schema_exports.SKIP:
        pytest.skip("not generated")
    classes = [name for file in schema_exports.Modules(directory=directory).files for name in file.classes]
    existing = schema_exports.existing_exports(directory / "__init__.py")
    assert schema_exports.export_order(existing, classes) == existing


def test_the_modules_files_are_read_in_name_order(schema_exports: ModuleType):
    files = [file.import_path for file in schema_exports.Modules(directory=SCHEMA_DIR / "recipe_ingest").files]
    assert files == sorted(files)


def test_eval_card_fixtures_are_never_renamed(data_paths: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The eval fixtures' JSON names its photos (`source`), so a kebab-case rename would break them"""
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "IMG_2503.JPG").write_bytes(b"photo")
    (tmp_path / "cards" / "Grandmas Pancakes.json").write_text("{}")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "Some Photo.jpg").write_bytes(b"photo")
    monkeypatch.setattr(data_paths, "TEST_DATA", tmp_path)

    data_paths.rename_non_compliant_paths()

    assert sorted(path.name for path in (tmp_path / "cards").iterdir()) == ["Grandmas Pancakes.json", "IMG_2503.JPG"]
    assert [path.name for path in (tmp_path / "images").iterdir()] == ["some-photo.jpg"]
