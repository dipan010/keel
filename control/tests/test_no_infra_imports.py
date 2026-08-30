"""domain/ must stay pure.

It is the testable core and the part that outlives any particular substrate --
keeping this boundary clean is exactly what makes a second backend (section 4.1
of the architecture doc) cheap rather than a rewrite.
"""

import ast
import pathlib

FORBIDDEN = {"kubernetes", "kubernetes_asyncio", "psycopg", "psycopg_pool", "httpx", "fastapi"}


def test_domain_imports_no_infrastructure():
    root = pathlib.Path(__file__).resolve().parents[1] / "domain"
    offences = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in FORBIDDEN:
                    offences.append(f"{path.name}: {name}")
    assert not offences, f"domain/ must stay infrastructure-free: {offences}"
