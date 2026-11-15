"""Repair SQLModel's Python-value annotations at SQL expression boundaries.

Run with Python 3.14 from the repository root:
  python scripts/repair-sqlmodel-types.py /tmp/ty-diagnostics.log
The input is `ty check app --output-format concise` from backend/. Edits are
idempotent; mapped class fields and deprecated Session.execute calls are repaired.
col() preserves the exact InstrumentedAttribute object at runtime. This script
preserves model-field SQL metadata, expressing datetime annotations through
equivalent Columns; migration operations, query ordering and predicates stay intact.
"""

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def repair(log: Path) -> int:
    diagnostics = {}
    for line in log.read_text().splitlines():
        match = re.match(
            r"(?:backend/)?(app/[^:]+):(\d+):\d+: (?:error|warning)\[([^]]+)\]", line
        )
        if match:
            diagnostics.setdefault(match[1], []).append((int(match[2]), match[3]))
    models = {}
    for path in (ROOT / "backend/app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                fields = {
                    n.target.id
                    for n in node.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                }
                bases = {n.id for n in node.bases if isinstance(n, ast.Name)}
                table = any(
                    k.arg == "table"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value
                    for k in node.keywords
                )
                old_fields, old_bases, old_table = models.get(
                    node.name, (set(), set(), False)
                )
                models[node.name] = (
                    fields | old_fields,
                    bases | old_bases,
                    table or old_table,
                )

    def fields(name, seen=frozenset()):
        if name not in models or name in seen:
            return set()
        own, bases, _ = models[name]
        return own | set().union(*(fields(b, seen | {name}) for b in bases))

    mapped = {name: fields(name) for name, (_, _, table) in models.items() if table}
    changed = 0
    for relative, issues in diagnostics.items():
        path = ROOT / "backend" / relative
        source = path.read_text()
        tree = ast.parse(source)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line.encode()))
        raw = source.encode()
        edits = []
        need_col = need_session = False
        execute_lines = {line for line, kind in issues if kind == "deprecated"}
        aliases = [
            n.asname or n.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy.orm"
            for n in node.names
            if n.name == "Session"
        ]
        session_alias = aliases[0] if aliases else "SQLAlchemySession"

        def replace(node, value, edits=edits, offsets=offsets):
            edits.append(
                (
                    offsets[node.lineno - 1] + node.col_offset,
                    offsets[node.end_lineno - 1] + node.end_col_offset,
                    value.encode(),
                )
            )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if (
                isinstance(node.value, (ast.Name, ast.Attribute))
                and (
                    node.value.id
                    if isinstance(node.value, ast.Name)
                    else node.value.attr
                )
                in mapped
                and node.attr
                in mapped[
                    node.value.id
                    if isinstance(node.value, ast.Name)
                    else node.value.attr
                ]
                and not node.attr.startswith("_")
            ):
                parent = parents.get(node)
                if (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Name)
                    and parent.func.id in {"col", "cast", "typing_cast"}
                ):
                    continue
                replace(node, "col(" + ast.get_source_segment(source, node) + ")")
                need_col = True
            elif node.attr == "execute" and node.lineno in execute_lines:
                if (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in {"typing_cast", "cast"}
                ):
                    continue
                value = ast.get_source_segment(source, node.value)
                replace(node, f"typing_cast({session_alias}, {value}).execute")
                need_session = True
        for start, end, value in sorted(edits, reverse=True):
            raw = raw[:start] + value + raw[end:]
        result = raw.decode()
        imports = []
        if need_col and not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "sqlmodel"
            and any(a.name == "col" for a in n.names)
            for n in tree.body
        ):
            imports.append("from sqlmodel import col\n")
        if need_session:
            if not aliases:
                imports.append(
                    "from sqlalchemy.orm import Session as SQLAlchemySession\n"
                )
            if "from typing import cast as typing_cast" not in source:
                imports.append("from typing import cast as typing_cast\n")
        if imports:
            # Insert after the module docstring and future imports.
            after = 0
            for node in tree.body:
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    or isinstance(node, ast.ImportFrom)
                    and node.module == "__future__"
                ):
                    after = node.end_lineno
                else:
                    break
            rows = result.splitlines(keepends=True)
            rows[after:after] = imports
            result = "".join(rows)
        if result != source:
            path.write_text(result)
            changed += 1
    return changed


def repair_datetime_fields() -> int:
    """Make the existing timezone/default/nullability explicit in Column metadata.

    SQLModel accepts DateTime instances at runtime but annotates sa_type as a
    class. Column is the supported typed boundary for configured SQL types.
    """
    changed = 0
    for path in (ROOT / "backend/app/modules/ai_workbench").glob("*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        edits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(
                node.value, ast.Call
            ):
                continue
            call = node.value
            options = {kw.arg: kw.value for kw in call.keywords}
            sql_type = options.get("sa_type")
            if (
                not isinstance(sql_type, ast.Call)
                or not isinstance(sql_type.func, ast.Name)
                or sql_type.func.id != "DateTime"
            ):
                continue
            nullable = "None" in ast.unparse(node.annotation)
            parts = [
                ast.unparse(sql_type),
                "index=False",
                "unique=False",
                f"nullable={nullable}",
            ]
            if "default_factory" in options:
                parts.append("default=" + ast.unparse(options["default_factory"]))
            elif "default" in options and not (
                isinstance(options["default"], ast.Constant)
                and options["default"].value is None
            ):
                parts.append("default=" + ast.unparse(options["default"]))
            kw = next(k for k in call.keywords if k.arg == "sa_type")
            lines = source.splitlines(keepends=True)
            start = sum(len(s.encode()) for s in lines[: kw.lineno - 1]) + kw.col_offset
            end = (
                sum(len(s.encode()) for s in lines[: kw.end_lineno - 1])
                + kw.end_col_offset
            )
            edits.append(
                (start, end, ("sa_column=Column(" + ", ".join(parts) + ")").encode())
            )
        raw = source.encode()
        for start, end, replacement in sorted(edits, reverse=True):
            raw = raw[:start] + replacement + raw[end:]
        if edits:
            # These model modules already use Column for JSON/Time fields.
            assert any(
                isinstance(n, ast.ImportFrom)
                and n.module == "sqlalchemy"
                and any(a.name == "Column" for a in n.names)
                for n in tree.body
            ), path
            path.write_text(raw.decode())
            changed += 1
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diagnostics", type=Path)
    args = parser.parse_args()
    print(
        f"Updated {repair(args.diagnostics)} SQL files and {repair_datetime_fields()} datetime model files"
    )
