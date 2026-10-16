"""The static scan `eval.run` applies to its `code` on the server.

The scan runs before anything is generated or sent.

Accident prevention, not a sandbox. `eval.run` runs any Python with the permissions of the
user running Flame: `open(...).write`, `shutil.rmtree`, `urllib`, `socket`, an aliased
`os.system`, `getattr(os, "sys" + "tem")` and `while True: pass` all pass this scan. The real
gate is FLAME_MCP_ENABLE_EVAL, which is off by default. The scan refuses the shapes that are
plainly against what the caller declared or against this server's own rules, and nothing
else. It reads names, not behaviour:
`getattr(obj, name_built_at_run_time)()`, an alias (`f = flame; f.projects`), a helper that
hides the call, a Python method that happens to share a name with a Flame one, all pass or
misfire. Being caught here means "you wrote something the declaration forbids", being
missed here means nothing.

Rules (each violation carries its rule name, the line and a message):

  syntax          the code does not parse (or is longer than MAX_CODE_CHARS)
  flame.projects  `flame.projects` used in any way but `flame.projects.<attribute>`: iterating,
                  indexing, len(), passing or aliasing it. Iterating and indexing it are reported to
                  crash Flame in another tool's notes (not reproduced here); the rest is
                  refused to keep the guard simple.
  shortcut        `execute_shortcut`: it can send Undo, which pops the LIFO stack -- the
                  journal's assumptions and the user's own last operation alike.
  thread          `schedule_idle_event`, `threading`, `_thread`, `QTimer`: every Flame call goes
                  through the bridge's main-thread relay only; calling `schedule_idle_event`
                  from the bridge's handler thread deadlocked Flame (see the threading contract in
                  flame_mcp_bridge/impl.py).
  dynamic-exec    `exec` / `eval` / `compile` / `__import__`, `importlib`, `subprocess`,
                  `os.system` and the other process-starting `os` calls: a second, unscanned
                  layer of code, or an external process.
  process         `flame.exit` (it quits Flame) is refused whatever the effect; so is
                  `flame.execute_command` (it may run an external process). The latter is
                  refused from its name alone; what it runs has not been checked.
  write-in-read   (effect='read' only) a call or reference to a Flame writing method -- see
                  WRITE_NAMES / WRITE_PREFIXES -- or an assignment to an object's attribute,
                  or setattr / delattr.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Final

MAX_CODE_CHARS: Final[int] = 50_000
MAX_VIOLATIONS: Final[int] = 20

RULE_SYNTAX: Final[str] = "syntax"
RULE_PROJECTS: Final[str] = "flame.projects"
RULE_SHORTCUT: Final[str] = "shortcut"
RULE_THREAD: Final[str] = "thread"
RULE_DYNAMIC: Final[str] = "dynamic-exec"
RULE_PROCESS: Final[str] = "process"
RULE_WRITE: Final[str] = "write-in-read"

WRITE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "set_value",
        "delete",
        "clear",
        "duplicate",
        "save_setup",
        "load_setup",
        "append_setup",
        "replace_setup",
        "render",
        "import_clip",
        "import_clips",
        "copy",
        "move",
        "connect_nodes",
        "disconnect_node",
        "organize",
        "iterate",
        "slip",
        "overwrite",
        "insert",
        "cut",
        "commit",
        "reformat",
        "cache_media",
        "export",
        "set_current_tab",
    }
)
"""Method / function names a read-only eval may not call or refer to. Not exhaustive on
purpose. Some are also Python container methods (`copy`, `clear`, `insert`): a false alarm
there is cheaper than letting `batch.clear()` through; write `list(x)` / `dict(x)` instead."""

WRITE_PREFIXES: Final[tuple[str, ...]] = (
    "create_",
    "clear_",
    "duplicate_",
    "set_",
    "save_",
    "load_",
    "append_",
    "replace_",
    "import_clip",
    "trim_",
    "smart_replace",
    "flush_",
    "export_",
)

THREAD_NAMES: Final[frozenset[str]] = frozenset(
    {"schedule_idle_event", "threading", "_thread", "QTimer"}
)
DYNAMIC_BUILTINS: Final[frozenset[str]] = frozenset({"exec", "eval", "compile", "__import__"})
DYNAMIC_MODULES: Final[frozenset[str]] = frozenset({"importlib", "subprocess"})
OS_PROCESS_NAMES: Final[frozenset[str]] = frozenset(
    {"system", "popen", "fork", "forkpty", "startfile", "posix_spawn", "posix_spawnp"}
)
OS_PROCESS_PREFIXES: Final[tuple[str, ...]] = ("exec", "spawn")
PROCESS_NAMES: Final[frozenset[str]] = frozenset({"exit", "execute_command"})


@dataclass(frozen=True)
class Violation:
    rule: str
    line: int
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"rule": self.rule, "line": self.line, "message": self.message}

    def __str__(self) -> str:
        return f"line {self.line}: [{self.rule}] {self.message}"


def _is_write_name(name: str) -> bool:
    return name in WRITE_NAMES or name.startswith(WRITE_PREFIXES)


def _is_os_process_name(name: str) -> bool:
    return name in OS_PROCESS_NAMES or name.startswith(OS_PROCESS_PREFIXES)


def _root(module: str | None) -> str:
    return (module or "").split(".")[0]


class _Scan:
    def __init__(self, tree: ast.AST, effect: str) -> None:
        self.effect = effect
        self.found: list[Violation] = []
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        for node in ast.walk(tree):
            self._visit(node)

    def _add(self, rule: str, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        violation = Violation(rule, int(line), message)
        if violation not in self.found:
            self.found.append(violation)

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Attribute):
            self._attribute(node)
        elif isinstance(node, ast.Name):
            self._name(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                self._import(node, _root(alias.name), alias.name)
        elif isinstance(node, ast.ImportFrom):
            self._import_from(node)
        elif isinstance(node, ast.Call):
            self._call(node)
        elif self.effect == "read" and isinstance(
            node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)
        ):
            self._attribute_assignment(node)

    def _attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        base = node.value
        if isinstance(base, ast.Name) and base.id == "flame" and attr == "projects":
            parent = self.parents.get(node)
            if not (isinstance(parent, ast.Attribute) and parent.value is node):
                self._add(
                    RULE_PROJECTS,
                    node,
                    "flame.projects may only be used as flame.projects.<attribute>; iterating, "
                    "indexing, len() or aliasing it is refused (iterating and indexing are "
                    "reported to crash Flame in another tool's notes; len() and aliasing are "
                    "refused only to keep the guard simple)",
                )
        if attr == "execute_shortcut":
            self._shortcut(node)
        if isinstance(base, ast.Name) and base.id == "flame" and attr in PROCESS_NAMES:
            self._process(node, attr)
        if attr in THREAD_NAMES:
            self._thread(node, attr)
        if isinstance(base, ast.Name) and base.id == "os" and _is_os_process_name(attr):
            self._add(
                RULE_DYNAMIC, node, f"os.{attr} starts an external process, which is not allowed"
            )
        if self.effect == "read" and _is_write_name(attr):
            self._write(node, attr)

    def _name(self, node: ast.Name) -> None:
        name = node.id
        if name == "execute_shortcut":
            self._shortcut(node)
        if name in THREAD_NAMES:
            self._thread(node, name)
        if name in DYNAMIC_BUILTINS or name in DYNAMIC_MODULES:
            self._add(
                RULE_DYNAMIC,
                node,
                f"'{name}' runs code or a process this scan cannot see, which is not allowed",
            )
        if self.effect == "read" and name in ("setattr", "delattr"):
            self._add(
                RULE_WRITE,
                node,
                f"{name} can write to a Flame object; not allowed with effect='read'",
            )

    def _import(self, node: ast.AST, root: str, shown: str) -> None:
        if root in THREAD_NAMES:
            self._thread(node, shown)
        if root in DYNAMIC_MODULES:
            self._add(RULE_DYNAMIC, node, f"importing '{shown}' is not allowed")

    def _import_from(self, node: ast.ImportFrom) -> None:
        root = _root(node.module)
        self._import(node, root, node.module or "")
        for alias in node.names:
            name = alias.name
            if name == "execute_shortcut":
                self._shortcut(node)
            if root == "flame" and name in PROCESS_NAMES:
                self._process(node, name)
            if name in THREAD_NAMES:
                self._thread(node, name)
            if name in DYNAMIC_BUILTINS or name in DYNAMIC_MODULES:
                self._add(RULE_DYNAMIC, node, f"importing '{name}' is not allowed")
            if root == "os" and _is_os_process_name(name):
                self._add(RULE_DYNAMIC, node, f"importing os.{name} is not allowed")
            if self.effect == "read" and _is_write_name(name):
                self._write(node, name)

    def _call(self, node: ast.Call) -> None:
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2):
            return
        name = node.args[1]
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            return
        value = name.value
        if value == "execute_shortcut":
            self._shortcut(node)
        owner = node.args[0]
        if isinstance(owner, ast.Name) and owner.id == "flame" and value in PROCESS_NAMES:
            self._process(node, value)
        if value in THREAD_NAMES:
            self._thread(node, value)
        if self.effect == "read" and _is_write_name(value):
            self._write(node, value)

    def _attribute_assignment(self, node: ast.AST) -> None:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                    self._add(
                        RULE_WRITE,
                        node,
                        f"assigning to the attribute '{sub.attr}' of an object may write to "
                        "Flame; not allowed with effect='read'",
                    )

    def _shortcut(self, node: ast.AST) -> None:
        self._add(
            RULE_SHORTCUT,
            node,
            "execute_shortcut is not allowed: it can send Undo, which removes the last "
            "operation on Flame's stack whoever made it",
        )

    def _process(self, node: ast.AST, name: str) -> None:
        reason = (
            "it quits Flame" if name == "exit" else "it may run an external process (provisional)"
        )
        self._add(RULE_PROCESS, node, f"flame.{name} is not allowed whatever the effect: {reason}")

    def _thread(self, node: ast.AST, name: str) -> None:
        self._add(
            RULE_THREAD,
            node,
            f"'{name}' is not allowed: Flame API calls run on the bridge's main-thread path only",
        )

    def _write(self, node: ast.AST, name: str) -> None:
        self._add(
            RULE_WRITE,
            node,
            f"'{name}' is a writing method / function name and effect='read' was declared "
            "(declare effect='destructive' with a target, or, if this is a plain Python "
            "container method, use list(x) / dict(x) instead)",
        )


def check_eval_code(code: str, effect: str) -> list[Violation]:
    """Returns every violation of the rules in the module docstring, in source order.

    At most MAX_VIOLATIONS are returned. An empty list means the scan found nothing,
    which proves nothing.
    """
    if len(code) > MAX_CODE_CHARS:
        return [
            Violation(RULE_SYNTAX, 0, f"code is {len(code)} characters, more than {MAX_CODE_CHARS}")
        ]
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        line = getattr(exc, "lineno", None)
        return [Violation(RULE_SYNTAX, int(line) if line else 0, f"cannot parse: {exc}")]
    found = _Scan(tree, effect).found
    found.sort(key=lambda v: v.line)
    return found[:MAX_VIOLATIONS]
