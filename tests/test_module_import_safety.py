"""Things that break on import in production but not on this machine.

Every module here is imported by a process that starts with it, so an error at import
time is not a bug in a feature -- it is the whole bot failing to boot, and no amount of
green feature tests says otherwise. The suite runs on whatever Python this developer has;
the Dockerfile pins the one production runs. Anything whose BEHAVIOUR differs between
those two versions is invisible to every other test in the repo, and this file is where
those differences are pinned instead.
"""

import ast
import builtins
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Modules in the repo root are the ones an entrypoint imports. Tests, the virtualenv and
# scratch scripts are not shipped and are deliberately out of scope.
MODULES = sorted(
    path for path in ROOT.glob("*.py")
    if not path.name.startswith("_")
)


def _future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _module_level_names(tree: ast.Module) -> set[str]:
    """Everything a module-level annotation could legally refer to.

    Deliberately generous -- it walks the whole tree rather than just the top level, so a
    name bound inside a conditional or a try/except import still counts. The question here
    is "could this name exist at all", and a false negative is worth far more than the
    precision: this test must never fail on working code.
    """
    names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in ast.walk(node):
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class ModuleLevelAnnotationTests(unittest.TestCase):
    """A module-level annotation must name something that exists.

    Shipped broken once: `PHOENIX_TOTEM_NOTICE: Final = (...)` in pets.py, where `Final`
    was never imported. The suite was green because this machine runs Python 3.14, which
    defers annotation evaluation (PEP 649) and therefore never looks the name up.
    Production runs 3.12, which evaluates it as the module is read -- so `import pets`
    raised NameError and every process that starts with it died on boot.

    Checked by reading rather than by importing, so the result does not depend on which
    Python is running the tests. That independence is the entire point: a check that only
    fires on the old version is a check that this machine can never run.
    """

    def test_every_module_level_annotation_names_something_that_exists(self):
        for path in MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if _future_annotations(tree):
                # `from __future__ import annotations` makes every annotation a string on
                # every version, so nothing here can be evaluated and nothing can fail.
                continue
            known = _module_level_names(tree)
            for node in tree.body:
                if not isinstance(node, ast.AnnAssign) or node.annotation is None:
                    continue
                used = {
                    inner.id for inner in ast.walk(node.annotation)
                    if isinstance(inner, ast.Name)
                }
                for name in sorted(used - known):
                    self.fail(
                        f"{path.name}:{node.lineno} annotates with «{name}», which the "
                        f"module never imports or defines. This machine's Python defers "
                        f"annotations and will not notice; production's evaluates them "
                        f"and will refuse to import the module at all."
                    )


class DeployedRuntimeTests(unittest.TestCase):
    """What production actually runs, so a version-sensitive check has a number to use."""

    def test_the_dockerfile_still_pins_a_python_this_file_knows_about(self):
        """If the pin moves, the reasoning above has to be re-read rather than assumed.

        The annotation check does not depend on the version, but the REASON it exists does:
        it is only interesting while production is older than 3.14. A silent upgrade past
        that should make somebody look at this file, not quietly make it pointless.
        """
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(r"FROM python:(\d+)\.(\d+)", dockerfile)
        self.assertIsNotNone(match, "Dockerfile no longer starts from a pinned python")
        major, minor = int(match.group(1)), int(match.group(2))
        self.assertEqual(major, 3)
        self.assertGreaterEqual(minor, 12, "older than the code is written against")
        self.assertLess(
            minor, 14,
            "production has reached the Python where annotations are deferred -- "
            "re-read ModuleLevelAnnotationTests before relaxing anything here",
        )


if __name__ == "__main__":
    unittest.main()
