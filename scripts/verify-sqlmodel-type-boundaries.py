"""Verify SQLModel typing repair runtime boundaries without app configuration.

Run with the repository's Python environment:
  python scripts/verify-sqlmodel-type-boundaries.py ae923c8
Checks mapped-column identity, configured datetime metadata against the supplied
Git revision, and that the historical migration has only a typing-only change.
"""

import argparse
import ast
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import new_class

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel, col

ROOT = Path(__file__).resolve().parents[1]


class ColumnProbe(SQLModel, table=True):
    id: int = Field(primary_key=True)
    value: str


def evaluate_field(node, namespace):
    """Interpret only the existing Field/Column datetime declarations, not module code."""
    if isinstance(node, ast.Name):
        return namespace[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        return getattr(evaluate_field(node.value, namespace), node.attr)
    if isinstance(node, ast.Lambda):
        return lambda: evaluate_field(node.body, namespace)
    if isinstance(node, ast.Call):
        factory = evaluate_field(node.func, namespace)
        return factory(
            *(evaluate_field(arg, namespace) for arg in node.args),
            **{kw.arg: evaluate_field(kw.value, namespace) for kw in node.keywords},
        )
    raise ValueError(f"unsupported datetime declaration: {ast.dump(node)}")


def metadata(source, identities):
    result = {}
    for definition in ast.walk(ast.parse(source)):
        if not isinstance(definition, ast.ClassDef):
            continue
        for field in definition.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(
                field.value, ast.Call
            ):
                continue
            expression = ast.unparse(field.value)
            if "DateTime(" not in expression:
                continue
            options = {kw.arg: kw.value for kw in field.value.keywords}
            column_call = options.get("sa_column")
            if "default_factory" in options and isinstance(column_call, ast.Call):
                column_options = {kw.arg: kw.value for kw in column_call.keywords}
                assert ast.dump(options["default_factory"]) == ast.dump(
                    column_options["default"]
                ), "Column default differs from the model factory"
            identities.append(None)
            name = f"DateProbe{len(identities)}"
            namespace = {
                "SQLModel": SQLModel,
                "Field": Field,
                "SQLField": Field,
                "Column": Column,
                "DateTime": DateTime,
                "datetime": datetime,
                "UTC": UTC,
                "_now": lambda: datetime.now(UTC),
            }
            field_value = evaluate_field(field.value, namespace)
            attributes = {
                "__annotations__": {
                    "id": int,
                    "value": datetime | None
                    if "None" in ast.unparse(field.annotation)
                    else datetime,
                },
                "id": Field(primary_key=True),
                "value": field_value,
            }
            probe = new_class(
                name,
                (SQLModel,),
                {"table": True},
                lambda ns, attributes=attributes: ns.update(attributes),
            )
            column = probe.__table__.c.value
            default = (
                None
                if column.default is None
                else (
                    "callable"
                    if column.default.is_callable
                    else repr(column.default.arg)
                )
            )
            result[(definition.name, field.target.id)] = (
                str(column.type),
                column.type.timezone,
                column.nullable,
                default,
                column.primary_key,
                column.index,
                column.unique,
            )
    return result


class EraseTyping(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return None if node.module == "typing" else node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "cast":
            return node.args[1]
        return node


def main(base):
    assert col(ColumnProbe.value) is ColumnProbe.value
    identities = []
    checked = 0
    for path in (ROOT / "backend/app/modules/ai_workbench").glob("*.py"):
        original = subprocess.check_output(
            ["git", "show", f"{base}:{path.relative_to(ROOT)}"], cwd=ROOT, text=True
        )
        before = metadata(original, identities)
        after = metadata(path.read_text(), identities)
        assert before == after, path
        checked += len(before)
    migration = "backend/app/alembic/versions/0115_copy_current_source.py"
    original = subprocess.check_output(
        ["git", "show", f"{base}:{migration}"], cwd=ROOT, text=True
    )
    before = EraseTyping().visit(ast.parse(original))
    after = EraseTyping().visit(ast.parse((ROOT / migration).read_text()))
    assert ast.dump(before) == ast.dump(after), "historical migration behavior changed"
    print(
        f"col identity preserved; {checked} datetime fields unchanged; migration AST unchanged"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="Git revision before the typing repair")
    main(parser.parse_args().base)
