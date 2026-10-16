"""Tests for the static scan of `eval.run`'s code (eval_check.py).

It is not a safety proof; these tests pin what it DOES stop, what it deliberately lets through, and
that a read-only call with ordinary Python does not trip it."""

from __future__ import annotations

import pytest

from flame_mcp import eval_check
from flame_mcp.eval_check import check_eval_code


def rules(code: str, effect: str = "read") -> list[str]:
    return [v.rule for v in check_eval_code(code, effect)]


@pytest.mark.parametrize(
    "code",
    [
        "_result = [_v(n.name) for n in target.nodes]",
        "p = flame.projects.current_project\n_result = _v(p.name)",
        "ws = flame.projects.current_project.current_workspace\n_result = len(list(ws.libraries))",
        "_result = {}\n_result['a'] = 1\n_result['b'] = [x.name for x in []]",
        "import json, re\n_result = json.dumps(re.compile('a').pattern)",
        "_result = ' '.join(['a', 'b']).replace('a', 'c')",
        "_result = _v(target.name).upper()",
        "x = 1\nx += 2\n_result = x",
    ],
)
def test_ordinary_read_code_passes(code: str) -> None:
    assert check_eval_code(code, "read") == []


def test_write_methods_are_fine_when_the_call_is_declared_destructive() -> None:
    code = "n = target.create_node('Mux')\nn.pos_x.set_value(3)\ntarget.clear()\nnode.name = 'x'"
    assert check_eval_code(code, "destructive") == []


def test_syntax_error_and_size() -> None:
    [v] = check_eval_code("def (:", "read")
    assert v.rule == "syntax" and v.line == 1
    [big] = check_eval_code("x = 1\n" * eval_check.MAX_CODE_CHARS, "read")
    assert big.rule == "syntax" and "characters" in big.message
    assert rules("a\x00b") == ["syntax"]


@pytest.mark.parametrize(
    "code",
    [
        "for p in flame.projects:\n    pass",
        "_result = [p for p in flame.projects]",
        "_result = flame.projects[0]",
        "_result = len(flame.projects)",
        "_result = list(flame.projects)",
        "ps = flame.projects\n_result = 1",
        "_result = next(iter(flame.projects))",
    ],
)
@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_flame_projects_is_only_allowed_as_a_base_for_an_attribute(code: str, effect: str) -> None:
    assert rules(code, effect) == ["flame.projects"]


@pytest.mark.parametrize(
    ("code", "rule"),
    [
        ("flame.execute_shortcut('Undo')", "shortcut"),
        ("f = flame.execute_shortcut", "shortcut"),
        ("from flame import execute_shortcut", "shortcut"),
        ("getattr(flame, 'execute_shortcut')('Undo')", "shortcut"),
        ("flame.schedule_idle_event(lambda: 1)", "thread"),
        ("import threading", "thread"),
        ("import threading as t", "thread"),
        ("from threading import Thread", "thread"),
        ("import _thread", "thread"),
        ("from PySide6.QtCore import QTimer", "thread"),
        ("QtCore.QTimer.singleShot(0, f)", "thread"),
        ("threading.Thread(target=f).start()", "thread"),
    ],
)
@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_shortcut_and_thread_names_are_refused(code: str, rule: str, effect: str) -> None:
    assert rule in rules(code, effect)


@pytest.mark.parametrize(
    "code",
    [
        "exec('x = 1')",
        "eval('1')",
        "compile('1', 'f', 'eval')",
        "__import__('os')",
        "import importlib",
        "from importlib import import_module",
        "import subprocess",
        "subprocess.run(['ls'])",
        "import os\nos.system('ls')",
        "os.popen('ls')",
        "os.execv('/bin/ls', [])",
        "os.spawnl(0, 'x')",
        "from os import system",
    ],
)
@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_dynamic_execution_and_processes_are_refused(code: str, effect: str) -> None:
    assert "dynamic-exec" in rules(code, effect)


def test_re_compile_and_os_path_are_not_dynamic_execution() -> None:
    assert check_eval_code("import os, re\nre.compile('a')\nos.path.join('a')", "read") == []


@pytest.mark.parametrize(
    "code",
    [
        "flame.exit()",
        "import flame\nflame.exit",
        "f = flame.exit",
        "from flame import exit",
        "getattr(flame, 'exit')()",
        "flame.execute_command('ls')",
        "from flame import execute_command",
        "getattr(flame, 'execute_command')('ls')",
    ],
)
@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_flame_exit_and_execute_command_are_refused_whatever_the_effect(
    code: str, effect: str
) -> None:
    assert "process" in rules(code, effect)


def test_a_process_violation_names_the_line_and_the_name() -> None:
    [v] = check_eval_code("x = 1\nflame.exit()", "destructive")
    assert v.rule == "process" and v.line == 2 and "flame.exit" in v.message


@pytest.mark.parametrize(
    "code",
    [
        "import sys\nsys.exit(1)",
        "_result = obj.exit_mode",
        "obj.exit()",
        "_result = flame.exit_code",
        "_result = flame.execute_command_name",
        "from flame import exit_mode",
        "getattr(obj, 'exit')()",
    ],
)
def test_other_exit_names_are_not_process_violations(code: str) -> None:
    assert "process" not in rules(code, "destructive")


def test_set_current_tab_is_a_write_in_read_only() -> None:
    assert "set_current_tab" in eval_check.WRITE_NAMES
    assert rules("flame.set_current_tab('Batch')", "read") == ["write-in-read"]
    assert check_eval_code("flame.set_current_tab('Batch')", "destructive") == []
    assert check_eval_code("_result = flame.get_current_tab()", "read") == []


@pytest.mark.parametrize(
    "code",
    [
        "x.set_value(1)",
        "target.create_node('Mux')",
        "target.create_reel('r')",
        "flame.delete(x)",
        "x.delete()",
        "target.clear()",
        "target.clear_setup()",
        "x.duplicate()",
        "x.duplicate_something()",
        "target.save_setup('p')",
        "target.load_setup('p')",
        "target.append_setup('p')",
        "target.replace_setup('p')",
        "target.render()",
        "target.import_clip('p')",
        "target.import_clips(['p'])",
        "flame.media_panel.copy(a, b)",
        "flame.media_panel.move(a, b)",
        "target.connect_nodes(a, 'Default', b, 'Default')",
        "target.disconnect_node(a)",
        "target.organize()",
        "target.iterate()",
        "seg.trim_head(1)",
        "seg.slip(1)",
        "seg.smart_replace_media(x)",
        "seq.overwrite(a)",
        "seq.insert(a)",
        "seq.cut(1)",
        "lib.commit()",
        "clip.reformat(width=1)",
        "clip.cache_media()",
        "clip.flush_cache_media()",
        "clip.export()",
        "clip.export_something()",
        "f = target.create_node",
        "getattr(target, 'create_node')('Mux')",
        "node.name = 'x'",
        "node.pos_x += 1",
        "del node.attr",
        "setattr(node, 'a', 1)",
        "delattr(node, 'a')",
        "target.set_context(1)",
    ],
)
def test_writing_names_are_refused_when_the_effect_is_read(code: str) -> None:
    assert "write-in-read" in rules(code, "read")


def test_a_write_in_read_is_reported_with_its_line_and_a_workaround() -> None:
    [v] = check_eval_code("x = 1\n\nl.copy()", "read")
    assert v.line == 3 and v.rule == "write-in-read" and "list(x)" in v.message


def test_violations_are_capped_and_sorted_by_line() -> None:
    code = "\n".join(f"a{i}.set_value({i})" for i in range(50))
    found = check_eval_code(code, "read")
    assert len(found) == eval_check.MAX_VIOLATIONS
    assert [v.line for v in found] == sorted(v.line for v in found)


# What the scan does not catch is documented in its docstring.


@pytest.mark.parametrize(
    "code",
    [
        "name = 'set' + '_value'\ngetattr(x, name)(1)",
        "f = flame\nfor p in f.projects:\n    pass",
        "g = x.__getattribute__('delete')\ng()",
    ],
)
def test_dynamic_access_and_aliases_pass_the_scan(code: str) -> None:
    assert check_eval_code(code, "read") == []
