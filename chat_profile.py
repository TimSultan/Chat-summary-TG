"""Builds and caches a compact "flavor profile" of a chat -- its humor style, recurring
jokes/phrases, and notable participants' vibe -- from a few days of its already-cached
transcript. Fed into joke.py's prompts (both the buffer-triggered automatic path and the
manual "пошути" trigger) so generated jokes read like they came from someone who's
actually been in the room, not a generic bystander.

Refreshed on a TTL (JOKE_PROFILE_TTL_SECONDS in config.py), not on every joke -- this
reads multiple days of history, which is comparatively expensive, and a chat's overall
vibe doesn't meaningfully shift message to message. `ensure_profile` is the entry point
everything else calls: it returns the cached profile if still fresh, otherwise fetches
(via the existing per-day transcript cache -- no extra Telegram load) and regenerates.
"""

import asyncio
import hashlib
import json
import os
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI, OpenAIError

from app_time import cache_namespace, now as app_now, resolve_timezone
from errors import ChatSummaryError

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
PROFILE_DIR = DATA_DIR / "cache" / "chat_profile"


def _profile_dir() -> Path:
    return PROFILE_DIR / cache_namespace(resolve_timezone())

PROFILE_SYSTEM_PROMPT = """\
Ты анализируешь историю сообщений группового чата, чтобы составить компактную \
характеристику его атмосферы и юмора. Это будет использовано, чтобы шутки бота в этом \
чате звучали органично, как от своего, а не от постороннего.

Опиши, одним-двумя короткими абзацами, без заголовков и markdown-разметки:
- какой тут стиль юмора (сарказм, абсурд, доброе подкалывание, самоирония, мат в ходу или нет)
- повторяющиеся шутки, фразочки, мемы или темы, которые всплывают снова и снова
- заметные, часто пишущие участники и их "образ" в чате -- только то, что реально видно \
из переписки, без выдумок; это справочный контекст, а не прожарка, так что без \
оскорблений и без педалирования личных/уязвимых тем.

Пиши по-русски, по делу."""

PROFILE_USER_PROMPT = """\
Сообщения чата "{chat_title}" за последние несколько дней (формат \
"[YYYY-MM-DD HH:MM] Имя: текст"):
{transcript}

Составь характеристику атмосферы и юмора этого чата по инструкции из системного промпта."""


def _cache_key(entry: str) -> str:
    return hashlib.sha1(entry.strip().lower().encode("utf-8")).hexdigest()[:16]


def _path(entry: str) -> Path:
    return _profile_dir() / f"{_cache_key(entry)}.json"


def is_stale(entry: str, ttl_seconds: int) -> bool:
    """Cheap and synchronous -- just a file read, no network. True if there's no cached
    profile yet, or the cached one is older than ttl_seconds."""
    path = _path(entry)
    if not path.exists():
        return True
    payload = json.loads(path.read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(payload["generated_at"])
    age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
    return age_seconds > ttl_seconds


def load_cached_profile(entry: str) -> str | None:
    """Whatever's cached, regardless of freshness -- callers that care should check
    is_stale first. Used as a fallback if a refresh attempt fails."""
    path = _path(entry)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["profile"]


def generate_and_cache_profile(api_key: str, model: str, chat_title: str, entry: str, lines: list[str]) -> str | None:
    """Blocking (one OpenAI call) -- run via asyncio.to_thread. Always regenerates and
    overwrites the cache regardless of current freshness."""
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")
    if not lines:
        return None

    client = OpenAI(api_key=api_key)
    prompt = PROFILE_USER_PROMPT.format(chat_title=chat_title, transcript="\n".join(lines))
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.7,
            messages=[
                {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while building the chat flavor profile: {e}") from e

    content = (response.choices[0].message.content or "").strip()
    if not content:
        return None

    _profile_dir().mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": app_now().isoformat(), "profile": content}
    _path(entry).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return content


async def ensure_profile(
    client,
    chat_ref: str,
    entry: str,
    api_key: str,
    model: str,
    tz,
    ttl_seconds: int,
    lookback_days: int,
    max_messages: int,
    log=print,
) -> str | None:
    """The entry point listener.py and bot_listener.py both call. Returns the cached
    profile if still fresh; otherwise fetches lookback_days of history for `chat_ref`
    (via telegram_fetch's existing per-day cache, so this doesn't add Telegram load on
    its own -- only a fresh OpenAI call, and only once per ttl_seconds) and regenerates.
    Falls back to whatever's cached (even if stale) rather than None if a refresh
    attempt fails, so a transient error doesn't strip jokes of room-context entirely."""
    if not is_stale(entry, ttl_seconds):
        return load_cached_profile(entry)

    from telegram_fetch import fetch_range_messages_cached, format_transcript_lines

    end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=lookback_days - 1)
    try:
        chat_title, messages = await fetch_range_messages_cached(
            client=client, chat_ref=chat_ref, start_day=start_date, end_day=end_date, tz=tz, log=log,
        )
    except Exception:
        log(f"[chat_profile] failed to fetch history for flavor profile:\n{traceback.format_exc()}")
        return load_cached_profile(entry)

    if not messages:
        return load_cached_profile(entry)
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
    lines = format_transcript_lines(messages, include_date=True)

    try:
        return await asyncio.to_thread(generate_and_cache_profile, api_key, model, chat_title, entry, lines)
    except Exception:
        log(f"[chat_profile] failed to generate flavor profile:\n{traceback.format_exc()}")
        return load_cached_profile(entry)
