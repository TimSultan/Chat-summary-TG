# Chat Summary TG

Reads all messages from a specific Telegram chat you're a member of, for a specific day,
and produces a markdown digest of the main topics discussed that day — grouping a whole
back-and-forth (even if 20 people piled on) into one entry with its key points and
conclusion, if the conversation reached one. Small talk, spam, and low-content threads
are filtered out.

Logs into Telegram as **you** (via [Telethon](https://docs.telethon.dev)), not a bot, so it
can read history of any chat you're already in — including messages from before the tool
existed.

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Get Telegram API credentials from https://my.telegram.org/apps (log in with your phone
   number, create an app, copy the `api_id` and `api_hash`).

3. Get an OpenAI API key from https://platform.openai.com/api-keys.

4. Copy `.env.example` to `.env` and fill in `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and
   `OPENAI_API_KEY`.

## First run / login

The first time you run the tool it will ask for your phone number, then a login code sent
to your Telegram app, and your 2FA password if you have one set. This creates a local
session file (named by `TELEGRAM_SESSION` in `.env`, default `tg_summary_session.session`)
so you won't have to log in again. **Keep that file private** — it's equivalent to being
logged into your account.

## Usage: desktop window (`gui.py`)

```
python gui.py
```

A small window with a model picker on top and three tabs:
- **Model** dropdown — pick from `RECOMMENDED_MODELS` in `config.py` (editable, so you
  can type any future model string too). Applies to both tabs below.
- **Generate Summary** — fill in chat / date / optional user filter / optional timezone,
  click Generate. Progress and errors show in the log pane at the bottom; "Open Output
  Folder" opens the saved `.md` once it's done.
- **Live Listener** — Start/Stop the mention-triggered auto-reply bot (see below) without
  a terminal.
- **History** — every question the listener has answered: who asked, in which chat,
  when, and a preview of the question. Double-click a row (or "Open Answer") to open the
  full answer in its own file rather than cramming it into the list. Refreshes every 5
  seconds while the window is open.

First use pops up plain dialog boxes for the phone number / login code / 2FA password
instead of a terminal prompt. The Generate Summary and Listener tabs share one Telegram
session, so only one can run at a time.

## Usage: CLI digest (`main.py`)

```
python main.py --chat "My Group" --date 2026-07-08
python main.py --chat @some_channel --date yesterday
python main.py --chat -1001234567890 --date today --tz Europe/Istanbul

# date ranges
python main.py --chat "My Group" --date 2026-07-01:2026-07-08
python main.py --chat "My Group" --date last7days

# what did one person talk about?
python main.py --chat "My Group" --date last7days --user @some_user
```

- `--chat` — the chat's `@username`, numeric ID, or a substring of its title (if
  ambiguous, the tool lists all matching chat titles so you can be more specific).
- `--date` — `YYYY-MM-DD`, a `YYYY-MM-DD:YYYY-MM-DD` range, `today`, `yesterday`,
  `last7days`, or `last30days` (default: `today`).
- `--user` — restrict the summary to what one participant discussed (matched by
  `@username` or a substring of their display name). The full transcript is still used
  for context, but the summary only covers topics that person raised or actively took
  part in.
- `--tz` — IANA timezone for defining calendar days, e.g. `Europe/Istanbul` (default:
  your system's local timezone).
- `--model` — override the model from `.env` for this run (e.g. `gpt-5.4-mini`,
  `gpt-5.5`, `gpt-5.4-nano` -- see model choice below).
- `--output-dir` — where to write the markdown file (default: `output/`).
- `--force` — ignore the cached transcript and re-fetch every day fresh from Telegram
  -- see caching below.

Output is saved to `output/<chat title>[_user]_<date(s)>.md`.

## Usage: live command-triggered replies (`listener.py`)

Run `python listener.py` and leave it running (a terminal, `screen`/`tmux` session, or a
background service). While it's running, any message in an allowed chat containing the
trigger keyword (default `/summary`) gets a themed summary reply — sent as **you**, in
that chat. Works like a slash-command: no @mention or reply-to-you needed, from anyone,
including yourself:

```
/summary что обсуждали сегодня
  -> replies with today's chat topics, in Russian

/summary сообщения @some_user за сегодня
  -> replies with what @some_user talked about today
```

The listener never re-triggers on its own generated replies (tracked by message ID),
even though a reply's text will often contain the trigger keyword itself.

The rest of the request text is parsed by the LLM (mixed languages, relative dates like
"сегодня" / "вчера", and an optional target user are all handled), so past the trigger
keyword itself there's no fixed syntax — phrase it naturally.

**Anti-spam behavior:**
- **One specific day only** — a request spanning more than one day (e.g. "this week",
  "last 7 days") is refused with a short notice ("Сводка выдается Только за 1 конкретный
  день и юзера") instead of being processed, regardless of whether it's a whole-chat or
  per-user question. This applies to the listener only -- `main.py`/`gui.py` still
  support date ranges for your own generated reports.
- **Cooldown with a reply, not silence** — `LISTENER_COOLDOWN_SECONDS` (default 180 = 3
  minutes) limits how often the listener will answer in the same chat. Asking again
  before that gets "Спросите через X минут" instead of no response, and that notice
  (like the day-limit one) auto-deletes after 10 seconds.

**Before running this against real chats**, set `LISTENER_ALLOWED_CHATS` in `.env` to a
comma-separated allowlist of chats (by `@username`, exact title, or numeric ID). Without
it, the listener will respond to *anyone* who mentions you in *any* chat you're in,
spending your OpenAI budget on their requests — fine for a private group with people you
trust, risky in a large/public one.

### Roasting (`прожарь меня`) -- currently disabled

The trigger is switched off (forced to never match, in both `listener.py` and
`bot_listener.py`) rather than removed, so turning it back on is a one-line revert. The
rest of this section describes how it behaves when enabled.

A second trigger keyword (default `прожарь меня`, `ROAST_TRIGGER_KEYWORDS` in `.env`)
roasts whoever sends it, in Russian, using **their own** messages from the last
`ROAST_LOOKBACK_DAYS` days (default 30). It's a two-step, confirm-then-react flow rather
than an immediate reply:

```
прожарь меня
  -> bot replies "Ты точно хочешь прожарку? поставь реакцию для подтверждения"

(you react to that prompt with any emoji)
  -> pulls your own messages from the last 30 days (across each day's cached
     transcript), sends them to OpenAI, and replies with a no-holds-barred 5-point
     roast (Russian, swearing allowed) plus a punchline
```

Only a reaction from the **same person who was asked** counts -- someone else reacting
to your confirmation prompt does nothing. If you send `прожарь меня` again while your
previous request is still awaiting a reaction or already generating, the bot doesn't
send another prompt -- it just reacts to your new message with ⏳ to show one's already
in flight.

It reuses the same per-day transcript cache as `/summary` (see caching below), so
roasting doesn't re-fetch days already pulled for other requests. Same allowlist and
cooldown as the summary trigger (applies to the initial confirmation prompt). Unlike
`/summary`, **the roast itself does not self-delete** -- it stays in the chat. If you
have no messages in that window, it replies with a short "nothing to roast" notice
instead of calling OpenAI.

**On an active chat, generation itself can take a while.** Roasting map-reduces the
transcript into ~6000-token chunks with one *sequential* OpenAI call per chunk, so an
uncapped month of messages from a chatty poster can mean dozens of blocking calls before
anything is sent -- with no "generating..." message in between, that looks like the bot
hung. `ROAST_MAX_MESSAGES` (default 400) caps input to the requester's most recent N
messages to keep this bounded; lower it for faster/cheaper roasts.

### Jokes -- off by default

Unlike everything else in this project, `JOKE_ENABLED=true` (`.env`) adds one thing
nobody has to ask for: an occasional short, in-context joke or remark, dropped into the
chat while it's actually active. Requires a bot account (`TELEGRAM_BOT_TOKEN`) and a
non-empty `LISTENER_ALLOWED_CHATS` -- it never defaults to "everywhere" the way
`/summary` does, and always posts as the bot, never your personal account.

It only fires off real, recent activity, not a timer: `JOKE_ACTIVITY_MIN_MESSAGES`
(default 8) messages have to land within the trailing `JOKE_ACTIVITY_WINDOW_SECONDS`
(default 300 = 5 min). A quiet or sleeping chat can't cross that on its own, so there's no
clock-based firing at all -- it's structurally impossible for this to go off in a dead
chat. Once that's true, `JOKE_COOLDOWN_SECONDS` (default 1 hour) since the last one still
has to have passed, *and* a random roll under `JOKE_FIRE_PROBABILITY` (default 0.35) has
to hit, so it doesn't fire like clockwork the instant the threshold is crossed.

On top of all of that, nothing reviews a joke before it posts, so the model itself
(`joke.py`) is instructed to back off (respond with a `SKIP` sentinel, which is silently
dropped -- nothing gets sent) for anything that isn't actually a good moment: an active
argument, a heavy or personal topic (appearance, health, money, relationships, grief),
protected-characteristic territory, or anything it would otherwise have to invent instead
of drawing from what was actually said. Verified against real chat transcripts -- an
active public accusation/conflict and a body-image conversation were both correctly
skipped, while ordinary banter got a short, specific, on-topic joke.

## Model choice

`config.py` defines `RECOMMENDED_MODELS`, curated as of July 2026: `gpt-5.4-mini`
(default -- fast, cheap, a big step up from the old `gpt-4o-mini`), `gpt-5.5` (flagship,
best quality, similar latency to 5.4), `gpt-5.4-nano` (fastest/cheapest, fine for quiet
chats), plus `gpt-5`/`gpt-5-mini`/`gpt-5-nano` and the legacy `gpt-4o`/`gpt-4o-mini`.
Set `OPENAI_MODEL` in `.env`, pass `--model` on the CLI, or pick from the GUI's dropdown
(which also accepts typing in anything not on the list, for whenever this list goes
stale).

## Caching: the raw transcript, not the answer

What's expensive and reusable is *reading the chat* -- what's cheap and always-fresh is
*answering a specific question about it*. So the tool caches per calendar day, per chat,
the raw fetched transcript (under `cache/transcripts/`), not the generated summary:

- A day that's fully in the past can't gain new messages, so once fetched it's cached
  indefinitely.
- Today (the day still in progress) is cached for 30 minutes (`transcript_cache.py`'s
  `TODAY_TTL_SECONDS`). A request within that window reuses the saved transcript; past
  it, the day is re-fetched and the file updated before answering.
- Every request -- "summary of today", "what did Anzhelika talk about today", asked five
  minutes apart by different people -- always gets its own fresh OpenAI call, just
  against a transcript that's often already on disk instead of freshly pulled from
  Telegram.

`--force` (CLI) / "Force refresh" (GUI checkbox) bypasses the cache and re-fetches every
day in the requested range regardless of freshness. The listener always uses the cache
when available; delete files under `cache/transcripts/` to force specific days to
refresh.

## Asking about a specific person, even if you don't @mention them

`/summary situation with Anzhelika` (no `@mention` of Anzhelika, possibly misspelled
or transliterated) still works: the LLM first notes it's a person-reference it couldn't
resolve to an exact username, then -- once the day's transcript is in hand -- a second
pass matches that name against the chat's actual participants (handles misspellings,
nicknames, and script transliteration, e.g. "Anzhelika" for a Cyrillic "Анжелика") and
scopes the summary to topics that person was involved in, including ones others
discussed *about* them without them posting.

## Deploying the listener to Railway

Only `listener.py` runs on a server -- `gui.py` needs a display, and `main.py` is a
one-off you'd normally run locally. Railway can't do interactive phone/code logins, so
generate a portable session first:

1. **Locally**, with `.env` filled in: `python generate_session_string.py` (log in
   interactively, once) -- or if you already have a working local session file
   (`tg_summary_session.session` from an earlier `main.py`/`gui.py` login),
   `python convert_existing_session.py` instead, which skips the phone/code step
   entirely. Either way it prints a session string. (`debug_login.py` exists to
   diagnose a "no code received" problem, if that happens.)
2. Push this repo to GitHub (or use the Railway CLI to deploy without GitHub -- see
   below), then in Railway: **New Project → Deploy from GitHub repo** (or **Empty
   Project**, then `railway login && railway link && railway up` from this directory
   with the [Railway CLI](https://docs.railway.app/guides/cli), if you don't want to use
   GitHub). Railway will pick up the `Dockerfile` automatically (`railway.json` pins it
   explicitly).
3. In the Railway service's **Variables** tab, set everything from `.env.example`:
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING` (the string from
   step 1 -- leave `TELEGRAM_SESSION` unset, it's not used when this is set),
   `OPENAI_API_KEY`, `OPENAI_MODEL`, `LISTENER_ALLOWED_CHATS` (**set this** -- see the
   warning above), `LISTENER_TRIGGER_KEYWORDS`, `LISTENER_COOLDOWN_SECONDS`,
   `ROAST_TRIGGER_KEYWORDS`, `ROAST_LOOKBACK_DAYS`, `TELEGRAM_BOT_TOKEN` (if replies
   should come from a bot account instead of this one -- see above), `JOKE_ENABLED` and
   the other `JOKE_*` vars if you also want the occasional unprompted joke (off by
   default; see Jokes above).
4. Deploy. Check the Railway logs for `[listener] logged in as @...` to confirm it's
   running.

**Persistence is optional -- the listener works fine without it.** Without any
persistent disk, the transcript cache and Q&A history just reset to empty on every
redeploy/restart (a minor efficiency loss, not a functional break); the Telegram session
itself never needs one, since `TELEGRAM_SESSION_STRING` is just an env var. If you want
the cache/history to survive restarts: add a Railway **Volume** (not a Bucket -- a Bucket
is S3-style object storage and this app just writes plain files, so it doesn't apply),
mount it at any path (e.g. `/data`), and set `DATA_DIR=/data` in the service's Variables.

**Cost note:** this is a long-running worker (not a request-driven web service), so it
runs continuously and bills for uptime accordingly -- check Railway's current pricing
before leaving it deployed indefinitely.

## Notes

- Very active chats/ranges are automatically split into chunks, pre-summarized in parts,
  then merged into one final themed summary so topics that span chunks still get combined
  into a single entry.
- Media messages (photos, videos, stickers, voice notes, etc.) are included as tags like
  `[Photo]` so they factor into topic detection, but their content isn't analyzed.
- Anonymous/admin-posted messages are attributed to the channel/group name Telegram gives
  them, since Telegram doesn't expose the real sender in that case.
- The listener doesn't require a Telegram `@username` on your account -- triggering
  doesn't need @mentions at all anymore. Without one, it just logs a warning and skips a
  couple of minor "never target myself" safety checks in name resolution.
- Every question the listener answers (and its full answer) is recorded under `history/`
  (`history.py`) -- one small index file plus one file per answer. The GUI's History tab
  reads this; it's also there if you'd rather grep the files directly.
