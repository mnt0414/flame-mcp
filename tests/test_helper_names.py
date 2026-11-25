"""The helpers share one namespace, so one name must not be defined by two of them."""

from __future__ import annotations

import ast
import builtins
import symtable
from collections import defaultdict

from conftest import shared_registry
from flame_mcp import execution


def _top_level_defs(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def test_no_function_or_class_is_defined_by_two_helpers() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for attr in dir(execution):
        value = getattr(execution, attr)
        if attr.endswith("_HELPER") and isinstance(value, str):
            for name in _top_level_defs(value):
                owners[name].append(attr)
    clashes = {name: sorted(set(who)) for name, who in owners.items() if len(set(who)) > 1}
    assert clashes == {}


def _referenced_globals(table: symtable.SymbolTable) -> set[str]:
    names = {
        sym.get_name()
        for sym in table.get_symbols()
        if sym.is_referenced() and sym.is_global() and table.get_type() != "module"
    }
    for child in table.get_children():
        names |= _referenced_globals(child)
    return names


def _assigned_globals(table: symtable.SymbolTable) -> set[str]:
    """Names bound at module level, plus names a function binds after `global NAME`."""
    names = {
        sym.get_name()
        for sym in table.get_symbols()
        if (table.get_type() == "module" or sym.is_declared_global())
        and (sym.is_assigned() or sym.is_imported() or sym.is_namespace())
    }
    for child in table.get_children():
        names |= _assigned_globals(child)
    return names


def _undefined_global_names(code: str) -> set[str]:
    table = symtable.symtable(code, "<exec-code>", "exec")
    known = _assigned_globals(table) | set(dir(builtins)) | {"flame"}
    return _referenced_globals(table) - known


def _full_exec_code() -> str:
    op = shared_registry().get("project.info")
    assert op is not None and op.to_python is not None
    return execution.build_exec_code(op, {})


def test_every_global_a_helper_function_reads_is_defined_or_builtin_or_flame() -> None:
    code = _full_exec_code()
    assert len(code) > 100_000
    assert _undefined_global_names(code) == set()


def test_the_undefined_name_check_catches_a_name_that_nothing_defines() -> None:
    code = _full_exec_code() + "\n\ndef _planted():\n    return _name_nothing_defines\n"
    assert _undefined_global_names(code) == {"_name_nothing_defines"}
