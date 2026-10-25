"""Text functions for the ops that replace one value of a setup file (`segment.edit_effect_setup`,
`node.edit_setup`).

A setup file is text made of tags. The one thing these ops may do to it is replace the value of a
tag that occurs exactly once, written `<TAG>value</TAG>`: a tag with child elements, with
attributes, written self-closing or occurring more than once is refused, and so is a name or a
value that could change the structure of the file. Nothing here uses a regular expression's
replacement string, so a value such as `\\1` goes in as it is.

The source of this module is also sent to Flame (`setup_edit_prelude`): the edit runs inside
Flame, between the save and the load of the setup. It is ASCII, needs nothing but `re` and
`Decimal`, and takes only plain values (str, int, float, bool) so that the copy that runs there
behaves like this one.
"""

import re
from decimal import Decimal

SETUP_MAX_VALUES = 20
SETUP_MAX_VALUE_LENGTH = 256
WORKDIR_MAX_AGE_DAYS = 7
MAX_DIFFERENCES = 20

_TAG_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MARKUP = '<>&"'
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_NUMBER_MAX_LENGTH = 64


def tag_name_problem(tag: object) -> "str | None":
    if not isinstance(tag, str):
        return "the tag name must be a string"
    if _TAG_NAME.fullmatch(tag) is None:
        return "the tag name " + ascii(tag) + " does not match [A-Za-z_][A-Za-z0-9_]*"
    return None


def _text_of(value: object) -> "str | None":
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value - value == 0 else None
    if isinstance(value, str):
        return value
    return None


def value_problem(value: object) -> "str | None":
    """Why `value` may not be written into a setup, or None. A value is a string, an integer, a
    finite float or a bool, and its text holds only printable ASCII without `<`, `>`, `&` and `"`:
    no newline, carriage return, tab or other control character."""
    text = _text_of(value)
    if text is None:
        if isinstance(value, float):
            return "a float value must be finite"
        return (
            "a value must be a string, an integer, a float or a boolean, got "
            + type(value).__name__
        )
    if len(text) > SETUP_MAX_VALUE_LENGTH:
        return (
            "the value is "
            + str(len(text))
            + " characters, more than "
            + str(SETUP_MAX_VALUE_LENGTH)
        )
    for ch in text:
        if ch in _MARKUP:
            return (
                "the value "
                + ascii(text)
                + " holds "
                + ascii(ch)
                + ", which could change the structure of the setup"
            )
        if ord(ch) < 32 or ord(ch) == 127:
            return (
                "the value "
                + ascii(text)
                + " holds a control character (a newline, carriage return or tab is one)"
            )
        if ord(ch) > 126:
            return "the value " + ascii(text) + " holds a character that is not ASCII"
    return None


def format_value(value: object) -> str:
    """The text written for `value`: a bool as `True` / `False`, a number as Python prints it."""
    problem = value_problem(value)
    text = _text_of(value)
    if problem is not None or text is None:
        raise ValueError(problem)
    return text


def check_values(values: object) -> list[str]:
    """Every reason `values` ({tag: value}) may not be used, as one line each; [] when fine."""
    if not isinstance(values, dict) or not 1 <= len(values) <= SETUP_MAX_VALUES:
        return ["values must be an object of 1 to " + str(SETUP_MAX_VALUES) + " entries"]
    problems = []
    for tag, value in values.items():
        problem = tag_name_problem(tag)
        if problem is None:
            problem = value_problem(value)
        if problem is not None:
            problems.append("tag " + ascii(tag) + ": " + problem)
    return problems


def count_tags(text: str, tags: list[str]) -> dict[str, int]:
    """How many times each tag is opened in `text`, in any form (`<TAG>`, `<TAG a="1">`,
    `<TAG/>`)."""
    return {tag: len(re.findall("<" + re.escape(tag) + r"(?=[\s/>])", text)) for tag in tags}


def _scalars(text: str, tag: str) -> list[re.Match[str]]:
    name = re.escape(tag)
    return list(re.finditer("<" + name + ">([^<]*)</" + name + ">", text))


def scalar_value(text: str, tag: str) -> "str | None":
    """The value of `tag` when it occurs exactly once as `<TAG>value</TAG>`, else None."""
    found = _scalars(text, tag)
    if len(found) == 1 and count_tags(text, [tag])[tag] == 1:
        return found[0].group(1)
    return None


def tag_problems(text: str, tags: list[str]) -> dict[str, str]:
    """{tag: why its value cannot be replaced} for every tag that does not occur exactly once as
    `<TAG>value</TAG>`."""
    problems = {}
    counts = count_tags(text, tags)
    for tag in tags:
        if counts[tag] == 0:
            problems[tag] = "the tag is not in the setup"
        elif counts[tag] > 1:
            problems[tag] = (
                "the tag occurs "
                + str(counts[tag])
                + " times (only a tag that occurs once can be replaced)"
            )
        elif len(_scalars(text, tag)) != 1:
            problems[tag] = (
                "the tag is not a plain <TAG>value</TAG>: it has attributes, child elements "
                "or is written self-closing"
            )
    return problems


def replace_tags(text: str, values: dict[str, object]) -> str:
    """`text` with the value of each tag in `values` replaced. Raises ValueError, changing
    nothing, when a name or a value is not allowed or a tag is not one plain scalar."""
    problems = check_values(values)
    if problems:
        raise ValueError("; ".join(problems))
    bad = tag_problems(text, list(values))
    if bad:
        raise ValueError("; ".join(tag + ": " + bad[tag] for tag in bad))
    for tag, value in values.items():
        found = _scalars(text, tag)[0]
        text = text[: found.start(1)] + format_value(value) + text[found.end(1) :]
    return text


def values_match(requested: object, found: "str | None") -> bool:
    """The value read back (`found`, the text of the tag) is the one asked for. A bool matches
    its `True` / `False`; an int or float matches the same number written another way (`0.5` and
    `0.500000`), compared as decimal numbers and only when `found` is a plain decimal number
    (digits with an optional sign, point and exponent: no space, underscore, inf or nan);
    anything else matches only the same text."""
    if found is None:
        return False
    if isinstance(requested, bool):
        return found == ("True" if requested else "False")
    if isinstance(requested, (int, float)):
        text = _text_of(requested)
        if text is None:
            return False
        if text == found:
            return True
        if len(found) > _NUMBER_MAX_LENGTH or _NUMBER.fullmatch(found) is None:
            return False
        return Decimal(text) == Decimal(found)
    return found == requested


def scalar_differences(
    edited: str, readback: str, ignore: list[str]
) -> dict[str, dict[str, list[str]]]:
    """{tag: {'edited': [values], 'readback': [values]}} for the `<TAG>value</TAG>` tags whose
    values differ between two texts, leaving out the tags in `ignore`; at most MAX_DIFFERENCES."""
    pattern = r"<([A-Za-z_][A-Za-z0-9_]*)>([^<]*)</\1>"
    first: dict[str, list[str]] = {}
    second: dict[str, list[str]] = {}
    for table, text in ((first, edited), (second, readback)):
        for found in re.finditer(pattern, text):
            table.setdefault(found.group(1), []).append(found.group(2))
    differences = {}
    for tag in list(first) + [t for t in second if t not in first]:
        if tag in ignore or first.get(tag, []) == second.get(tag, []):
            continue
        differences[tag] = {"edited": first.get(tag, []), "readback": second.get(tag, [])}
        if len(differences) >= MAX_DIFFERENCES:
            break
    return differences
