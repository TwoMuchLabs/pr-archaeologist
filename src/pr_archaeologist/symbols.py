"""Find functions and classes in source text, with line ranges.

Python uses the real `ast`. Everything else uses declaration regexes plus brace
matching, which is approximate by design: good enough to say "these lines belong to
`charge()`", not a compiler front end.
"""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional

CODE_EXTENSIONS = {
    "py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "java", "kt", "kts",
    "c", "h", "cc", "cpp", "hpp", "cxx", "cs", "php", "rb", "swift", "scala", "m", "mm",
    "sh", "bash", "pl", "pm", "ex", "exs", "lua", "sql", "dart", "vue", "svelte",
}

KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "do", "try", "new", "throw",
    "sizeof", "typeof", "function", "await", "yield", "case", "using", "lock", "foreach",
    "elif", "with", "assert", "delete", "defer", "go", "select", "match", "when",
}

_JS = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?"
        r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)")),
    ("method", re.compile(
        r"^\s+(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*"
        r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{;]+)?\{\s*$")),
]
_GO = [
    ("function", re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
    ("type", re.compile(r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
]
_RUST = [
    ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
]
_CLIKE = [
    ("class", re.compile(r"^\s*(?:[\w@]+\s+)*(?:class|interface|enum|struct|object|trait)\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:[\w@]+\s+)*(?:fun|func|function)\s+(?:\w+\.)?([A-Za-z_]\w*)\s*[(<]")),
    ("function", re.compile(r"^\s*(?:[\w<>\[\],.*&:~?]+\s+)+[*&]?\s*([A-Za-z_~][\w:]*)\s*\([^;]*$")),
]
_SHELL = [("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"))]
_INDENT = {
    "rb": [("function", re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)")),
           ("class", re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)"))],
    "py": [("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")),
           ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)"))],
    "ex": [("function", re.compile(r"^\s*defp?\s+([a-z_]\w*[?!]?)")),
           ("class", re.compile(r"^\s*defmodule\s+([A-Z][\w.]*)"))],
    "lua": [("function", re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)"))],
}
_INDENT["pyi"] = _INDENT["py"]
_INDENT["exs"] = _INDENT["ex"]

_BRACE = {}
for _ext in ("js", "jsx", "mjs", "cjs", "ts", "tsx", "vue", "svelte"):
    _BRACE[_ext] = _JS
_BRACE["go"] = _GO
_BRACE["rs"] = _RUST
for _ext in ("java", "kt", "kts", "c", "h", "cc", "cpp", "hpp", "cxx", "cs", "php", "swift", "scala", "m", "mm", "dart"):
    _BRACE[_ext] = _CLIKE
for _ext in ("sh", "bash"):
    _BRACE[_ext] = _SHELL


def extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_code(path: str) -> bool:
    return extension(path) in CODE_EXTENSIONS


def _python_ast(text: str) -> Optional[List[Dict]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out: List[Dict] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append({
                    "name": name,
                    "kind": "class" if isinstance(child, ast.ClassDef) else ("method" if prefix else "function"),
                    "start": start,
                    "end": getattr(child, "end_lineno", None) or child.lineno,
                })
                if isinstance(child, ast.ClassDef):
                    visit(child, name + ".")

    visit(tree, "")
    return out


def _indent_based(lines: List[str], patterns) -> List[Dict]:
    out: List[Dict] = []
    for i, line in enumerate(lines):
        for kind, rx in patterns:
            m = rx.match(line)
            if not m:
                continue
            indent = len(line) - len(line.lstrip())
            end = len(lines)
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent and not nxt.lstrip().startswith(("end", ")", "]")):
                    end = j
                    break
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            out.append({"name": m.group(1), "kind": kind, "start": i + 1, "end": end})
            break
    return out


def _brace_end(lines: List[str], start: int) -> Optional[int]:
    """Index of the line closing the block opened at/after `start`; None if it never opens."""
    depth, opened = 0, False
    for j in range(start, min(len(lines), start + 3000)):
        line = lines[j]
        if not opened and j > start + 3:
            return None
        if not opened and line.rstrip().endswith(";") and "{" not in line:
            return None  # a declaration, not a definition
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return j
    return None


def _brace_based(lines: List[str], patterns) -> List[Dict]:
    out: List[Dict] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*", "#")):
            continue
        for kind, rx in patterns:
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name.rsplit(":", 1)[-1] in KEYWORDS:
                continue
            end = _brace_end(lines, i)
            if end is None:
                continue
            out.append({"name": name, "kind": kind, "start": i + 1, "end": end + 1})
            break
    return out


def extract(path: str, text: str) -> List[Dict]:
    """Return [{name, kind, start, end}] (1-based, inclusive) for one source file."""
    ext = extension(path)
    lines = text.splitlines()
    symbols: List[Dict] = []
    if ext in ("py", "pyi"):
        parsed = _python_ast(text)
        symbols = parsed if parsed is not None else _indent_based(lines, _INDENT["py"])
    elif ext in _INDENT:
        symbols = _indent_based(lines, _INDENT[ext])
    elif ext in _BRACE:
        symbols = _brace_based(lines, _BRACE[ext])
    # Qualify methods found inside a class by regex (ast already did this for Python).
    if ext not in ("py", "pyi"):
        classes = [s for s in symbols if s["kind"] in ("class", "type")]
        for sym in symbols:
            if sym["kind"] in ("class", "type"):
                continue
            owner = next((c for c in reversed(classes) if c["start"] < sym["start"] and sym["end"] <= c["end"]), None)
            if owner:
                sym["name"] = f"{owner['name']}.{sym['name']}"
                sym["kind"] = "method"
    return symbols[:400]
