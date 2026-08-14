"""The float ban, enforced mechanically (creed #4).

Walks the AST of every module under the money-path namespaces and fails
the build if it finds float literals, float() calls, round() calls, bare
`/` division, or a float type annotation. The ban is a build gate, not a
convention — conventions erode, gates don't.

Usage: python tools/check_money_paths.py
Exit 0 clean; exit 1 with file:line findings otherwise.
"""

from __future__ import annotations

import ast
from pathlib import Path

MONEY_PATH_ROOTS = (
    Path("src/noctix_engine/money.py"),
    Path("src/noctix_engine/domain.py"),
    Path("src/noctix_engine/slice.py"),
    Path("src/noctix_engine/ingest"),
    Path("src/noctix_engine/verify"),
    Path("src/noctix_engine/act"),
    Path("src/noctix_engine/report"),
)


def money_path_files() -> list[Path]:
    files: list[Path] = []
    for root in MONEY_PATH_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def violations_in(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        match node:
            case ast.Constant(value=float()):
                found.append((node.lineno, "float literal — use Decimal via Money"))
            case ast.Call(func=ast.Name(id="float")):
                found.append((node.lineno, "float() call"))
            case ast.Call(func=ast.Name(id="round")):
                found.append((node.lineno, "round() call — quantize via Money policy"))
            case ast.BinOp(op=ast.Div()):
                found.append(
                    (node.lineno, "bare / division — integer or Decimal math only")
                )
            case ast.Name(id="float", ctx=ast.Load()) if not isinstance(node, ast.Call):
                found.append((node.lineno, "float type reference"))
            case _:
                pass
    return found


def main() -> int:
    all_clean = True
    for path in money_path_files():
        for line, message in violations_in(path):
            print(f"{path}:{line}: {message}")
            all_clean = False
    if all_clean:
        print(f"money paths clean: {len(money_path_files())} file(s) checked")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
