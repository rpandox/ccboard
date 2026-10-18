"""Issue #293: every named timeout in app/ is either lifted for tests or exempt with a reason, so a short production limit around a fake binary
cannot turn a stalled test machine into "unknown" / "did not answer". The tables live in tests/conftest.py next to the autouse fixture.

A new `*_TIMEOUT` constant in app/ fails this file until it is added to RAISED_TIMEOUTS (the fixture lifts it for every test) or to
EXEMPT_TIMEOUTS with the reason it needs no lifting."""
import ast
import importlib
import pathlib
import re


from tests.conftest import (EXEMPT_TIMEOUTS, LITERAL_TIMEOUT_EXEMPT, RAISED_TIMEOUTS, TEST_TIMEOUT, production_timeout)

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
NAME = re.compile(r"(^|_)TIMEOUT$")
# The calls that run a subprocess (or wrap one): a numeric literal `timeout=` in one of them is a limit no test can lift.
SUBPROCESS_CALLS = {"run", "run_group", "check_output", "check_call", "call", "communicate", "Popen", "_run", "_git", "_restic",
                    "_restic_unlocking", "_gh", "_gh_json", "_invoke", "ls_remote"}
SHORT = TEST_TIMEOUT            # a literal at or above this needs no lifting: no stall of a test machine is that long


def _modules():
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(ROOT)
        yield ".".join(rel.with_suffix("").parts), rel.as_posix(), ast.parse(path.read_text(), filename=str(path))


def _timeout_constants():
    """(module, NAME) of every module-level `NAME = ...` in app/ whose name is TIMEOUT or ends in _TIMEOUT."""
    found = set()
    for module, _rel, tree in _modules():
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            found.update((module, t.id) for t in targets if isinstance(t, ast.Name) and NAME.search(t.id))
    return found


def _short_literals(tree):
    """(line, callee, value) of each subprocess call with `timeout=<number under SHORT>`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            callee = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            for kw in node.keywords:
                v = kw.value
                if (kw.arg == "timeout" and callee in SUBPROCESS_CALLS and isinstance(v, ast.Constant)
                        and isinstance(v.value, (int, float)) and not isinstance(v.value, bool) and v.value < SHORT):
                    yield node.lineno, callee, v.value


def _calls_a_subprocess(fn):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in SUBPROCESS_CALLS:
                return True
    return False


def _frozen_defaults(tree, raised_names):
    """(line, function, text) of each `def f(..., timeout=<constant lifted for tests in this module>)`, or `timeout=<short number>` in a function that
    runs a subprocess: a default is bound when the module is imported, so the fixture would never reach it. The default must be None, with the
    constant read inside the function."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults)) + [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for arg, default in pairs:
                if arg.arg != "timeout":
                    continue
                if isinstance(default, ast.Name) and default.id in raised_names:
                    yield node.lineno, node.name, default.id
                elif (isinstance(default, ast.Constant) and isinstance(default.value, (int, float)) and default.value < SHORT
                      and _calls_a_subprocess(node)):
                    yield node.lineno, node.name, repr(default.value)


def test_every_timeout_constant_in_app_is_lifted_for_tests_or_exempt_with_a_reason():
    raised = {(m, n) for m, names in RAISED_TIMEOUTS.items() for n in names}
    exempt = set(EXEMPT_TIMEOUTS)
    unknown = sorted(_timeout_constants() - raised - exempt)
    assert not unknown, ("a timeout constant in app/ that tests do not lift and no one exempted: add it to RAISED_TIMEOUTS in tests/conftest.py "
                         "(a subprocess a test fakes) or to EXEMPT_TIMEOUTS with the reason: " + ", ".join(f"{m}.{n}" for m, n in unknown))


def test_the_tables_name_constants_that_exist_and_do_not_overlap():
    existing = _timeout_constants()
    raised = {(m, n) for m, names in RAISED_TIMEOUTS.items() for n in names}
    exempt = set(EXEMPT_TIMEOUTS)
    assert not raised & exempt, f"both lifted and exempt: {sorted(raised & exempt)}"
    # RAISED may also hold a limit that is not named *_TIMEOUT (GH_WAIT, CHECK_TIMEOUT is), so only the exempt table must match the scan
    assert not exempt - existing, f"exempt but gone from app/ (stale entry): {sorted(exempt - existing)}"
    for module, name in sorted(raised):
        assert isinstance(getattr(importlib.import_module(module), name), (int, float)), f"{module}.{name} is not a number"
    assert all(reason.strip() for reason in EXEMPT_TIMEOUTS.values()) and all(reason.strip() for reason in LITERAL_TIMEOUT_EXEMPT.values())


def test_the_fixture_lifts_every_listed_timeout_and_never_lowers_one():
    for module, names in RAISED_TIMEOUTS.items():
        mod = importlib.import_module(module)
        for name in names:
            assert getattr(mod, name) == TEST_TIMEOUT, f"{module}.{name} was not lifted for this test"
            shipped = production_timeout(module, name)
            assert shipped <= TEST_TIMEOUT, f"{module}.{name} ships {shipped} s: lifting it to {TEST_TIMEOUT} s would shorten it"


def test_no_short_literal_timeout_in_a_subprocess_call_outside_the_listed_modules():
    offenders, used = [], set()
    for _module, rel, tree in _modules():
        found = list(_short_literals(tree))
        if found and rel in LITERAL_TIMEOUT_EXEMPT:
            used.add(rel)
        elif found:
            offenders += [f"{rel}:{line} {callee}(timeout={value})" for line, callee, value in found]
    assert not offenders, ("a literal timeout under 30 s cannot be lifted for tests: give it a module-level *_TIMEOUT constant and add that to "
                           "RAISED_TIMEOUTS (or list the module in LITERAL_TIMEOUT_EXEMPT with a reason): " + "; ".join(offenders))
    assert used == set(LITERAL_TIMEOUT_EXEMPT), f"listed in LITERAL_TIMEOUT_EXEMPT but no short literal left (stale): {sorted(set(LITERAL_TIMEOUT_EXEMPT) - used)}"


def test_no_timeout_default_that_the_fixture_could_not_reach():
    offenders = []
    for module, rel, tree in _modules():
        found = _frozen_defaults(tree, set(RAISED_TIMEOUTS.get(module, ())))
        # a short number as a default is allowed in the modules listed for short literals (a real git on a temp directory); a lifted constant never is
        offenders += [f"{rel}:{line} def {fn}(timeout={text})" for line, fn, text in found if not (rel in LITERAL_TIMEOUT_EXEMPT and text[0].isdigit())]
    assert not offenders, ("a default `timeout=` is bound at import, so tests cannot lift it: default to None and read the constant in the body: "
                           + "; ".join(offenders))


def test_the_guard_notices_each_kind_of_new_timeout():
    """The scans themselves: a constant, a literal and a frozen default are found in a made-up module."""
    tree = ast.parse("import subprocess\nNEW_TIMEOUT = 2.0\nTIMEOUT = 1\nOTHER = 3\n"
                     "def f(timeout=NEW_TIMEOUT):\n    return subprocess.run(['x'], timeout=5)\n"
                     "def g(timeout=7):\n    return subprocess.run(['x'], timeout=NEW_TIMEOUT)\n"
                     "def h(timeout=None):\n    return subprocess.run(['x'], timeout=300)\n")
    consts = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if NAME.search(t.id)}
    assert consts == {"NEW_TIMEOUT", "TIMEOUT"}
    assert [(c, v) for _l, c, v in _short_literals(tree)] == [("run", 5)]
    assert [(fn, text) for _l, fn, text in _frozen_defaults(tree, {"NEW_TIMEOUT"})] == [("f", "NEW_TIMEOUT"), ("g", "7")]
