"""Clean Architecture guard: dependencies point inward only."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

FORBIDDEN = {
    "domain": ("app.application", "app.infrastructure", "app.api", "app.cli", "app.bootstrap", "sqlalchemy", "fastapi", "httpx"),
    "application": ("app.infrastructure", "app.api", "app.cli", "app.bootstrap", "sqlalchemy", "fastapi", "httpx"),
}


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_import_outer_layers(layer):
    bad = []
    for file in (APP / layer).rglob("*.py"):
        for mod in imports(file):
            if mod.startswith(FORBIDDEN[layer]):
                bad.append(f"{file.relative_to(APP)} imports {mod}")
    assert not bad, "\n".join(bad)
