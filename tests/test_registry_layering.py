"""The Garmin registry's module layering, enforced.

`shared/garmin_registry_runtime.py` once reached back into the facade through a
`_registry()` function-local import for its constants, its clock and its error
classes (and the locks module kept a twin of it for its lock paths), while the
facade imported both at the top -- a cycle that only worked because one side
deferred its import to call time. Nothing noticed, because a late import passes
every test that happens to import the facade first. These tests make the
layering fail instead: no module names one above it in any import spelling, no
registry module hides an import inside a function or behind `importlib`, every
module imports alone in a fresh interpreter without side effects, and the
facade keeps each attribute the suite monkeypatches.
"""

import ast
import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.database import get_primary_person_id

REPO = Path(__file__).resolve().parent.parent

ERRORS = "shared.garmin_registry_errors"
LOCKS = "shared.garmin_registry_locks"
COMMON = "shared.garmin_registry_common"
RUNTIME = "shared.garmin_registry_runtime"
FACADE = "shared.garmin_registry"
LEGACY = "shared.garmin_registry_legacy"

# The DAG, written as the edges that must never exist: each module maps to the
# registry modules ABOVE it, which it may not name in any import spelling --
# `from shared import x`, `from shared.x import y`, `import shared.x`, aliased
# or not, at the top or inside a function. The module-form spelling matters:
# `from shared import garmin_registry_legacy` at the top of the facade imports
# cleanly in every order (Python resolves it against the partially initialised
# module) and would bring the cycle #64 removed straight back.
FORBIDDEN_EDGES = {
    ERRORS: (RUNTIME, FACADE, LEGACY),
    LOCKS: (RUNTIME, FACADE, LEGACY),
    COMMON: (RUNTIME, FACADE, LEGACY),
    RUNTIME: (FACADE, LEGACY),
    FACADE: (LEGACY,),
}
# The leaves, each with the only `shared.` modules it may import at all: the
# errors and the locks import nothing from the package (the facade hands the
# locks their paths); common needs the one bounded error it raises.
LEAF_IMPORTS = {
    ERRORS: (),
    LOCKS: (),
    COMMON: (ERRORS,),
}

REGISTRY_MODULES = sorted(str(p.relative_to(REPO)) for p in (REPO / "shared").glob("garmin_registry*.py"))
IMPORTABLE_MODULES = (ERRORS, LOCKS, COMMON, RUNTIME, FACADE, LEGACY)

# Every facade attribute the suite patches or reads, plus the registry
# attributes the legacy module builds on (`legacy_store_flock`,
# `_wait_for_call_permit`, ...) -- public constructors and re-exports that no
# test patches but whose loss would break adoption at boot. This catches a
# LOST attribute, not a seam that stopped being read through the facade:
# `reserve_call_permit` and `actor_has_effective_manage` are runtime-bound
# seams now (a patch on the facade no longer reaches `_wait_for_call_permit`
# or `_publish_link`; patch them on `shared.garmin_registry_runtime`), and
# runtime's clock and sleep respond to `garmin_registry.time.time` /
# `.asyncio.sleep` patches only because those are attributes on the shared
# stdlib module objects.
FACADE_PATCH_POINTS = (
    "GARTH_TOKEN_DIR",
    "time",
    "asyncio",
    "garmin_client",
    "call",
    "call_paced",
    "link",
    "relink",
    "unlink",
    "resolve_token_dir",
    "person_flock",
    "legacy_store_flock",
    "_person_lock_path",
    "_wait_for_call_permit",
    "_publish_link",
    "_reserve_generation",
    "_remove_token_dir",
    "_remove_person_token_root",
    "_ensure_token_root",
    "_generation_token_dir",
)


def _source_path(module: str) -> Path:
    return REPO / (module.replace(".", "/") + ".py")


def _parse(module: str) -> ast.AST:
    return ast.parse(_source_path(module).read_text(), filename=module)


def _imported_modules(node: ast.AST) -> list[str]:
    """Every module path an Import/ImportFrom node names, as dotted strings.

    `from shared import garmin_registry` names `shared.garmin_registry`; a
    `from shared.garmin_registry import x` names the module too, so both forms
    of reaching into a module are caught by one prefix check.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [module] + [f"{module}.{alias.name}" if module else alias.name for alias in node.names]
    return []


def _names_module(target: str, module: str) -> bool:
    return target == module or target.startswith(module + ".")


def _import_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def test_the_dag_tables_cover_every_registry_module():
    """The DAG and fresh-interpreter tests iterate hand-written tables; a new
    `shared/garmin_registry_<x>.py` gets no enforcement until it is placed in
    them, and an emptied table would pass every check. So the tables and the
    glob must agree exactly, and the glob must still match."""
    assert len(REGISTRY_MODULES) >= 4, f"only found {REGISTRY_MODULES}; the glob is not matching"
    place = "place it in the DAG: add it to IMPORTABLE_MODULES and to FORBIDDEN_EDGES (or LEAF_IMPORTS)"
    listed = {_source_path(m).relative_to(REPO).as_posix() for m in IMPORTABLE_MODULES}
    assert listed == set(REGISTRY_MODULES), (
        f"IMPORTABLE_MODULES and the shared/garmin_registry*.py glob disagree "
        f"(only in glob: {sorted(set(REGISTRY_MODULES) - listed)}, only in table: {sorted(listed - set(REGISTRY_MODULES))}); "
        f"a new registry module must be listed: {place}"
    )
    ruled = set(FORBIDDEN_EDGES) | {LEGACY}
    assert ruled == set(IMPORTABLE_MODULES), (
        f"every registry module but the top one needs a FORBIDDEN_EDGES row "
        f"(missing: {sorted(set(IMPORTABLE_MODULES) - ruled)}, extra: {sorted(ruled - set(IMPORTABLE_MODULES))}); {place}"
    )
    assert set(LEAF_IMPORTS) == {ERRORS, LOCKS, COMMON}, (
        f"LEAF_IMPORTS must name exactly the leaves (got {sorted(LEAF_IMPORTS)}); "
        "a new leaf needs a row here saying what it may import from shared., a non-leaf needs none"
    )


def test_no_registry_module_imports_one_above_it():
    """Walk EVERY node, not just the module top level: the shim was a
    function-local import, which is exactly where a cycle hides."""
    violations = []
    for module, forbidden in FORBIDDEN_EDGES.items():
        if not _source_path(module).is_file():
            violations.append(f"{module} does not exist")
            continue
        for node in _import_nodes(_parse(module)):
            for target in _imported_modules(node):
                for above in forbidden:
                    if _names_module(target, above):
                        violations.append(f"{module}:{node.lineno} imports {above} (via {target})")
    assert not violations, "registry modules reach up the layering:\n" + "\n".join(violations)


def test_leaves_import_nothing_else_from_the_package():
    foreign = []
    for leaf, allowed in LEAF_IMPORTS.items():
        for node in _import_nodes(_parse(leaf)):
            for target in _imported_modules(node):
                # `from shared import x` also names the bare package; only the
                # submodule entry it produces is the edge being checked.
                if target == "shared" or not target.startswith("shared"):
                    continue
                if not any(_names_module(target, module) for module in allowed):
                    foreign.append(f"{leaf}:{node.lineno} imports {target} (allowed: {list(allowed) or 'nothing'})")
    assert not foreign, "a leaf imports more of shared. than it may:\n" + "\n".join(foreign)


def test_no_function_local_imports_in_registry_modules():
    violations = []
    for module in REGISTRY_MODULES:
        tree = ast.parse((REPO / module).read_text(), filename=module)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for statement in function.body:
                for node in ast.walk(statement):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        violations.append(f"{module}:{node.lineno} inside {function.name}()")
    assert not violations, "function-local imports in registry modules:\n" + "\n".join(violations)


def test_no_smuggled_imports_in_registry_modules():
    """The import checks above read Import/ImportFrom nodes; close the other
    doors. Flagged: any relative import (it names no `shared.` path); a bare
    `import shared` (the package's attributes resolve submodules at call time,
    which is the old shim in a new spelling); any `import importlib[.x]` or
    `from importlib[.x] import ...`; and any bare use of the names `importlib`,
    `__import__` or `import_module`, which is what a call through them looks
    like once the import itself is disguised (aliasing `import_module` to
    another name is not caught -- nor is anything that reaches the facade
    through `sys.modules`)."""
    violations = []
    for module in REGISTRY_MODULES:
        tree = ast.parse((REPO / module).read_text(), filename=module)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                violations.append(f"{module}:{node.lineno} relative import")
            elif isinstance(node, ast.Import) and any(_names_module(alias.name, "importlib") for alias in node.names):
                violations.append(f"{module}:{node.lineno} imports importlib")
            elif isinstance(node, ast.ImportFrom) and _names_module(node.module or "", "importlib"):
                violations.append(f"{module}:{node.lineno} imports from importlib")
            elif isinstance(node, ast.Import) and any(alias.name == "shared" for alias in node.names):
                # `import shared` + `shared.garmin_registry.X` at call time is the _registry() shim
                # in a new spelling; no registry module needs the bare package.
                violations.append(f"{module}:{node.lineno} imports the bare `shared` package")
            elif isinstance(node, ast.Name) and node.id in ("importlib", "__import__", "import_module"):
                violations.append(f"{module}:{node.lineno} uses {node.id}")
    assert not violations, "late-import mechanisms in registry modules:\n" + "\n".join(violations)


def _tree_listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("module", IMPORTABLE_MODULES)
def test_each_registry_module_imports_alone_in_a_fresh_interpreter(module, tmp_path):
    """The import-order-cycle guard: importing the legacy module first (as both
    lifespans do) must not blow up, and no module may depend on a sibling
    having been imported before it. The interpreter gets an allowlisted
    environment, never the developer's, and the import must leave no file
    behind anywhere it could reach."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DB_PATH": str(tmp_path / "vf-test.db"),
        "GARTH_TOKEN_DIR": str(tmp_path / "garth"),
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    shared_before = _tree_listing(REPO / "shared")
    top_before = sorted(p.name for p in REPO.iterdir())

    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, f"`import {module}` failed in a fresh interpreter:\n{completed.stderr}"
    assert _tree_listing(REPO / "shared") == shared_before, f"`import {module}` wrote into shared/"
    assert sorted(p.name for p in REPO.iterdir()) == top_before, f"`import {module}` wrote into the repo root"
    assert sorted(tmp_path.iterdir()) == [], (
        f"`import {module}` created {sorted(p.name for p in tmp_path.iterdir())} under HOME/DB_PATH/GARTH_TOKEN_DIR"
    )


def test_facade_keeps_its_public_patch_points():
    facade = importlib.import_module(FACADE)
    errors = importlib.import_module(ERRORS)
    legacy = importlib.import_module(LEGACY)

    missing = [name for name in FACADE_PATCH_POINTS if not hasattr(facade, name)]
    error_classes = [name for name in dir(errors) if name.startswith("Garmin")]
    assert error_classes, "no Garmin* classes found in the errors module; the check below is vacuous"
    missing += [name for name in error_classes if not hasattr(facade, name)]
    assert not missing, f"the facade lost patch points the suite relies on: {missing}"

    assert callable(getattr(legacy, "bootstrap_legacy_token_store", None))
    assert not hasattr(facade, "bootstrap_legacy_token_store"), (
        "bootstrap_legacy_token_store moved to shared.garmin_registry_legacy; a stale facade import must fail loud"
    )


async def test_patching_the_facade_token_root_governs_legacy_adoption(initialized_db, monkeypatch, tmp_path, caplog):
    """Adoption must prepare the token root the facade holds at CALL time.

    A copy of GARTH_TOKEN_DIR read at import time would send adoption to
    /app/data/.garth no matter what conftest patched; the proof is the patched
    root coming into existence, private, while the flat-store probe skips.
    """
    facade = importlib.import_module(FACADE)
    legacy = importlib.import_module(LEGACY)
    root = tmp_path / "elsewhere"
    person_id = await get_primary_person_id()
    monkeypatch.setattr(facade, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(
        facade.garmin_client, "authenticate", lambda *_args: pytest.fail("no flat store: nothing to verify")
    )
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")
    caplog.set_level(logging.INFO, logger="shared.garmin_registry_legacy")

    assert await legacy.bootstrap_legacy_token_store() is False

    assert "no flat token store is present" in caplog.text
    assert root.is_dir(), "adoption prepared some other root than the facade's patched GARTH_TOKEN_DIR"
    assert root.stat().st_mode & 0o777 == 0o700
