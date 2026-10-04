"""IsolatedAsyncioTestCase without asyncio debug mode.

unittest's IsolatedAsyncioTestCase hard-codes ``asyncio.Runner(debug=True)``. Debug mode
captures a stack trace for every scheduled callback and every Future, and for the web
suites that bookkeeping was most of the run: test_pets_web took 33s with it and 10s
without, with identical results. Nothing here reads what debug mode produces -- the
slow-callback warnings it prints are not assertions, and the tests that guard the event
loop do it with a deliberately blocked worker and events (AGENTS.md), not with debug mode.

``loop_factory`` is honoured from Python 3.13; on an older interpreter the class still
works and simply keeps debug mode on.
"""

import asyncio
import sys
import unittest

_EventLoop = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop


class _NoDebugLoop(_EventLoop):
    def set_debug(self, enabled: bool) -> None:
        super().set_debug(False)


class AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    loop_factory = _NoDebugLoop
