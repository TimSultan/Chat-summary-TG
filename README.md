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
- **FIFO queue instead of cooldown rejection** — every accepted summary enquiry is
  retained and answered in arrival order. The first starts immediately; after one
  enquiry finishes, the worker waits `SUMMARY_QUEUE_DELAY_SECONDS` (default 20) before
  starting the next. Bursts are delayed rather than discarded.
- **Impression fallback** — a request containing `впечатление` about one person, with no
  explicit date, starts with today. If that person has fewer than 15 matching messages
  today, yesterday is prepended and the impression uses both Moscow calendar days.
  If the combined two-day total is still below 15, the bot replies that the user was not
  active during those days instead of asking the model to invent an impression. Explicit
  periods such as `сегодня` or `за вчера` are always respected as written.

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
roasting doesn't re-fetch days already pulled for other requests. The same allowlist
applies to its initial confirmation prompt. Unlike
`/summary`, **the roast itself does not self-delete** -- it stays in the chat. If you
have no messages in that window, it replies with a short "nothing to roast" notice
instead of calling OpenAI.

**On an active chat, generation itself can take a while.** Roasting map-reduces the
transcript into ~6000-token chunks with one *sequential* OpenAI call per chunk, so an
uncapped month of messages from a chatty poster can mean dozens of blocking calls before
anything is sent -- with no "generating..." message in between, that looks like the bot
hung. `ROAST_MAX_MESSAGES` (default 400) caps input to the requester's most recent N
messages to keep this bounded; lower it for faster/cheaper roasts.

### Natural chat remarks -- off by default

Unlike everything else in this project, `JOKE_ENABLED=true` (`.env`) adds one thing
nobody has to ask for: an occasional short, in-context remark, dropped into the chat
while it's actually active. Despite the legacy `JOKE_*` setting names, the prompt does
not ask the bot to make a joke; it asks for an ordinary continuation in the room's own
style, with a short sarcastic reaction only when that naturally fits. Requires a bot account (`TELEGRAM_BOT_TOKEN`) and a
non-empty `LISTENER_ALLOWED_CHATS` -- it never defaults to "everywhere" the way
`/summary` does, and always posts as the bot, never your personal account.

It only fires off real message volume, not a timer: `JOKE_ACTIVITY_MIN_MESSAGES` (default
20) qualifying messages have to land in a rolling per-chat buffer before it's even
considered -- a quiet or sleeping chat simply never fills that buffer, so it's
structurally impossible for this to go off in a dead chat, no matter how long it waits.
Once the buffer's full, it fires if the chat's cooldown has passed and a random roll under
`JOKE_FIRE_PROBABILITY` (default 0.35) hits; a miss doesn't reset the buffer, so the very
next message tries again rather than needing a whole new batch of 20.

Cooldown only ever kicks in after an actual remark gets sent -- `JOKE_COOLDOWN_MIN_SECONDS`/
`JOKE_COOLDOWN_MAX_SECONDS` (default 30-60 min, picked randomly each time so it's not a
flat interval). If the model declines instead (see below), there's no cooldown at all --
it just costs another full buffer of messages, not a timer, so one tense stretch of chat
can't suppress remarks for an hour once things lighten back up. A remark that lands well gets
rewarded: if it picks up `JOKE_REACTION_THRESHOLD` (default 3) reactions, that chat's
cooldown is pulled in to `JOKE_REACTION_COOLDOWN_SECONDS` (default 30 min) from when it
was posted, whichever is sooner.

On top of all of that, nothing reviews a remark before it posts, so the model itself
(`joke.py`) is instructed to back off (respond with a `SKIP` sentinel, which is silently
dropped -- nothing gets sent) for anything that isn't actually a good moment: an active
argument, a heavy or personal topic (appearance, health, money, relationships, grief),
protected-characteristic territory, anything it would otherwise have to invent, or a
moment where adding a message would feel forced.

**Feeling the room (`chat_profile.py`).** Every remark -- automatic or manual -- gets a
compact "flavor profile" of the chat alongside whatever prompted it: language mixing,
typical message length and structure, casing, punctuation, slang, conversational rhythm,
recurring context, and tone, built from a few days
of the already-cached transcript (`JOKE_PROFILE_LOOKBACK_DAYS`, default 3). This is one
OpenAI call, cached and reused for `JOKE_PROFILE_TTL_SECONDS` (default 24h) rather than
regenerated per remark, so it stays cheap. The profile format is versioned, so changing
these instructions invalidates an older cached profile immediately instead of waiting
for its normal TTL.

For each actual remark, the model gets exactly the newest 20 live chat messages. The
first 15 are labelled as background context; the final 5 are labelled as the active
conversation it must answer. Manual preview uses the same 20-message window. The model
may add something useful, funny, serious, sarcastic, or simply conversational, but is
never asked to produce a joke and is told not to use emoji.

**Manual trigger.** DM the bot `пошути` (`JOKE_MANUAL_TRIGGER_KEYWORD`) to generate a remark in
the home chat right now, bypassing the buffer/cooldown/random-roll gates above -- it's an
explicit ask. The model can still decline, and a remark that does go out still starts the
normal cooldown, so this can't be used to dodge it. DM `пошути превью`
(`JOKE_MANUAL_PREVIEW_KEYWORD`) instead to see the remark in the DM first, with a button to
actually send it to the chat -- useful for trying the feature out without risking a dud
landing in front of everyone. Both need `LISTENER_ALLOWED_CHATS` to name exactly one chat
(same requirement as DM `/summary`), and work independently of `JOKE_ENABLED` -- a manual
ask doesn't carry the "unprompted" risk that setting is guarding against.

### Conversational replies to the bot

When a person uses Telegram's **Reply** action on any message authored by the bot, the
bot always answers as a normal participant. This is built-in behavior, not part of
`JOKE_ENABLED`, and has no probability, cooldown, short watch window, or separate feature
flag. It works for bot messages sent before the current process started too, because the
incoming Telegram update identifies the replied-to message and its author directly.

The answer sees the exact bot message being replied to, the person's response, the newest
20 messages in the chat, and the same cached multi-day style profile used by natural chat
remarks. It may be funny when that fits, but it is prompted to answer questions and react
normally rather than forcing a joke. Messages in the general chat flow do not trigger
this behavior; the person must reply directly to the bot. Explicit commands such as
`/summary`, `/stat`, `/top`, and `/badge` keep their specialized behavior even when sent
as replies.

### XP, levels, coins, and badges

`/top today|week|month|year|all` ranks tracked members by XP. The existing activity
formula is unchanged; only its user-facing name changed from points to XP. `/top week`
also shows the member with the largest positive XP change compared with the preceding
seven-day window.

`/stat [username]` shows all-time XP plus three **independent** progression tracks. They
were split apart deliberately: a single ladder gated on XP *and* figurines at once meant
a member who chatted constantly but painted nothing, and a member who painted constantly
but rarely posted, were both frozen at the bottom forever. Now everybody always has at
least one bar moving.

**🧩 Уровень — chat level.** Scored on **season XP**, not all-time, with no figurine
requirement. Forty levels on a `25 × n^1.6` curve, renamed every five levels (🌱 Новенький
→ 💬 Болтун → 🗣️ Голос чата → 📣 Заводила → 🎙️ Старожил → 🔥 Душа чата → ⚡ Легенда общения
→ 🌟 Хранитель чата). A progress bar shows position inside the current level **without**
printing the target, so the old "don't reveal the next requirement" rule still holds.

Seasons are calendar quarters (Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec) — fixed boundaries so
everyone's season starts on the same day. Season XP and all-time XP are accumulated in the
**same aggregation pass** (`UserStats.season_*`), so `/stat` pays for one walk, not two.

The curve is calibrated from the chat's own measured rates, caps applied:

| | XP/day | after one season | max reached in |
|---|---|---|---|
| top-1 | 299 | level 40 | 31 days |
| **p95** | **103** | **level 40** | **89 days** ← the target |
| p90 | 68 | ⚡ Легенда общения 31 | 134 days |
| p75 | 12 | 💬 Болтун 10 | 2 years |
| median | 2.8 | 🌱 Новенький 4 | — |

Levels are scored seasonally because the two goals are otherwise incompatible: with
all-time XP, a ladder cheap enough to climb in a season is one that members tracked for a
year would start already past the top of, and it would never move again.

Only a **tier** change (every five levels) is announced, not each level. On this curve an
active member crosses ~40 levels a season; announcing each would put several promotion
messages a day into the chat from the same few people. A new season re-baselines everyone
silently — the ladder was rebuilt, nobody was demoted.

**🎨 Звание — painter rank.** The original seven names, now gated on figurines alone:

- 🩶 Серый новичок — 0 figurines
- ⚪ Ученик грунта — 3
- 🖌️ Подмастерье кисти — 5
- 💨 Укротитель аэрографа — 10
- 💧 Повелитель проливок — 20
- 🏛️ Мастер витрины — 35
- 👑 Легенда покраса — 50

**Репутация — peer-granted standing.** Cannot be earned by posting at all: 10 per weekly
contest win, 5 per administrator-awarded custom badge, and 1 per 20 coins *received* from
another member. Tiers: Пока тихо → 🌿 Замеченный → 👏 Уважаемый → 🤝 Опора чата →
🏅 Легенда сообщества.

A promotion on either the chat level or the painter rank is announced once, tracked per
track so progress on one never suppresses the other. The stored level state from before
the split is discarded rather than compared against, which silently re-baselines every
existing member — otherwise the rollout would announce a promotion for the whole chat at
once.

### ЕПХ Дерево — the chat's shared progression

One tree the whole chat grows together (`tree.py`). Unlike every other ladder here it is
not per-member: all XP earned in the chat pools into a single height, so a quiet member's
few points move the same tree as the loudest member's. It is the one score nobody
competes on.

Calibrated against the chat's own measured output rather than guessed — over a 34-day
window the whole chat produced **~3,600 XP/day**. At 200 XP per millimetre and a 20 m
ceiling that is ~18 mm a day, and the final stage lands just under three years out:

| | after | stage |
|---|---|---|
| 1 day | 1.8 cm | 🌰 Семечко |
| 1 month | 54 cm | 🪴 Саженец |
| 1 year | 6.6 m | 🍃 Крепкое дерево |
| 2 years | 13.1 m | 🦉 Дерево с дуплом |
| **3 years** | **19.7 m** | **👑 Легендарное Древо ЕПХ** |

Thirteen stages, set in **height** rather than XP so the names line up with a tree
somebody can picture. Height is capped at the top: XP accrues forever, and without the cap
the tree would silently grow past its own last name.

#### Planting it: the ceremony

`/посадить_семечко` — or `/plant`, see below — posts an invitation to the chat and starts
collecting presses on the button under it. The next 10:00 post names everyone who pressed,
plants the tree, and hands each of them the **🌱 Основатель** badge. Administrators only,
and it works from the chat itself as well as from the bot's DM; only the acknowledgement
follows the admin back to wherever they typed it.

Two spellings for one action because Telegram only treats `[a-zA-Z0-9_]` after a slash as
a command: `/посадить_семечко` is never highlighted, never autocompletes, cannot be
registered in the menu, and — if the bot's privacy mode is ever turned back on — would not
reach the bot in a group at all. `/plant` always works. The Cyrillic spelling is kept
because it reads far better in the chat.

Members take part by **pressing a button**, not by reacting. A reaction was the original
design and had to go: Telegram only accepts its own fixed quick-reaction set
([core.telegram.org/api/reactions](https://core.telegram.org/api/reactions)), which
contains no 🌳, 🌱 or 🌿 at all — 🎄, a new year tree, is the only tree in it, and anything
outside the set fails silently with no other symptom. A button carries any emoji, reports
exactly who pressed it, and needs no Telethon session to read. Presses are answered as a
toast on the presser's own screen, so 190 members tapping a button cannot become 190
messages in the chat.

Nothing is pinned or unpinned. The admin pins the invitation by hand, which is why the bot
never asks for `can_pin_messages` — and why nothing has to remember to unpin at 10:00.

If **nobody** pressed by 10:00 the tree is deliberately *not* planted and the ceremony
stays open for another day: opening the whole thing on an empty roll call would be worse
than waiting for a better one. While a ceremony is open `/tree` answers "Посадка открыта"
rather than a meaningless "0 мм".

The guest list goes through the stats directory because the two halves live in different
processes — presses arrive at `bot_listener.py`, the 10:00 post is built by `listener.py`.
Each planter's display name is stored **at press time** rather than looked up later: the
roll call has to name members who have never written a word in the chat, and those are
exactly the ones no stats file knows about.

The founder badge lives in the custom-badge store rather than in `AUTOMATIC_BADGES`,
because nothing about it can be recomputed from a member's stats — it records a single
afternoon, and afterwards there is no way to earn it again. That also puts it in the
`✨ Уникальные значки` block at the top of `/stat`, which is where a thing you cannot earn
belongs. It is exempt from `MAX_CUSTOM_BADGES`, so a chat that had already filled its badge
budget still plants its tree with something to show for it.

`/replant` (DM, administrators only) posts the planting announcement to the chat and
starts the tree over from today. It exists because the planting date lives in the stats
directory, which on a deployed host is a volume nothing else here can reach — without a
command there is no way to re-run the opening post at all. Both halves happen together on
purpose: re-posting "сегодня мы посадили семечко" while the tree is already a metre tall
would be a lie, and re-planting without posting would silently zero the chat's progress.
The reset only happens once the announcement has actually landed, and it marks that day
as already greeted — the announcement *is* that morning's post, so without this the 10:00
loop would follow it with an ordinary zero-growth digest.

Neither the planting post nor the morning digest is ever scheduled for deletion — they
stay in the chat. Only `/tree` and the other on-demand stats replies self-delete.

The tree's height is measured **from its planting day forward**, not from the chat's whole
history. That is what makes "сегодня мы посадили семечко" true: this chat had months of
tracked activity before the tree existed, and counting it would plant a seed already a
metre tall. It also means the three-year horizon starts when the tree does.

The very first morning post plants it instead of reporting on it. No numbers, because on
that day a height of "0 мм" would undercut the moment — and, deliberately, **no mention of
how many stages there are or how long it takes**. Same rule `/stat` already follows by not
printing the next level's threshold: "thirteen stages and three years" turns an open-ended
thing the chat is growing into a progress bar with a visible end. The table above is
engineering documentation; none of it is ever said in the chat.

```text
🌱 Сегодня мы все вместе посадили семечко.

Из него вырастет могучее дерево ЕПХ — одно на весь чат, общее.
...
🌳 Давайте растить его вместе и радоваться каждому новому шагу.
```

Every morning at **10:00 Moscow** (`TREE_DIGEST_HOUR`, pinned to `Europe/Moscow` rather
than the app timezone — the deployment's own zone is a hosting detail that could move):

```text
🌳 Доброе утро, ЕПХ-чане!

Вчера наше дерево подросло на 20 мм.
Сейчас оно на стадии 🪴 Саженец. Высота — 61,2 см.
До следующей стадии «Молодая поросль» — 38,8 см.

Особенно помогли дереву вырасти:
@nalumurrr — 423 XP
@citrusssska — 383 XP
@Cloververona — 326 XP

Идея на день
Стабильный профиль принтера стоит сохранить прежде, чем экспериментировать дальше.
```

`/tree` answers the same question on demand, in a group or a DM:

```text
🌳 Высота нашего дерева ЕПХ — 61,2 см.
Сейчас оно на стадии 🪴 Саженец.
До следующей стадии «Молодая поросль» — 38,8 см.

Каждое сообщение, ответ и показанная работа помогают ему расти.
```

`/tree` reports the same three things as the morning post — total height, yesterday's
growth, and yesterday's top three — through the same `_contributor_lines`, so the two
cannot drift apart. It uses the **live** total, today included, because it answers "how
are we doing right now"; the morning post stays on the recorded-only total, since a total
that had jumped by an unexplained amount would contradict the growth figure right above
it. The two can therefore differ slightly, by design. The reply self-deletes like every other
stats reply; the standing announcement of the tree is the 10:00 post.

The morning post reports **yesterday**, a closed and recorded day: at 10:00 today's own numbers are
three hours old and would make the growth figure meaningless. The loop also checks once
on startup, so a process that was down at 10:00 still posts when it returns — a per-chat
marker keeps that from double-posting. That startup check does nothing before the hour
itself: without the guard, deploying at 05:00 would plant the tree at 05:00 rather than
at the 10:00 it was promised for.

`DAILY_ADVICE` holds **120** lines covering painting, 3D printing, creative work,
curiosity, being inspired and inspiring others, not drowning in a backlog, being social
online and in person, time outdoors, and the workbench itself. Picked **by date**, not at
random, so everybody sees the same line on the same morning and a restart cannot change it
halfway through — 120 entries means no repeat for four months.

The digest queue carries a parse mode alongside the text: the tree post is HTML (and
escapes names itself), the procrastinator call-out stays plain text (it embeds raw display
names). Sending either with the other's mode would print tags verbatim or have Telegram
reject the message.

### `/preview` — looking at a scheduled post before the chat does

`preview.py`. The tree posts were the only ones nobody could see in advance: the morning
digest fires from a scheduler, the planting happens once in the lifetime of the chat, and
the nobody-turned-up variant needs a roll call nobody signed up for to reproduce. `/preview`
in the bot's DM (a hidden, unadvertised command) renders a curated set from fixed sample
data.

Without an argument it draws exactly six buttons: **Приглашение**, **Перекличка в
10:00**, **Утренний пост**, **Обычный**, **Значок** and **Отправить тест**.
`/preview rollcall` sends the roll call directly. The five DM previews are
**pure** — fixed sample cast, fixed numbers, no stats store, no clock beyond the day
passed in — and every builder calls the same formatter the scheduler calls, so a preview
cannot drift from the real thing. `/stat`, `/top`, `/shop` and the cabinet are already
one command away.

`/preview test_button` is the exception: it posts a neutral **Тестовый текст** with a
**Нажмите сюда** button to the real chat. Each member who presses is persisted once in a
test-only list; it never opens a planting, changes the tree, or awards a badge. The DM
receipt has **📋 Опубликовать список нажавших**, which posts the current names to the
group, and **🗑 Удалить из чата**, which removes the test and closes its list. Sending a
new test starts a fresh list.

Sample planting buttons in the DM carry their own callback payload, separate from both
the menu's post-to-the-chat action and the recording test button. Pressing a visual sample
still just says "это тест": it neither posts anything nor adds the presser to a list.

#### Inline buttons must answer before they work

Every callback handler answers `answerCallbackQuery` **first**, before anything that can
block, and reports refusals and errors as a DM instead of a toast. That is a fair trade for
a button that always responds.

The rule exists because the preview buttons once didn't. They resolved the group chat
before answering, and resolving goes through the Telethon session whenever `known_chat_ids`
misses — which is every press in a DM until the bot has seen a live group message. A
Telethon session that cannot connect **does not raise; it waits**, retrying underneath,
with no timeout anywhere. The handler sat on that await, `answerCallbackQuery` was never
reached, and the button stayed lit up forever with nothing in the log to explain it.

Every interactive Telethon call is now bounded by `CHAT_RESOLVE_TIMEOUT_SECONDS` (10 s —
longer than a healthy resolve, shorter than a member's patience), so a sick session degrades
to a message rather than a hang. `_resolve_chat_id` already returned `None` on failure, so a
timeout fits its existing contract exactly.

### Coins, the shop, and anti-farming

Coins are a **real ledger** (`economy.py`), not the derived `xp // 10` display they used
to be. The earned half is still derived, and only what cannot be derived is stored:

```text
balance = coins_for_xp(xp) + bonus + received - spent
```

That means existing members were grandfathered automatically — on the first run `spent`
is 0 for everyone, so the opening balance is exactly the number `/stat` had been showing
all along. No migration script had to be right once. Balance is clamped at zero, because
`/deletepokras` can remove 200 XP (20 coins) that may already have been spent.

Commands (any tracked chat):

- `/coins` — balance and banked streak freezes
- `/shop` — catalogue, marked ✅ affordable / 🔒 too expensive / ⏳ on cooldown
- `/buy title <текст>` — purchase

Administrator commands, none of them advertised in the menu:

| command | where | what |
|---|---|---|
| `/посадить_семечко`, `/plant` | chat or DM | open the planting ceremony |
| `/preview [id]` | DM | look at a scheduled post before the chat does |
| `/replant` | DM | re-post the planting announcement, start the tree over |
| `/badgeadmin [-] @user` | DM | delegate custom-badge rights |
| `/badge` | DM | create, award and remove custom badges |
| `/weekwinner`, `/deletepokras` | DM | weekly winner badge; remove a figurine credit |

### Menu button and the fallback menu

The bot publishes its command list to Telegram at startup (`setMyCommands`), so the
client shows a tappable ☰ **Menu** next to the input field and nobody has to know a
command exists. Two scopes: DMs get `/cabinet /stat /top /shop /coins /tree`, groups get
`/stat /topall /toppokras /tree`. Wallet actions belong in the DM where a balance isn't public,
and `/cabinet` is absent from the group menu on purpose — it only works in a DM, so a
group button for it would just answer "напиши мне в личку".

The two aliases are spelled without a space because Telegram only accepts `[a-z0-9_]` in
a registered command name: **`/top all` cannot be a menu entry at all**. Both spellings
work when typed — `/top all` = `/topall`, `/top pokras` = `/toppokras` = `/stat pokras`.
The procrastinator list is capped at `PROCRASTINATOR_LIST_SIZE` (10) names, on demand and
in the automatic digest alike: it is a public call-out, and past about ten names it stops
reading as a nudge and starts reading as a wall. Admin-only commands — `/badge`, `/weekwinner`, `/deletepokras`,
`/badgeadmin`, `/replant`, `/preview`, and the two planting spellings — are deliberately
**not** advertised: putting them in front of all 190 members would invite a wave of "нужны
права администратора". Registration is best-effort: the bot starts fine without a menu.

Because `_match_allowed_chat` never matches a private chat, `/stat`, `/top`, `/shop` and
`/coins` used to be silent no-ops in a DM. They now fall back to the configured home chat
(`_stats_entry_for`), the same way `/cabinet` and the summary pipeline already did — a
published menu must not contain commands that do nothing where it is published.

**Any unhandled DM gets the menu back.** After every specific handler has declined —
commands, summary keywords, the joke trigger, both force-reply flows — a private message
the bot has no answer for is replied to with the cabinet menu instead of silence. It never
fires in a group, stays quiet while somebody is mid-way through a force-reply step
(another member's pending flow doesn't mute you), and a burst of messages produces one
menu rather than one each (`MENU_FALLBACK_COOLDOWN_SECONDS`, 60s; set 0 to answer every
message). Somebody the stats don't know yet gets a short welcome instead of six buttons
leading to six empty screens.

### Личный кабинет (`/cabinet`)

`/cabinet` in the bot's DM opens a button-driven personal cabinet (`cabinet.py`). Sent in
a group it just points the member at the DM — it shows one person's balance and offers
buttons that spend their coins, neither of which belongs in a group.

```text
👤 Личный кабинет

Леонид Уросов

🧩 🗣️ Голос чата 15  ▓▓▓▓▓▓▓▓▓░
🪙 Монеты: 841
📈 Место в рейтинге: 4 из 191
🔥 Серия: 6 дней
❄️ Заморозок в запасе: 1

[📊 Статистика] [🏪 Магазин]
[🎨 Мои работы] [🏅 Значки]
[✏️ Титул]      [💸 Перевод]
[🔄 Обновить]
```

Sections navigate **in place** by editing the same message (`editMessageText`), so the DM
never fills up with dead menus, and every leaf screen carries a ◀️ Назад button. Buying
from the shop is one tap; the two actions that need free text — setting a title, sending
coins — open a force-reply prompt and only debit once the reply arrives.

- 📊 **Статистика** — the exact `/stat` card the group sees, so the cabinet never becomes
  a second, subtly different source of truth
- 🏪 **Магазин** — one button per item, with the same ✅/🔒/⏳ marks
- 🎨 **Мои работы** — showcase links plus up to 30 `#япокрасил` works, one per line.
  ✏️ Переименовать renames one by position (`3 Дредноут`, up to 32 chars; a bare number
  clears it). 🗑 Удалить removes one of your own, behind a confirmation — it writes a
  permanent tombstone, costs 200 XP and a figurine, and can drop a level or a badge with
  it, so it is never a single tap. Both the name and the confirm button carry the work's
  **message_id**, not its position: deleting compacts the numbering, so a position could
  point at a different work by the time the second tap arrives. The confirm handler also
  checks the message_id belongs to that member, so a hand-crafted callback cannot delete
  somebody else's work.
- 🏅 **Значки** — admin-granted badges in their own section **first** (split on
  `Badge.custom`, since those are the only ones somebody chose to give you), then earned
  ones, then `📦 Открыто: N из M`. The denominator counts every tier individually (all
  three painting medals, not just the highest shown), plus the 8 chat-level tiers, the 7
  painting ranks, and however many custom badges the chat has defined.
- ✏️ **Титул** — the one force-reply purchase flow

Each button carries its owner's user id inside its `callback_data`, so a forwarded menu is
inert (`Это чужой кабинет.`) and navigation keeps working across a process restart —
unlike the admin `/badge` flows, only the two text-entry prompts hold server-side state.
Views are rendered as HTML with every user-controlled string escaped.

**Render cost.** A button press must not wait on Telegram. Two caches keep it cheap:

- `_CABINET_CHAT_REF_CACHE` — the group's chat id and `@username`, needed only to build
  `t.me` links, resolved once per process instead of twice per press. A failed resolution
  is not cached, so a transient outage doesn't permanently break links.
- `_CABINET_CONTEXT_CACHE` — the resolved member, XP and rank, for
  `CABINET_CONTEXT_TTL_SECONDS` (45s) per person. The underlying `resolve_stat_target`
  re-reads every recorded day file (~70 ms at 60 days × 190 members) and can refetch
  today's transcript from Telegram, which is far too much to repeat per tap.

Balances, titles and streak freezes are deliberately **not** cached — every view reads
them straight from the ledger, so a purchase shows up immediately rather than 45 seconds
later. Screens with no links (main, shop, title, send, badges) resolve no chat entity at
all; only Статистика and Мои работы do.

The shop sells one thing:

| Item | Price | Effect |
|---|---|---|
| `title` Свой титул | 400 | Custom title under your name in `/stat` and the cabinet, 30 days |

The roast, the work critique and the streak freeze were removed from the catalogue;
their delivery code and the freeze machinery are left in place, so re-listing any of them
is adding one `ShopItem` back — the same "disabled, not removed" convention the roast
trigger and the XP cooldown already follow.

Member-to-member transfers were removed too. **That took the economy's only always-on
sink with it**: transfers used to burn 10% of every gift, and now the sole drain is a
400-coin title every 30 days against ~1,000 coins a month for an active member. Balances
will grow. `received` is still read by `balance()` so any ledger written while transfers
existed keeps computing the same number; nothing can add to it any more.

If delivery of a purchase fails the coins are refunded, so a debit and its effect are
never left half-applied.

**Note on reach:** because coins track XP, the economy is only meaningful for roughly the
top quarter of the chat. The median member earns ~3 coins/week and will not realistically
buy anything. That is a property of the curve, not the prices.

**Anti-farming.** Per-day ceilings on the scored counters — 1,500 words, 25 media, 100
replies — applied when a day is *computed*, so they never reach back and reprice an
already-recorded day. Measured against 1,579 real person-days these bite 30, 67 and 48
days respectively, all genuine outliers (the worst real day was 11,025 words from one
person). `messages`, `chars`, active days and the hour histogram are never capped, since
they describe what actually happened.

A per-message XP cooldown is implemented but **ships disabled** (`XP_MESSAGE_COOLDOWN_
SECONDS = 0`). Measured against this chat's own 62k cached messages, the standard 30–60s
advice suppressed 42–50% of all media, because painters post several angles of one model
back to back — it is an XP cut aimed at the most engaged members, not an anti-farming
measure. Setting the constant non-zero re-enables it.

The activity block stays compact and uses dot-separated thousands:

```text
🛠️ Рабочее место: ссылка
💎 Моя лучшая: ссылка
Фигурок: 12 (#япокрасил)
Активных дней: 96 (🔥 Серия: 11 дней)
💬 Сообщений: 1.842 (19.2 в день)
```

The streak note is hidden when the current streak is zero.

The two showcase lines link straight to the person's own post and sit immediately above
`Фигурок:`:

- `💎 Моя лучшая` — `#моялучшая`
- `🛠️ Рабочее место` — `#рабочееместо` or `#рабочее_место`; Telegram treats `_` as part
  of a hashtag, so both spellings are matched separately

Both require an attached photo or video, exactly like `#япокрасил`, so a text-only
message that merely mentions the tag never becomes somebody's link. Each line is omitted
entirely when that person has no such post. Only the **newest** post of each tag is
linked, since both describe a current state that a later post supersedes — the full
history is still stored, so that display choice can be changed later without a re-scan.
Neither tag awards XP or a badge, and neither is covered by `/deletepokras` (that command
remains specific to `#япокрасил` figurine credit).
The name, progression, and activity sections are separated by blank lines. The last
activity timestamp is intentionally omitted, and badges are rendered two per row.

A new **painting rank** is announced once:

```text
@user получил новое звание «⚪ Ученик грунта»! 🎉🎊🥳
```

Chat levels are tracked but deliberately **not** announced: on the seasonal curve they
come round again every quarter for the same handful of people, which turns the chat into
a promotion feed, and the level is always visible in `/stat` and the cabinet. The
watermark is still maintained, so restoring the announcement is two lines and needs no
migration.

The last observed level is persisted per chat, so a promotion is announced only once
across `/stat` calls and process restarts. Existing users are silently baselined when
this feature is first deployed; only later promotions generate announcements. Level
checks run during `/stat` and the daily stats rollover.

Automatic badges are derived from production counters and hashtag activity. Only the
highest earned painting medal is shown:

- 🎨 Я покрасил 1 — 1 painted figurine
- 🥉 Я покрасил 2 — 5
- 🥈 Я покрасил 3 — 10
- 🥇 Я покрасил 4 — 25
- 💎 Я покрасил 5 — 50

  Numbered **ascending** (1 = first work, 5 = fifty), unlike the streak and night-shift
  families where I is still the best — with five steps, "IV" gives no hint whether it
  beats "II".
- 🦄 Я не пидор — post `#янепидор`
- 🎪 Участник Недельного конкурса ×N — post `#итогинедели`; several posts by the
  same person in one Monday–Sunday ISO week count once
- 💯 Сотня — 100 messages
- 📣 Голос чата — 1,000 messages
- 🖼️ Галерея — 25 photo/video messages
- 📅 Завсегдатай — 30 active days
- 🔥 Не остановить 1 / 2 / 3 — a longest historical streak of 7 / 14 / 30 days
- 🦉 Ночная смена 1 / 2 / 3 — 50 / 250 / 1,000 messages between 00:00 and 05:59

  All tier families now count **upward**: 1 is the easiest step, the highest number
  the hardest.

Painting medals, message-count badges, streak badges, and night-shift badges are upgrade
families: `/stat` displays only the highest unlocked badge in each family. For example,
`📣 Голос чата` replaces `💯 Сотня` at 1,000 messages instead of appearing beside it.

Custom badges are rendered **first**, in their own `✨ Уникальные значки` block, above the
automatic ones — they are the only badges somebody chose to give this person, and mixed
into a dozen automatic counters that is exactly what gets lost. The split is on
`Badge.custom`, so a weekly-contest win (assigned by an administrator, but *won*) stays
with the earned ones.

The last line of `/stat` is a `t.me/<bot>?start=cabinet` deep link — one tap opens the
member's cabinet instead of dropping them into an empty DM where they would still have to
know a command. `/start` (with or without the payload) opens the cabinet too. The link is
omitted entirely when no bot username is available, which is exactly the case where
`listener.py` answers `/stat` itself and there is no cabinet to link to.

Badges appear near the end of `/stat`, immediately before the complete tracked work
history. Every work is a compact clickable entry (newest first) with no display cap, showing
`номер. Название` once it has been named in the cabinet and a bare number until then. The
number always stays visible even for a named work: `/deletepokras` takes the number shown
here as its argument, so replacing it with a name would leave an administrator nothing to
point at. No new message schema or history fetch is needed for
automatic badges.

Custom badges are created and awarded with `/badge` in a private chat with the bot.

Who may do that: Telegram administrators of the home chat, the hardcoded delegates in
`PRIVILEGED_MANAGEMENT_USERNAMES`, and anybody an administrator delegates at runtime:

```text
/badgeadmin              list current delegates
/badgeadmin @username    grant
/badgeadmin - @username  revoke
```

`/badgeadmin` is administrators-only and DM-only — a delegate can award badges, but not
appoint further delegates. A delegate sees a **🛠️ Выдать значок участнику** button in
their `/cabinet`, which opens the same `/badge` menu rather than a second implementation.
That button decides only what is *drawn*; the callback re-verifies permission before
acting, so a menu left open after a revoke cannot still hand out badges.

`/badge` itself: The bot shows two inline options:

- **Создать значок** asks for `<emoji> <name>`, for example `🎯 Меткий глаз`.
- **Выдать значок** shows the chat's saved custom badges, then asks for the recipient's
  exact `@username`.
- **Забрать у участника** takes one badge from one member, leaving the definition alone.
  Not announced in the group: an award is good news worth sharing, having one taken away
  is not something to publish about somebody.
- **Удалить значок совсем** deletes the definition *and* every assignment of it, behind a
  confirmation that spells out how many members currently hold it. Assignments are
  cleared rather than left dangling — a leftover would be invisible but would still count
  towards somebody's collection total and would come back if the id were reused.

Custom definitions and assignments are persisted per chat under the existing stats
cache. Awarding the same badge to the same member twice is idempotent.

A **new** award is announced in the group chat:

```text
@user получил уникальный значок: 🎯 Меткий глаз
```

Only on a genuinely new award — re-running the flow must not post it again. The recipient
is named by `@username` so they are actually notified, falling back to their display name
when they have none. Best-effort: the badge is already recorded by the time this sends, so
a failed announcement costs the message, never the badge. The menu and its
force-reply steps expire after ten minutes and remain bound to the administrator who
opened them. The bot verifies that the person using the DM menu is still an
administrator of the configured home chat. The explicitly delegated
`@sultan_kembayev` account has the same DM-management permission without requiring
group administrator status. `/badge` is silently ignored in groups.

The weekly winner is assigned separately by a chat administrator in the bot's private
chat, using the contest's sequence number rather than the calendar week:

```text
/weekwinner 1 @username
```

Only one winner can occupy each numbered week. Repeating the same assignment is
idempotent; trying to give that week to somebody else is refused. `/stat` displays
`🏆 Победитель Недельного Конкурса ×N`, where N is that person's number of winning weeks.
`/weekwinner` is silently ignored in groups.

If a counted `#япокрасил` Telegram post was deleted, an administrator can remove its
stale work link and figurine credit from the bot's private chat:

```text
/deletepokras @username 1
```

The final argument is the clickable work number currently shown in `/stat`; number 1 is
the newest work. Removing it also removes one figurine and its 200 XP, then the remaining
work numbers are compacted. A persistent tombstone prevents a stale transcript cache
from restoring the deleted submission. `/deletepokras` is silently ignored in groups.

On startup, the normal recent-day stats catch-up backfills the new hashtag fields from
the raw transcript cache without recomputing or changing anybody's historical XP. That
scan covers the last 30 days (`HASHTAG_BADGE_BACKFILL_DAYS`) and only revisits days that
already have stats. Anything older than the window keeps whatever it was recorded with —
a hashtag introduced before then is only tracked from the window's start onward.

## Model choice

`config.py` defines `RECOMMENDED_MODELS`, curated as of July 2026: `gpt-5.4-mini`
(default -- fast, cheap, a big step up from the old `gpt-4o-mini`), `gpt-5.5` (flagship,
best quality, similar latency to 5.4), `gpt-5.4-nano` (fastest/cheapest, fine for quiet
chats), plus `gpt-5`/`gpt-5-mini`/`gpt-5-nano` and the legacy `gpt-4o`/`gpt-4o-mini`.
Set `OPENAI_MODEL` in `.env`, pass `--model` on the CLI, or pick from the GUI's dropdown
(which also accepts typing in anything not on the list, for whenever this list goes
stale).

## Caching: the raw transcript, not the answer

The application timezone comes from `APP_TIMEZONE` and defaults to `Europe/Moscow`,
independently of the host machine's timezone. New caches are stored below a
timezone-specific subdirectory (for example `cache/transcripts/Europe_Moscow/`), so
pre-existing caches made with London calendar-day boundaries are not overwritten or
mixed into Moscow days.

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

The router receives the requester's username and display name, which lets it understand
first-person requests such as `/summary расскажи обо мне` without a rigid phrase list.
The final answering model also receives the exact original Telegram message—not only the
router's cleaned interpretation—and is told to prioritize the user's actual wording and
treat first-person pronouns relative to that sender. When the router selects the
requester, the listener confirms that identity using Telegram's numeric sender ID.

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
   warning above), `LISTENER_TRIGGER_KEYWORDS`, `SUMMARY_QUEUE_DELAY_SECONDS`,
   `ROAST_TRIGGER_KEYWORDS`, `ROAST_LOOKBACK_DAYS`, `TELEGRAM_BOT_TOKEN` (if replies
   should come from a bot account instead of this one -- see above), `JOKE_ENABLED` and
   the other `JOKE_*` vars if you also want the occasional unprompted remark (off by
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
- **👍 to delete.** Reacting with a thumbs-up (from your own account) on any message the
  bot or your account sent -- a summary, joke, `/stat`/`/top` reply, whatever -- deletes
  it almost immediately, as a one-tap cleanup shortcut. It never touches other people's
  messages, even though your account may have delete rights over the whole chat.
