"""The script sent to Flame is a string, so mypy and ruff never see a name a helper uses without
defining it. Every registered op's script, its op code and its verify code are checked here for a
name that is read but defined nowhere, is no builtin, and is not given by the bridge."""

from __future__ import annotations

import builtins
import symtable
from collections.abc import Iterator, Mapping
from functools import cache
from typing import Any

from conftest import shared_registry
from flame_mcp import execution
from flame_mcp.registry import RegisteredOperation

# The globals the bridge passes to exec(): `ns = {"flame": ..., "__name__": ...}`
# (src/flame_mcp_bridge/impl.py:1048). stub/flame_mcp_bridge.py only execs impl.py itself
# (stub/flame_mcp_bridge.py:104), so it hands nothing to the code Flame runs.
BRIDGE_GLOBALS = frozenset({"flame", "__name__"})

# Set by the script around the op code, which runs with exec(..., globals()) in the same namespace.
SCRIPT_GLOBALS = frozenset({"__builtins__", "__file__"})


def _bound_names(table: symtable.SymbolTable) -> set[str]:
    """Names this code binds in the global namespace: module-level bindings, and names a function
    or class binds after `global NAME`."""
    names: set[str] = set()
    for sym in table.get_symbols():
        binds = sym.is_assigned() or sym.is_imported() or sym.is_namespace()
        if binds and (table.get_type() == "module" or sym.is_declared_global()):
            names.add(sym.get_name())
    for child in table.get_children():
        names |= _bound_names(child)
    return names


def _read_globals(table: symtable.SymbolTable) -> set[str]:
    """Names read from the global namespace (locals, arguments, comprehension targets, except
    names and closure cells are resolved by symtable and never show up here)."""
    names: set[str] = set()
    for sym in table.get_symbols():
        if sym.is_referenced() and (sym.is_global() or table.get_type() == "module"):
            if table.get_type() != "module" or not sym.is_local():
                names.add(sym.get_name())
            elif not (sym.is_assigned() or sym.is_imported() or sym.is_namespace()):
                names.add(sym.get_name())
    for child in table.get_children():
        names |= _read_globals(child)
    return names


def _table(code: str, label: str) -> symtable.SymbolTable:
    return symtable.symtable(code, label, "exec")


@cache
def _analysis(code: str) -> tuple[frozenset[str], frozenset[str]]:
    table = _table(code, "<exec-code>")
    return frozenset(_bound_names(table)), frozenset(_read_globals(table))


def _analyse_split(code: str) -> tuple[frozenset[str], frozenset[str]]:
    """The 39 helpers lead every script and are the same each time: analyse them once. Names
    are resolved over the union, which is what one namespace gives."""
    prefix = "\n".join(execution._HELPER_LINES)
    if not code.startswith(prefix):
        return _analysis(code)
    bound, read = _analysis(prefix)
    rest_bound, rest_read = _analysis(code[len(prefix) :])
    return bound | rest_bound, read | rest_read


def undefined_names(code: str, defined_elsewhere: frozenset[str] = frozenset()) -> set[str]:
    bound, read = _analyse_split(code)
    known = bound | set(dir(builtins)) | BRIDGE_GLOBALS | SCRIPT_GLOBALS | defined_elsewhere
    return set(read) - known


def _variants(op: RegisteredOperation) -> Iterator[tuple[str, Mapping[str, Any]]]:
    from flame_mcp.types import PHASE_ARG

    yield op.name, dict(op.sample_args)
    for phase in op.phases:
        yield f"{op.name}[{phase.name}]", {**op.sample_args, **phase.args, PHASE_ARG: phase.name}


def _findings(op: RegisteredOperation) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for label, args in _variants(op):
        assert op.to_python is not None
        script = execution.build_exec_code(op, args)
        op_src = op.to_python(args)
        verify_src = op.verify(args) if op.verify is not None else None
        script_names = _analyse_split(script)[0]
        sources = {"script": script, "op": op_src}
        if verify_src is not None:
            sources["verify"] = verify_src
        shared = set(script_names)
        for src in sources.values():
            shared |= _analyse_split(src)[0]
        for kind, src in sources.items():
            if kind == "script":
                missing = undefined_names(src)
            else:
                missing = undefined_names(src, frozenset(shared))
            if missing:
                found[f"{label}:{kind}"] = missing
    return found


def test_no_script_reads_a_name_that_nothing_defines() -> None:
    checked = 0
    findings: dict[str, set[str]] = {}
    for op in shared_registry().list_ops():
        if op.to_python is None:
            continue
        checked += 1
        findings |= _findings(op)
    assert checked > 50
    assert findings == {}


def test_the_check_finds_a_helper_that_reads_an_undefined_name() -> None:
    helper = (
        "def _good(x):\n"
        "    return [y for y in x if flame]\n"
        "def _bad(x):\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as exc:\n"
        "        return exc\n"
        "    return _missing_helper(x) + len(x)\n"
        "_result = _good([1])\n"
    )
    assert undefined_names(helper) == {"_missing_helper"}
    assert undefined_names("import os\nglobal_x = os.sep\nprint(global_x, undefined_top)") == {
        "undefined_top"
    }
    assert undefined_names("def f():\n    global late\n    late = 1\nprint(late)") == set()
