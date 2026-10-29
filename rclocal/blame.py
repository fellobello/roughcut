"""Who is wrong when a file keeps failing its tests, settled from facts where facts can settle it.

A small reviewer model guessed this badly: in the SplitTip run it blamed the contract in 7
cases and was right in 2. But most of what it guessed at is known for certain: which tests
failed, whether the file meets the contract's own examples (rounds adds them to every test
file), what the tests import, and what they expect for a call the contract has an example
of. `settle` goes through those in order and returns a verdict when one of them decides it;
only what's left goes to the model, and the model may blame the contract only by quoting
the line that can't work (`quoted`).
"""

import re

from .stacks import example_cases, literal_prefix

EXAMPLES_TESTS = ("the contract examples hold", "ContractExamples")  # the test stacks.example_tests adds
EXAMPLES_START = ("// The examples in the contract", "class ContractExamples")  # where that test begins in the file
WEB_FAILED = re.compile(r"^\s*✖\s+(.+?)(?:\s+\([\d.]+m?s\))?\s*$", re.M)
PY_FAILED = re.compile(r"^(?:FAIL|ERROR): (.+)$", re.M)
WEB_MISSING = re.compile(r"requested module '([^']+)' does not provide an export named '([^']+)'")
PY_MISSING = re.compile(r"cannot import name '([^']+)' from '([^']+)'")
LOAD_FAILED = re.compile(r"\.m?js\b|/|_FailedTest")  # the whole file failed to load, not one test
CRASH = re.compile(r"\b(TypeError|ReferenceError|RangeError|NameError|AttributeError|ZeroDivisionError|KeyError|IndexError)\b")


def failed_tests(output):
    """The names of the tests that failed, as the runner printed them (node's spec reporter or unittest)."""
    names = [n for n in WEB_FAILED.findall(output or "") if n != "failing tests:"]
    names += PY_FAILED.findall(output or "")
    return list(dict.fromkeys(names))


def is_examples_test(name):
    return any(x in name for x in EXAMPLES_TESTS)


def tester_part(test_code):
    """The tester's own tests: the file without the contract's examples added at the end."""
    cut = min((test_code.find(m) for m in EXAMPLES_START if m in test_code), default=-1)
    return test_code if cut < 0 else test_code[:cut]


def compact(text):
    return re.sub(r"\s+", "", text or "").replace("'", '"')


def same_value(a, b):
    a, b = compact(a), compact(b)
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def export_names(contract):
    return {e["name"].split(".")[0] for e in contract["exports"]}


def imported_names(stack, test_code, path):
    """Names the test file imports from the file under test, written the stack's way."""
    if stack.name == "web":
        groups = re.findall(rf"""import\s*\{{([^}}]*)\}}\s*from\s*['"]\.\./{re.escape(path)}['"]""", test_code)
    else:
        groups = re.findall(rf"^\s*from\s+{re.escape(stack.import_name(path))}\s+import\s+\(?([^)\n]*)\)?", test_code, re.M)
    return {n.split(" as ")[0].strip() for g in groups for n in g.split(",") if n.strip()}


def contradictions(contract, test_code):
    """Tests that expect something else than the contract's own example for the same call: [(call, expected, promised)]."""
    code = compact(tester_part(test_code))
    found = []
    for call, returns in example_cases(contract):
        needle = compact(call)
        start = 0
        while (at := code.find(needle, start)) >= 0:
            start = at + len(needle)
            if at and re.match(r"[\w$]", code[at - 1]):
                continue  # part of a longer name
            rest = code[start:]
            expected = literal_prefix(rest[1:]) if rest.startswith(",") else ""
            if expected and not same_value(expected, returns):
                found.append((call, expected, returns))
    return found


def clashing_examples(contract):
    """Calls the contract gives two different results for: [(call, one, other)]."""
    seen, clashes = {}, []
    for call, returns in example_cases(contract, limit=10):
        key = compact(call)
        if key in seen and not same_value(seen[key], returns):
            clashes.append((call, seen[key], returns))
        seen.setdefault(key, returns)
    return clashes


def settle(stack, contract, test_code, code, failure):
    """A verdict ({blame, reason}) when the facts decide it, or None to ask the reviewer."""
    path, names = contract["path"], export_names(contract)

    # 1. Errors that can only come from one side.
    if test_code and stack.syntax_error(test_code, contract["test_path"]):
        return verdict("test", "the test file isn't valid code")
    if code and stack.syntax_error(code, path):
        return verdict("code", f"{path} isn't valid code")
    for match in WEB_MISSING.finditer(failure or ""):
        module, name = match.groups()
        if module.endswith(path) and name not in names:
            return verdict("test", f"the tests import {name}, which the contract doesn't have")
        return verdict("code", f"{module} doesn't export {name}, which the contract promises")
    for match in PY_MISSING.finditer(failure or ""):
        name, module = match.groups()
        if module == stack.import_name(path) and name not in names:
            return verdict("test", f"the tests import {name}, which the contract doesn't have")
        return verdict("code", f"{module} doesn't define {name}, which the contract promises")
    extra = sorted(imported_names(stack, test_code, path) - names)
    if extra:
        return verdict("test", f"the tests import {', '.join(extra)}, which the contract doesn't have")

    # 2. The contract can't be right if it disagrees with itself.
    clashes = clashing_examples(contract)
    if clashes:
        call, one, other = clashes[0]
        return verdict("contract", f"its examples give two results for {call}: {one} and {other}")

    # 3. Tests that expect something else than the contract's example for the same call.
    wrong = contradictions(contract, test_code)
    if wrong:
        call, expected, promised = wrong[0]
        return verdict("test", f"a test expects {expected} for {call}, but the contract's example says {promised}")

    # 4. The contract's own examples, run as their own test.
    failed = failed_tests(failure)
    if not failed or not any(x in test_code for x in EXAMPLES_TESTS) or any(LOAD_FAILED.search(n) for n in failed):
        return None
    if any(is_examples_test(n) for n in failed):
        return verdict("code", f"{path} fails the contract's own examples")
    if CRASH.search(failure):
        return None  # something threw: the code on an input the examples don't cover, or the test itself
    return verdict("test", f"{path} meets every example in its contract; the failing tests ({', '.join(failed[:3])}) "
                           "ask for more than the contract promises")


def quoted(quote, brief):
    """Whether the reviewer's quote is really in the contract: what it must give to blame the contract."""
    quote = compact(quote).strip('"')
    return len(quote) >= 8 and quote.lower() in compact(brief).lower()


def verdict(blame, reason):
    return {"blame": blame, "reason": reason}
