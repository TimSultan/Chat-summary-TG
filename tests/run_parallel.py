"""Run the unittest suite across every core.

    python tests/run_parallel.py                      # the whole suite
    python tests/run_parallel.py test_pets_web test_pets_combat
    python tests/run_parallel.py -j 4 tests.test_voting.StorageTests

Whole test classes are handed to a pool of worker processes, largest first, so a class's
setUpClass still runs once and the long classes start before the short ones fill in
around them. Each worker keeps its imports between classes. Output a test prints is held
back and shown only with its failure, the same as unittest's --buffer.

Plain ``python -m unittest`` still works and runs exactly the same tests; this only
changes how many run at once.
"""

import argparse
import multiprocessing
import os
import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def _paths() -> None:
    # Discovery imports test modules by bare name from tests/, and the modules themselves
    # import the app and `tests.async_case` from the repository root.
    for path in (str(ROOT), str(TESTS)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _by_class(tests) -> list[list[str]]:
    """The selected test ids, one list per class, largest class first."""
    groups: dict[tuple[str, str], list[str]] = {}
    for test in tests:
        groups.setdefault((type(test).__module__, type(test).__qualname__), []).append(test.id())
    return sorted(groups.values(), key=len, reverse=True)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class _Collected(unittest.TestResult):
    """A result that can cross a process boundary: tracebacks are already strings."""

    def __init__(self):
        super().__init__()
        self.buffer = True
        self.slow: list[tuple[float, str]] = []
        self._started = 0.0

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.slow.append((time.perf_counter() - self._started, test.id()))

    def summary(self) -> dict:
        def rows(pairs):
            return [(str(test), text) for test, text in pairs]
        return {
            "run": self.testsRun,
            "failures": rows(self.failures),
            "errors": rows(self.errors),
            "skipped": len(self.skipped),
            "expected_failures": len(self.expectedFailures),
            "unexpected_successes": [str(test) for test in self.unexpectedSuccesses],
            "slow": self.slow,
        }


def _run_class(ids: list[str]) -> dict:
    """Run one class's selected tests as one suite, so its setUpClass runs once."""
    _paths()
    result = _Collected()
    try:
        unittest.defaultTestLoader.loadTestsFromNames(ids).run(result)
    except Exception as error:  # an import that worked in the parent failed here
        result.errors.append((ids[0], f"{type(error).__name__}: {error}"))
    return result.summary()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", help="modules, classes or tests (default: all)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--durations", type=int, default=0, metavar="N",
                        help="list the N slowest tests")
    args = parser.parse_args(argv)

    _paths()
    loader = unittest.defaultTestLoader
    if args.names:
        names = [name.removeprefix("tests.") for name in args.names]
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(str(TESTS), top_level_dir=str(TESTS))

    # A module that failed to import shows up as a placeholder test. It cannot be loaded
    # by name in a worker, so it is run here, where it simply reports the import error.
    everything = list(_flatten(suite))
    broken = unittest.TestSuite(
        test for test in everything if type(test).__module__ == "unittest.loader"
    )
    tasks = _by_class(
        test for test in everything if type(test).__module__ != "unittest.loader"
    )

    started = time.perf_counter()
    totals = _Collected()
    broken.run(totals)
    combined = [totals.summary()]
    jobs = max(1, min(args.jobs, len(tasks) or 1))
    with multiprocessing.Pool(jobs, initializer=_paths) as pool:
        for summary in pool.imap_unordered(_run_class, tasks):
            combined.append(summary)
            mark = "F" if summary["failures"] or summary["errors"] else "."
            print(mark, end="", flush=True)
    elapsed = time.perf_counter() - started
    print()

    run = sum(part["run"] for part in combined)
    failures = [row for part in combined for row in part["failures"]]
    errors = [row for part in combined for row in part["errors"]]
    skipped = sum(part["skipped"] for part in combined)
    unexpected = [row for part in combined for row in part["unexpected_successes"]]
    for label, rows in (("ERROR", errors), ("FAIL", failures)):
        for test, text in rows:
            print("=" * 70)
            print(f"{label}: {test}")
            print("-" * 70)
            print(text)
    if args.durations:
        slow = sorted((row for part in combined for row in part["slow"]), reverse=True)
        print("Slowest test durations")
        for seconds, test in slow[:args.durations]:
            print(f"{seconds:7.3f}s  {test}")
    print("-" * 70)
    print(f"Ran {run} tests in {elapsed:.3f}s on {jobs} workers")
    print()
    extra = [f"skipped={skipped}"] if skipped else []
    if unexpected:
        extra.append(f"unexpected successes={len(unexpected)}")
    if failures or errors or unexpected:
        counts = [f"failures={len(failures)}"] if failures else []
        counts += [f"errors={len(errors)}"] if errors else []
        print(f"FAILED ({', '.join(counts + extra)})")
        return 1
    print("OK" + (f" ({', '.join(extra)})" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
