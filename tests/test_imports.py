"""Import every module under ``src/prolibspector`` and fail on any ImportError.

WHY THIS TEST EXISTS
====================
This repository once had a fully green test suite while the application was
completely unable to start: eleven internal modules were imported at module
scope but did not exist on disk, so both documented entry points died with
ModuleNotFoundError. Nothing in the suite imported the app, so nothing caught it.

This test makes that failure mode impossible to reintroduce. It walks the
package and imports every module, so a deleted module, a renamed symbol in an
``from X import Y`` list, or a missing third-party dependency fails CI
immediately.

It is intentionally cheap and dependency-honest: it imports only, and never
constructs a Tk root, so it works the same headless as on a desktop. (Linux CI
still runs it under ``xvfb-run``, because Tk resolves a display when some
matplotlib backends initialise.)
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import prolibspector


def _module_names() -> list[str]:
    """Return every importable module name under the ``prolibspector`` package."""
    return sorted(
        module.name
        for module in pkgutil.walk_packages(prolibspector.__path__, prolibspector.__name__ + ".")
    )


MODULE_NAMES = _module_names()

#: Entry points named in the README. These must always import.
DOCUMENTED_ENTRY_POINTS = (
    "prolibspector.analysis.app",
    "prolibspector.acquisition.app",
    "prolibspector.app.bootstrap",
    "prolibspector.app.launcher",
)


def test_package_discovery_found_modules():
    """Guard the guard: an empty module list would make this suite vacuous."""
    assert len(MODULE_NAMES) > 20, f"Only discovered {len(MODULE_NAMES)} modules: {MODULE_NAMES}"


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_module_imports(module_name):
    """Every module under src/prolibspector must import cleanly."""
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        pytest.fail(f"{module_name} failed to import: {type(exc).__name__}: {exc}")


@pytest.mark.parametrize("module_name", DOCUMENTED_ENTRY_POINTS)
def test_documented_entry_points_import(module_name):
    """The entry points the README tells users to run must import."""
    assert importlib.import_module(module_name) is not None


def test_no_module_references_a_missing_internal_module():
    """Report every unresolvable internal import together, not one at a time.

    Parametrised failures stop at the first assertion per module; this gives a
    single consolidated list, which is what made the original breakage tedious
    to diagnose.
    """
    failures: list[str] = []
    for module_name in MODULE_NAMES:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            failures.append(f"  {module_name}: {type(exc).__name__}: {exc}")
    assert not failures, "Modules failed to import:\n" + "\n".join(failures)
