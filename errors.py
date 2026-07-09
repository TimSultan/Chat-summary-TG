class ChatSummaryError(Exception):
    """Expected, user-facing error (bad input/config/permissions) -- the message alone
    is enough context to show the user, no traceback needed. Deliberately a plain
    Exception (not SystemExit) so it can't escape background threads/asyncio tasks in
    gui.py and listener.py uncaught."""
