"""Read or change one value in config.yaml without disturbing anything else.

    python3 deploy/configedit.py config.yaml get web.token
    python3 deploy/configedit.py config.yaml set web.host 127.0.0.1

Used by install.sh. A YAML library would round-trip the file and throw away
every comment in it, and the comments are most of the documentation. This
edits the one line in place instead, then re-reads the whole file to prove
the result still parses and now holds exactly the value asked for.

Only existing scalar keys can be set; a key that is missing is an error, not
something silently appended in the wrong place.

    python3 deploy/configedit.py config.yaml apply changes.json

applies a release's settings: a list of {"set": KEY, "value": V} - change it,
including one field of a one-line mapping such as
scheduling.windows.monday.end - and {"ensure": KEY, "value": V} - add it if
missing, and never overwrite a value you already have. Each change is
checked by re-reading the whole file, and the file is written once, only if
every change that could apply did.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

KEY = re.compile(r"^(?P<indent>\s*)(?P<key>\"[^\"]*\"|'[^']*'|[A-Za-z0-9_\-]+):"
                 r"(?P<space>\s*)(?P<rest>.*)$")
PLAIN_KEY = re.compile(r"[A-Za-z0-9_\-]+")


def _key(match) -> str:
    key = match["key"]
    return key[1:-1] if key[:1] in "\"'" else key


def _split_value(rest: str) -> tuple[str, str]:
    """('"value"', '  # comment') from the text after "key:"."""
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        index = 1
        while index < len(rest):
            if rest[index] == "\\" and quote == '"':
                index += 2
                continue
            if rest[index] == quote:
                if quote == "'" and rest[index + 1:index + 2] == "'":
                    index += 2
                    continue
                break
            index += 1
        return rest[:index + 1], rest[index + 1:]
    match = re.search(r"\s+#", rest)
    if match:
        return rest[:match.start()], rest[match.start():]
    return rest, ""


def _locate(lines: list[str], dotted: str) -> int:
    target = dotted.split(".")
    stack: list[tuple[int, str]] = []
    for number, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        match = KEY.match(line)
        if not match:
            continue
        indent = len(match["indent"])
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, _key(match)))
        if [key for _, key in stack] == target:
            return number
    raise KeyError(f"{dotted} is not in this config - add it by hand first")


def lookup(data, dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def set_value(path: Path, dotted: str, value: str) -> None:
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    number = _locate(lines, dotted)
    line = lines[number]
    ending = "\n" if line.endswith("\n") else ""
    match = KEY.match(line.rstrip("\n"))
    _, comment = _split_value(match["rest"])
    if not match["rest"].strip() or match["rest"].lstrip().startswith("#"):
        # "key:" with nothing after it is a mapping, not a value.
        if lookup(yaml.safe_load(text), dotted) is not None:
            raise ValueError(f"{dotted} holds a section, not a value")
    # A number stays a number: port: 8080, not port: "8080".
    current = lookup(yaml.safe_load(text), dotted)
    written = (value if isinstance(current, int) and value.isdigit()
               else json.dumps(value))
    lines[number] = (f"{match['indent']}{match['key']}: "
                     f"{written}{comment}{ending}")
    updated = "".join(lines)

    parsed = yaml.safe_load(updated)
    if str(lookup(parsed, dotted)) != value:
        raise ValueError(f"refusing to write: {dotted} would not read back "
                         f"as the value given")
    path.write_text(updated)


# ---------------------------------------------------------------- apply

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _flow(mapping: dict) -> str:
    return "{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in mapping.items()) + "}"


def _written_key(key: str) -> str:
    return key if PLAIN_KEY.fullmatch(key) else json.dumps(key)


def _set_in_text(text: str, dotted: str, value) -> str:
    """Set an existing value - a scalar line, or one field of a one-line
    {a: b} mapping. Returns the new text."""
    lines = text.splitlines(keepends=True)
    parts = dotted.split(".")
    try:
        number, field = _locate(lines, dotted), None
    except KeyError:
        number, field = _locate(lines, ".".join(parts[:-1])), parts[-1]
    line = lines[number]
    ending = "\n" if line.endswith("\n") else ""
    match = KEY.match(line.rstrip("\n"))
    current, comment = _split_value(match["rest"])
    head = f"{match['indent']}{match['key']}:{match['space'] or ' '}"
    if field is not None:
        mapping = yaml.safe_load(current) if current.strip().startswith("{") else None
        if not isinstance(mapping, dict) or field not in mapping:
            raise KeyError(f"{dotted} is not in this config")
        mapping[field] = value
        written = _flow(mapping)
    elif current.strip().startswith("{") and isinstance(value, dict):
        written = _flow(value)
    elif not current.strip():
        raise ValueError(f"{dotted} holds a section, not a value")
    else:
        written = _scalar(value)
    lines[number] = f"{head}{written}{comment}{ending}"
    return "".join(lines)


def _add_in_text(text: str, dotted: str, value) -> str:
    """Add a missing key under its section, creating the section if that is
    missing too. The new line goes after the section's last line, indented
    like its other entries."""
    lines = text.splitlines(keepends=True)
    parts = dotted.split(".")
    parent = ".".join(parts[:-1])
    if not parent:
        raise KeyError(f"{dotted}: only keys inside a section can be added")
    try:
        number = _locate(lines, parent)
    except KeyError:
        text = _add_in_text(text, parent, {})
        lines = text.splitlines(keepends=True)
        number = _locate(lines, parent)
    match = KEY.match(lines[number].rstrip("\n"))
    current, comment = _split_value(match["rest"])
    if current.strip() == "{}":
        # "places: {}" - an empty one-line mapping becomes a block.
        lines[number] = (f"{match['indent']}{match['key']}:{comment}"
                         + ("\n" if lines[number].endswith("\n") else ""))
    elif current.strip():
        raise ValueError(f"{parent} is a value, not a section")
    indent = len(match["indent"])
    child_indent, last = None, number
    for index in range(number + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        this = len(lines[index]) - len(lines[index].lstrip())
        if this <= indent:
            break
        if child_indent is None:
            child_indent = this
        last = index
    pad = " " * (child_indent if child_indent is not None else indent + 2)
    if isinstance(value, dict) and not value:
        new = f"{pad}{_written_key(parts[-1])}:\n"
    elif isinstance(value, dict):
        new = f"{pad}{_written_key(parts[-1])}: {_flow(value)}\n"
    else:
        new = f"{pad}{_written_key(parts[-1])}: {_scalar(value)}\n"
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, new)
    return "".join(lines)


def apply_changes(path: Path, changes: list[dict]) -> list[str]:
    """Apply a release's settings. Returns one line per change, saying what
    happened. Raises - and writes nothing - if the result would not parse
    or would not hold what was asked."""
    text = path.read_text()
    report = []
    for change in changes:
        key = change.get("set") or change.get("ensure")
        value = change["value"]
        have = lookup(yaml.safe_load(text), key)
        if "ensure" in change and have is not None:
            report.append(f"kept      {key} = {have!r} (yours)")
            continue
        if have == value:
            report.append(f"unchanged {key} = {value!r}")
            continue
        try:
            if have is None:
                updated = _add_in_text(text, key, value)
            else:
                updated = _set_in_text(text, key, value)
        except (KeyError, ValueError) as exc:
            report.append(f"skipped   {key}: {exc.args[0]}")
            continue
        if lookup(yaml.safe_load(updated), key) != value:
            raise ValueError(f"refusing to write: {key} would not read back "
                             f"as {value!r}")
        text = updated
        report.append(f"changed   {key}: {have!r} -> {value!r}"
                      if have is not None else f"added     {key} = {value!r}")
    yaml.safe_load(text)              # the whole file still parses
    path.write_text(text)
    return report


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "apply":
        try:
            changes = json.loads(Path(argv[2]).read_text())
            for line in apply_changes(Path(argv[0]), changes):
                print(line)
        except (KeyError, ValueError) as exc:
            print(f"configedit: {exc.args[0]}", file=sys.stderr)
            return 1
        return 0
    if len(argv) < 3 or argv[1] not in ("get", "set") \
            or (argv[1] == "set" and len(argv) != 4):
        print("usage: configedit.py CONFIG get KEY | set KEY VALUE | "
              "apply CHANGES.json", file=sys.stderr)
        return 2
    path = Path(argv[0])
    if argv[1] == "get":
        value = lookup(yaml.safe_load(path.read_text()), argv[2])
        if value is None:
            return 1
        print(value)
        return 0
    try:
        set_value(path, argv[2], argv[3])
    except (KeyError, ValueError) as exc:
        print(f"configedit: {exc.args[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
