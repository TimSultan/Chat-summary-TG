"""Open and close the game, without a redeploy.

This is the tool the deployment process is built around, and the reason the flag lives on
the volume rather than in an environment variable: flipping an env var IS a restart, so
you would be restarting around the very restart you were trying to make safe.

    python admin_pause.py status
    python admin_pause.py on  --notice "Обновление, минут на пять"
    python admin_pause.py off

Run it where the data lives -- the deployed volume, not a developer checkout. It writes
one small file and nothing else, so it works when the bot itself is down, which is exactly
when you are most likely to need it.

The usual order:

    admin_pause.py on   ->  git push  ->  wait for the deploy  ->  check  ->  admin_pause.py off

Reads keep working the whole time. Farm and quarry shifts keep running too: they are
settled from timestamps whenever they are next looked at, so a pause neither pays them
early nor loses them.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import maintenance
import stats


def _make_console_utf8_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _report(state: dict) -> None:
    if not state["paused"]:
        print("🟢 Игра открыта.")
        return
    since = str(state.get("since") or "")
    try:
        since = datetime.fromisoformat(since).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        pass
    who = f" · {state['by']}" if state.get("by") else ""
    print(f"🔴 Игра на паузе с {since or 'неизвестно когда'}{who}")
    print(f"   Игроки видят: {state['notice']}")


def main(argv: list[str] | None = None) -> int:
    _make_console_utf8_safe()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="открыта игра или нет")
    on = commands.add_parser("on", help="закрыть игру на время обновления")
    on.add_argument("--notice", default="", help="что увидят игроки")
    on.add_argument("--by", default="", help="кто закрыл, для журнала")
    off = commands.add_parser("off", help="открыть игру обратно")
    off.add_argument("--by", default="")
    args = parser.parse_args(argv)

    print(f"Файл флага: {maintenance._path()}")
    if args.command == "status":
        _report(maintenance.status())
        return 0
    if args.command == "on":
        _report(maintenance.pause(args.notice, args.by))
        print("\nТеперь деплой. После него — admin_pause.py off")
        return 0
    _report(maintenance.resume(args.by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
