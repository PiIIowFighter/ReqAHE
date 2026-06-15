import ast
from collections import Counter
from pathlib import Path


def test_no_duplicate_function_defs_in_component_schema() -> None:
    path = Path("src/reqahe/harness/component_schema.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    duplicated = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicated, f"duplicate function definitions: {duplicated}"
