"""Prevent regression to runtime integration of legacy projects in the new core."""

import ast
from pathlib import Path


def test_new_core_never_imports_legacy_backend_or_external_project_runtime():
    root = Path(__file__).parents[1] / "src/collection_context"
    forbidden = {"app", "dym", "dYm", "aione", "All_IN_ONE", "bailian_cli"}
    for file in root.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not forbidden.intersection(names), f"Legacy runtime import in {file.name}"


def test_direct_model_adapter_has_no_cli_subprocess_or_environment_loading():
    file = Path(__file__).parents[1] / "src/collection_context/processing/models.py"
    tree = ast.parse(file.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert not {"subprocess", "os", "dotenv"}.intersection(imports)
