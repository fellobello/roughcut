"""What kind of app a project builds: file rules, test commands and the code guard.

A stack decides which paths files may have, where their tests go, how a file is
checked before anything runs it, and which commands run its tests. Two stacks:

- web (default): a browser app. Logic in app/*.js ES modules, tested with node:test;
  one index.html and styles.css made by the designer. Opens in any browser.
- python: standard-library Python in app/, tested with unittest.

The guard is the first wall of the box: before model-written code is ever run, it's
scanned for things the app never needs and that could harm the machine (starting
processes, network, deleting files, reading the environment). Code that trips it is
sent back to its author with the reason and is never executed.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import PurePosixPath

# Python: modules and calls an app or its tests never need.
PY_BANNED_MODULES = {
    "subprocess", "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "telnetlib", "requests",
    "ctypes", "multiprocessing", "shutil", "signal", "pty", "webbrowser", "importlib", "asyncio.subprocess",
}
PY_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
PY_BANNED_OS = {
    "system", "popen", "remove", "unlink", "rmdir", "removedirs", "rename", "replace", "kill", "fork",
    "chmod", "chown", "environ", "getenv", "putenv", "execv", "execve", "execl", "execlp", "spawnv", "spawnl",
}

# JavaScript: patterns an app or its tests never need.
JS_BANNED = [
    (re.compile(r"\brequire\s*\("), "require() (use ES module imports)"),
    (re.compile(r"""from\s+['"](?!node:test|node:assert|\.{1,2}/)[^'"]+['"]"""), "imports from outside the project"),
    (re.compile(r"""\bimport\s*\("""), "dynamic import()"),
    (re.compile(r"""node:(?!test\b|assert\b)\w+"""), "Node system modules"),
    (re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b"), "network access"),
    (re.compile(r"\beval\s*\(|\bnew\s+Function\b"), "eval or new Function"),
    (re.compile(r"\bprocess\.(env|exit|kill)|\bchild_process\b"), "process access"),
    (re.compile(r"""<script[^>]+src=['"]https?:|<link[^>]+href=['"]https?:|@import\s+url\(\s*['"]?https?:"""),
     "external scripts or stylesheets"),
]


def py_guard(code):
    """Problems that keep this Python code from being run, or []."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # the syntax check reports it
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            if name in PY_BANNED_MODULES or name.split(".")[0] in PY_BANNED_MODULES:
                problems.append(f"imports {name}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in PY_BANNED_CALLS:
            problems.append(f"calls {node.func.id}()")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in PY_BANNED_OS:
            problems.append(f"uses os.{node.attr}")
    return sorted(set(problems))


def js_guard(code):
    return sorted({why for pattern, why in JS_BANNED if pattern.search(code)})


class Stack:
    name = ""
    lang = ""  # the code fence language
    path_re = None
    example = ""

    def check_path(self, path):
        return bool(self.path_re.match(path)) and not path.startswith("tests/")

    def guard(self, code):
        raise NotImplementedError


class Python(Stack):
    name, lang, example = "python", "python", "app/geo.py"
    path_re = re.compile(r"^app/([a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py$")
    test_glob = "tests/test_*.py"
    conventions = [
        "Python 3.9 or later, standard library only. No pip packages.",
        "Code lives in the app/ package. Import other project files as `from app.geo import distance`.",
        "Tests use unittest, live in tests/, and import the code the same way.",
        "Type hints on every public function, and a one-line docstring.",
        "Keep each file under about 200 lines.",
    ]
    export_hint = "'def name(arg: type) -> type' for a function, 'class Name' for a class, or 'NAME: type' for a constant"

    def test_path(self, path):
        return "tests/test_" + "_".join(PurePosixPath(path).with_suffix("").parts) + ".py"

    def import_name(self, path):
        return ".".join(PurePosixPath(path).with_suffix("").parts)

    def import_hint(self, path):
        return f"import as {self.import_name(path)}"

    def test_imports_it(self, test_code, path):
        return self.import_name(path) in test_code

    def defined_names(self, code):
        names = set()
        for node in ast.parse(code).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    def syntax_error(self, code, path):
        try:
            compile(code, path, "exec")
        except SyntaxError as e:
            return f"SyntaxError on line {e.lineno}: {e.msg}"
        return ""

    def guard(self, code):
        return py_guard(code)

    def test_command(self, python, test_file):
        return [python, "-s", "-m", "unittest", test_file[:-3].replace("/", ".")]

    def scaffold(self, project):
        return {}


class Web(Stack):
    name, lang, example = "web", "javascript", "app/tip.js"
    path_re = re.compile(r"^app/([a-z_][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.js$")
    test_glob = "tests/*.test.mjs"
    conventions = [
        "A browser app with no build step and no packages: plain JavaScript ES modules.",
        "Logic lives in app/*.js modules with named exports and no DOM access, so node can test them.",
        "Modules import each other with relative paths, like `import { split } from './bill.js'`.",
        "Tests use node:test and node:assert in tests/*.test.mjs and import from '../app/<file>.js'.",
        "index.html and styles.css are the only UI files; the page script imports from ./app/*.js.",
        "No network calls, no external fonts, scripts or images. Keep each file under about 200 lines.",
    ]
    export_hint = (
        "'function name(arg) -> returns' for a function, 'class Name' for a class, or 'const NAME' for a constant"
    )

    def test_path(self, path):
        return "tests/" + "_".join(PurePosixPath(path).with_suffix("").parts) + ".test.mjs"

    def import_name(self, path):
        return path

    def import_hint(self, path):
        return f"tests import it from '../{path}', the page from './{path}'"

    def test_imports_it(self, test_code, path):
        return f"../{path}" in test_code

    def defined_names(self, code):
        names = set(re.findall(r"export\s+(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)", code))
        for group in re.findall(r"export\s*\{([^}]*)\}", code):
            for part in group.split(","):
                part = part.strip()
                if part:
                    names.add(part.split(" as ")[-1].strip())
        return names

    def syntax_error(self, code, path):
        try:
            result = subprocess.run(
                ["node", "--input-type=module", "--check"], input=code, capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"couldn't check the syntax: {e}"
        if result.returncode != 0:
            lines = [l for l in result.stderr.splitlines() if l.strip()]
            return "SyntaxError: " + " | ".join(lines[:4])
        return ""

    def guard(self, code):
        return js_guard(code)

    def test_command(self, python, test_file):
        return ["node", "--test", "--test-reporter=spec", test_file]

    def scaffold(self, project):
        """Files every web project has: package.json marks the .js files as ES modules."""
        return {"package.json": json.dumps({"private": True, "type": "module"}, indent=2) + "\n"}


STACKS = {"web": Web(), "python": Python()}


def stack_for(name):
    if name not in STACKS:
        raise ValueError(f"unknown stack {name!r}; choose from {', '.join(STACKS)}")
    return STACKS[name]
