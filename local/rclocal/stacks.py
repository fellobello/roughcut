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
from pathlib import Path, PurePosixPath

KIT = Path(__file__).with_name("kit.css")

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


KIT_CLASSES = None
BASE_SELECTOR = re.compile(r"(^|[\s,>+~(])(html|body|:root|\*)(?=$|[\s,.:#\[>+~)])")


def kit_classes():
    global KIT_CLASSES
    if KIT_CLASSES is None:
        KIT_CLASSES = set(re.findall(r"\.([a-z][a-z0-9-]*)", re.sub(r"/\*.*?\*/", "", KIT.read_text(), flags=re.S)))
    return KIT_CLASSES


def _blocks(css):
    """Split CSS into top-level (prelude, body) pairs; body is None for statements like @import."""
    out, i, n = [], 0, len(css)
    while i < n:
        brace = css.find("{", i)
        semi = css.find(";", i)
        if brace == -1:
            break
        if semi != -1 and semi < brace and css[i:semi].strip().startswith("@"):
            out.append((css[i:semi].strip(), None))
            i = semi + 1
            continue
        depth, j = 1, brace + 1
        while j < n and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append((css[i:brace].strip(), css[brace + 1:j - 1]))
        i = j
    return out


def filter_styles(css):
    """Drop rules that restyle the kit or the page base, so styles.css can only add to the design.

    Returns (css, dropped selectors). Small models restyle .card and body with variables that
    don't exist, which breaks a finished design; this keeps their app-specific rules only.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    kit = kit_classes()
    kept, dropped = [], []
    for prelude, body in _blocks(css):
        if body is None:
            continue  # no @import or other statements
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            inner, gone = filter_styles(body)
            dropped += gone
            if inner.strip():
                kept.append(f"{prelude} {{\n{inner}}}\n")
            continue
        if prelude.startswith("@"):
            kept.append(f"{prelude} {{{body}}}\n")  # @keyframes and friends
            continue
        touches_kit = any(c in kit for c in re.findall(r"\.([a-z][a-z0-9-]*)", prelude))
        if touches_kit or BASE_SELECTOR.search(prelude):
            dropped.append(prelude)
            continue
        kept.append(f"{prelude} {{{body.rstrip()}\n}}\n")
    return "\n".join(kept), dropped


def example_cases(contract, limit=2):
    """The contract's examples that can be checked as written: (call, returns) pairs.

    A call must be one expression that calls the export by name; anything else (a class,
    several statements) is left out rather than guessed at.
    """
    cases = []
    for export in contract["exports"]:
        name = export["name"].split(".")[0]
        for example in export.get("examples", [])[:limit]:
            call, returns = example["call"].strip().rstrip(";"), example["returns"].strip().rstrip(";")
            if not returns or "\n" in call + returns or ";" in call + returns:
                continue
            if re.match(rf"{re.escape(name)}\s*\(", call):
                cases.append((call, returns))
    return cases


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

    test_header = "import unittest"
    example_call, example_returns = "to_celsius(212)", "100.0"

    def example_tests(self, contract):
        """The contract's examples as a test case, added to the end of the tester's file."""
        cases = example_cases(contract)
        if not cases:
            return ""
        lines = [
            f"import {self.import_name(contract['path'])} as _contract",
            "",
            "",
            "class ContractExamples(unittest.TestCase):",
            '    """The examples in the contract, exactly as written there."""',
            "",
            "    def test_the_contract_examples_hold(self):",
        ]
        lines += [f"        self.assertEqual(_contract.{call}, {returns})" for call, returns in cases]
        return "\n".join(lines) + "\n"

    def repair_test(self, test_code):
        """Add the standard import small models often forget. Free, unlike another model call."""
        if "unittest." in test_code and not re.search(r"^\s*import unittest\b", test_code, re.M):
            test_code = "import unittest\n" + test_code
        return test_code

    def test_problem(self, test_code):
        return "" if "unittest" in test_code else "the test file must use unittest (import unittest)"

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

    test_header = "import { test } from 'node:test';\nimport assert from 'node:assert/strict';"
    example_call, example_returns = "tipFor(100, 15)", "15"

    def example_tests(self, contract):
        """The contract's examples as one test, added to the end of the tester's file."""
        cases = example_cases(contract)
        if not cases:
            return ""
        lines = [
            f"import * as __contract from '../{contract['path']}';",
            "",
            "// The examples in the contract, exactly as written there.",
            "test('the contract examples hold', () => {",
        ]
        lines += [f"  assert.deepStrictEqual(__contract.{call}, {returns});" for call, returns in cases]
        return "\n".join(lines + ["});"]) + "\n"

    def repair_test(self, test_code):
        """Add the node:test and node:assert imports small models often forget. Free, unlike another model call."""
        lines = []
        if not re.search(r"""from\s*['"]node:test['"]""", test_code):
            used = [n for n in ("test", "describe", "it", "before", "after", "beforeEach", "afterEach")
                    if re.search(rf"(^|[^.\w]){n}\s*\(", test_code)]
            lines.append(f"import {{ {', '.join(used or ['test'])} }} from 'node:test';")
        if re.search(r"\bassert\b", test_code) and not re.search(r"""from\s*['"]node:assert(/strict)?['"]""", test_code):
            lines.append("import assert from 'node:assert/strict';")
        return "\n".join(lines + [test_code]) if lines else test_code

    def test_problem(self, test_code):
        if not re.search(r"""import\s*\{[^}]*\}\s*from\s*['"]node:test['"]""", test_code):
            return "the test file must start with: import { test } from 'node:test';"
        if not re.search(r"""from\s*['"]node:assert(/strict)?['"]""", test_code):
            return "the test file must import assert from 'node:assert'"
        return ""

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
        """Files every web project has: package.json marks the .js files as ES modules, and
        kit.css is the design system the designer builds the page from."""
        return {
            "package.json": json.dumps({"private": True, "type": "module"}, indent=2) + "\n",
            "kit.css": KIT.read_text(encoding="utf-8"),
        }


STACKS = {"web": Web(), "python": Python()}


def stack_for(name):
    if name not in STACKS:
        raise ValueError(f"unknown stack {name!r}; choose from {', '.join(STACKS)}")
    return STACKS[name]
