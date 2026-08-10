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

### Self-deleting replies take the command with them

On-demand replies clean themselves up so a chat for posting work doesn't turn into a
scrollback of lookups. **The command that asked for the reply is deleted along with it**,
at the same moment — a reply disappearing on its own would otherwise leave the `/stat`
line behind, quoting an answer that no longer exists.

The timers (`listener.py`, top of file):

| constant | delay | applies to |
| --- | --- | --- |
| `STATS_DELETE_AFTER` | 300 s (5 min) | `/top`, `/stat`, `/tree`, `/shop`, `/coins`, `/buy`, and their error notices |
| `ERROR_DELETE_AFTER` | 10 s | short rejection notices, such as the one-day limit |
| `BLOCKED_FILE_NOTICE_DELETE_AFTER` | 30 s | the "files only in DMs" notice left after a deleted attachment |
| `DISMISS_DELETE_AFTER` | 1 s | a 👍 on something the bot sent — a "get rid of this" tap |

Two things are deliberately left out of the sweep:

- **Scheduled posts never self-delete** — the 10:00 morning digest, the planting
  announcement, and level-up announcements stay in the chat. They are the standing record;
  a test in `tests/test_tree.py` guards this.
- **Dismissals pass no command** — a 👍 reaction is not a message of the user's, so there
  is nothing to take with it, and guessing would delete something unrelated.

Deleting someone else's message needs delete rights, so **the bot has to be an admin** in
the chat for the command half of the sweep to work. Without them, `deleteMessage` fails,
`bot_api.delete_message` swallows the error, and the bot removes only its own reply —
exactly the old behavior, never a crashed handler.

### Archives and 3D models are deleted on sight

The chat is for finished work, not for passing files around, so an attachment whose
filename ends in `.zip`, `.7z`, `.rar`, `.stl`, `.obj` or `.glb`
(`BLOCKED_FILE_EXTENSIONS`, top of `listener.py`) is deleted the moment it arrives, and
the bot posts one line naming the sender:

> @user, пересылка файлов разрешена только в личке. Спасибо за понимание.

Details worth knowing:

- **The notice self-deletes after 30 seconds** (`BLOCKED_FILE_NOTICE_DELETE_AFTER`) —
  long enough for the sender to read why their file went, short enough that the rule
  doesn't pile up in the chat one copy at a time. It takes no command with it the way
  `/stat` replies do: the message that prompted it is the file, already deleted.
- **Groups named in `LISTENER_ALLOWED_CHATS` only.** Never in a DM (the notice tells
  people to use one), and never in some other chat your account happens to be in.
- **One notice per album.** Ten `.stl` files dragged in at once arrive as ten messages;
  all ten go, the sender is told once.
- **Filename, not mime type.** Telegram hands most of these over as a generic
  `application/octet-stream`, so the name carried on the document is the only thing that
  tells a `.stl` from any other blob. Compressed photos and videos have no filename at all
  and can never match, so a `#япокрасил` post is untouched.
- **The bot must be an admin with delete rights.** Your personal session is the one that
  sees every message and spots the file, but the delete and the notice are handed to the
  bot account (`file_block_queue`), same split as the figurine reactions. Without
  those rights `deleteMessage` fails silently and only the notice appears.

`tests/test_blocked_files.py` pins which attachments match and how the sender is
addressed (an `@username` when there is one, a `tg://user` mention link when there isn't).

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

**Репутация — standing.** Tiers: Пока тихо → 🌿 Замеченный → 👏 Уважаемый → 🤝 Опора чата →
🏅 Легенда сообщества. Four inputs:

| source | rate |
| --- | --- |
| weekly contest win | **10** each |
| administrator-awarded custom badge | **5** each |
| coins *received* from another member | **1** per 20 |
| earned-badge **level** held | **1** each |

The first three are peer-granted and cannot be moved by posting — that is the anti-grind
core, and it still is. The fourth is the one self-earned input, added so a member nobody
has handed anything yet still has a reputation that moves.

A badge is worth **one point per level**, not one point per badge: a five-level family is
five points at the top. `🏅 Я покрасил 5` means levels 1–5 are all unlocked, so it scores
5 — "a point per medal" and "a point per level" are the same number here, not two rules to
combine. The medal total is capped at **17**: painting 5, messages 2, streak 3, night
shift 3, and one each for 🖼️ Галерея, 📅 Завсегдатай, 🦄 Я не пидор and 🎪 Участник
Недельного конкурса. That ceiling is deliberately below two contest wins — grinding the
whole collection can never outrank being valued by the chat.

Custom badges and contest wins are **excluded** from the per-level count: they already
score 5 and 10, and adding a point would pay twice for one medal.

Reputation is **derived, never stored** (`economy.reputation_for`) — it is recomputed from
the badge/contest/ledger stores on every `/stat`. Changing a rate therefore updates
everybody at once on deploy, with no migration or backfill step.

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
🪴 Доброе утро, ЕПХ-чане!

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

The greeting carries the **current stage's** emoji rather than a fixed 🌳: for the first
months of a three-year climb a mature tree at the top of the post quietly contradicts the
"🌰 Семечко" two lines below it.

#### One picture per stage

The morning post goes out as a **photo with the text as its caption** whenever the current
stage has a picture in `assets/tree_stages/`. The file name is the stage's slug from
`TREE_STAGES` (`01_seed`, `02_sprout`, … `13_legendary`), the extension any of `.png`,
`.jpg`, `.jpeg`, `.webp`, and the match is case-insensitive — a file saved as `SEED.JPG`
resolves on Windows and would silently miss on the Linux host. Uploading them is a manual
job; `assets/tree_stages/README.md` is the list.

Every part of this is optional and every failure degrades to the post that was there
before: no file for the current stage, a caption over Telegram's 1024-character limit, or
an upload Telegram rejects, and the day's post goes out as plain text. What is *not*
allowed to be silent is a missing file — the startup log names every slug with nothing
behind it, and `/preview stages` lists all thirteen with ✅/⬜ against what actually
reached the deployment.

The digest queue carries a parse mode and an optional image path alongside the text: the
tree post is HTML (and escapes names itself), the procrastinator call-out stays plain text
(it embeds raw display names). Sending either with the other's mode would print tags
verbatim or have Telegram reject the message.

The picture is uploaded fresh each morning (`sendPhoto` as multipart form data, not a
`file_id`): these files ship with the code and Telegram has never seen them, and once a
day a few hundred kilobytes is not worth a file_id cache that would have to survive a
redeploy that changed the picture.

### `/preview` — looking at a scheduled post before the chat does

`preview.py`. The tree posts were the only ones nobody could see in advance: the morning
digest fires from a scheduler, the planting happens once in the lifetime of the chat, and
the nobody-turned-up variant needs a roll call nobody signed up for to reproduce. `/preview`
in the bot's DM (a hidden, unadvertised command) renders a curated set from fixed sample
data.

Without an argument it draws exactly seven buttons: **Приглашение**, **Перекличка в
10:00**, **Утренний пост**, **Обычный**, **Значок**, **Картинки стадий** and **Отправить
тест**. `/preview rollcall` sends the roll call directly. The DM previews are
**pure** — fixed sample cast, fixed numbers, no stats store, no clock beyond the day
passed in — and every builder calls the same formatter the scheduler calls, so a preview
cannot drift from the real thing. `/stat`, `/top`, `/shop` and the cabinet are already
one command away.

**Картинки стадий** is the one preview that reads the disk: it lists all thirteen stages
with ✅/⬜ for whether that stage's picture is uploaded. Deliberately not pure, because a
folder on somebody's laptop cannot answer the only question worth asking here — whether the
file made it into the running deployment. **Утренний пост** likewise arrives as a photo
once the sample stage's picture exists, so an upload can be looked at before 10:00 rather
than after.

`/preview test_button` is the exception: it posts a neutral **Тестовый текст** with a
**Нажмите сюда** button to the real chat. Each member who presses is persisted once in a
test-only list; it never opens a planting, changes the tree, or awards a badge. The DM
receipt has **📋 Опубликовать список нажавших**, which posts the current names to the
group, and **🗑 Удалить из чата**, which removes the test and closes its list. Sending a
new test starts a fresh list.

Sample planting buttons in the DM carry their own callback payload, separate from both
the menu's post-to-the-chat action and the recording test button. Pressing a visual sample
still just says "это тест": it neither posts anything nor adds the presser to a list.

### `/buttons` — posts with up to five live counters

`/buttons` is an administrator-only constructor in the bot's DM. It asks for the message
text, lets the administrator choose **from one to five buttons**, asks for each button's label,
and then offers an optional photo. The final preview has **✅ Отправить в чат** and, once
published, that control becomes **🗑 Удалить из чата**.

Each member can make **one choice per post**. Repeated taps — on the same button or a
different one — do not change any counter. The post is edited in place at most once every
**three seconds**, so a busy burst produces one counter refresh rather than one Telegram
edit per person. Photo posts use the same cycle by editing their caption. Published post
state, including who already voted, lives on the data volume, so buttons continue
counting correctly after a restart; only an unfinished constructor is discarded.

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

### `/poker` — a table for up to ten, run by the «Диллер»

Техасский холдем in the group chat (`poker.py`). One table per chat at a time, opened with
`/poker` (or `/покер`) by a member holding the **🃏 Диллер** badge. Nobody else can open
one, and Telegram administrator status alone is not enough.

The badge is created by the **bot itself** at startup with a fixed id (`dealer`), the same
way `ensure_founder_badge` handles the planting badge and exempt from `MAX_CUSTOM_BADGES`
for the same reason — a chat that had filled its badge budget would otherwise have a
`/poker` nobody in it could ever use. An administrator therefore only has to *give* it
(`/badgeadmin` → 🎁 Выдать значок), never invent it. Holding it is checked by that id **or**
by name (`диллер`/`дилер`, case-insensitive), so a chat that hand-made its own badge before
this existed keeps working.

The table posts **🃏 Кто играет?** with a join button. Each press seats one member and
edits the message to add them to the list; pressing again answers "ты уже за столом" and
seats nobody twice. **▶️ Начать игру** works only for the member who opened *this* table.

Chips are **session chips**: everybody starts on 1000, blinds are 10/20, and nothing here
reads or writes `economy.py`. No hand can move a real coin balance, which is what makes it
safe to abandon a table mid-hand. They vanish when the table closes.

Betting is **fixed-limit** — one bet size per street — so every decision is a button and
nobody types a number into a group chat. The keyboard is contextual and shows the amounts:

```text
Ход: @nalumurrr — до колла 20

[ Колл 20 ]  [ Ставка 40 ]
[ Ва-банк 980 ]  [ Пас ]
[ 🛑 Завершить стол ]
```

Check replaces call when there is nothing to call, and a raise that would take the whole
stack is offered as **Ва-банк** instead, so no two buttons ever mean the same thing.

Hole cards go to each player's **DM**, which is why joining is what checks that the bot can
write to them: a member who has never pressed Start is told to, and is not seated. Finding
that out at the deal instead would mean somebody sitting through a hand blind.

Everybody sees everybody's buttons — Telegram has no per-viewer keyboards in a group — so
the wrong person pressing is the normal case, not an error case. It is answered with a
toast on that person's own screen (*"Сейчас ход: @X"*, *"Эта раздача уже сыграна"*) and
changes nothing at all. Every action button carries its hand number and street, so a press
on a scrolled-back message is recognised and refused rather than applied to the current
hand.

Each street gets its **own message** rather than one edited all hand: an edit is silent,
and a flop that arrives without anything appearing in the chat is a flop nobody notices is
their turn at. The finished street's buttons are taken away (`editMessageReplyMarkup`,
buttons only — never the text). After the showdown the dealer gets **🔄 Следующая раздача**
and **🛑 Завершить стол**; players who ran out of chips leave before the next deal, and the
next-hand button disappears once fewer than two remain.

**Side pots are real.** An all-in wins only what it covered, a folded player's chips stay
in the pot, an uncalled bet comes back to whoever made it, and a split pot's odd chip goes
to the first winner left of the button. The property test that matters most plays 25 whole
random hands at four unequal stacks and asserts that chips are conserved: a bug in the pot
maths is a bug that invents or destroys somebody's chips.

Table state lives on the data volume, so a redeploy mid-hand does not eat the session. The
dealer can always close the table, and so can a chat administrator — without that escape
hatch a dealer who went to bed would wedge the chat's only table forever. `/poker стоп`
(also `закрыть`, `stop`, `close`) does the same from the command line, because the button
that normally closes a table lives on a message that yesterday's table has long scrolled
past.

#### Where poker breaks the answer-first rule

Two poker callbacks do a Telegram round trip *before* `answerCallbackQuery`, because each
needs the result to decide what the toast should say. **Я в игре** writes the "ты за столом"
DM to check reachability — the answer decides whether the presser is seated at all. **🛑
Завершить стол** asks for the administrator list, but only when the presser is *not* the
table's dealer, so the ordinary case still answers straight from local state.

Both are bounded Bot API calls with an HTTP timeout, not the unbounded Telethon resolve that
made the rule necessary: the spinner can be slow, but it cannot hang forever. Every other
poker button decides from disk and answers immediately.

### `/vote` — voting on the week's #итогинедели posts

A mobile-first voting page (`voting.py`, `vote_web.py`) opened as a **Telegram Mini App**
— a real web page that loads inside Telegram itself, identifying the viewer without a
login. In a group every subcommand's button is a plain link rather than a Mini App button,
since Telegram only allows the latter in a private chat — either straight into the Mini
App via a Direct Link (`VOTE_MINIAPP_SHORT_NAME`, see `/vote chat` below) or into the
bot's DM, where the real Mini App button is waiting. Either way it is opening the page
inside Telegram that gets a signed identity to vote with.

Six distinct things live behind `/vote` (or `/голосование`), kept apart rather than one
page that changes shape depending on who opens it:

- **Bare `/vote`** opens the actual ballot, for **everyone including an administrator** —
  an admin is never forced into moderation just to cast their own vote. For an
  administrator specifically, it's also a status/control panel: current standings (how
  many voted, the top 3 so far) and a button per command — the written-out list of the same
  commands used to sit above them and is gone, since every line of it was a slower way to
  press the button underneath. "Открыть голосование"/"Модерация" open the Mini App directly, while
  "Заявки за эту неделю"/"За прошлую неделю"/"Объявление"/"Картинка итогов"/"Очистить" run the exact
  same code path as typing the
  command (`handle_vote_action_callback` builds a synthetic message and hands it straight
  to `handle_vote_command`, admin/DM check and all, rather than duplicating any of it).

  On the page itself, tapping a thumbnail opens the **reel** — every work in one continuous
  scroll, full width, starting at the one that was tapped — and tapping a photo there goes
  back to the grid, at the entry that was being read. Each photo also carries a **⛶ button
  that opens the lens**: that one photo, full screen, with pinch, double-tap and
  drag-to-pan, capped at 8× fit. The gestures are handled by hand (`touch-action: none`,
  pointer events) rather than left to the browser, because **Telegram's Android WebView
  does not offer the page pinch-zoom that iOS does** — which is why only Android users
  reported the pictures as impossible to examine (2026-08-10). A button rather than a
  gesture on the photo: double-tap is the reflex, and a tap on a photo already means
  "close the reel". Same ⛶ and the same reasoning as the arena's duel view
  (`arena_web.py`), so the bot's two voting systems agree on what "look closer" looks like.
  Telegram's back arrow steps out of the lens first and the reel second.
- **`/vote собрать`** (DM, administrators only) scans the contest week for `#итогинедели`
  posts and adds any that aren't already in the poll.

  **Which week is a choice, and it is two buttons rather than a picker** — "Заявки за эту
  неделю" and "За прошлую неделю" (`/vote собрать` and `/vote собрать прошлая`,
  `_vote_collect_weeks_ago` parses both). The vote for a week is run once that week is
  over, so on a Monday the default window is a few hours old and empty while every work
  worth voting on sits in the week just ended. The previous week's window is closed at
  **both** ends — Monday 00:00 through the following Monday 00:00 — so collecting it never
  drags in what has been posted since; each week's works stay in that week's own poll.
  Collecting also makes the week it collected **the newest** poll (`voting.make_current`),
  which is how `latest_poll` breaks a tie — without it an untouched poll for the week that
  has only just begun would outrank the week just filled.

  **Which week the page opens is a rank, not a timestamp** (`voting.latest_poll`,
  `_ballot_rank`): a poll with **admitted** works outranks one that is merely collected,
  which outranks an **empty** one; `created_at` only breaks ties inside a rank. Both of
  the weaker kinds get written routinely — collecting a week nobody posted in still saves
  that week's file — and on a Monday that is the normal state of the week in progress. The
  rank is what stops a collect from taking the page away from a vote that is running: on
  2026-08-10 last week's poll was open with 15 admitted works and 34 ballots cast when a
  routine "за эту неделю" found one new nomination, and that single pending work moved the
  ballot to a poll with nothing admitted in it. No vote was lost (each week's are in its
  own file), but the ballot showed no candidates until the ordering was fixed. The
  consequence to know about: while a vote is running, a week you collect **stays behind
  it** — the collect reply says so outright — and takes the page once that vote is
  announced or cleared. There is no week picker; one contest is live at a time.

  **Starting a new week carries last week's field over**, minus its top 3: the podium has
  had its week, everything below it runs again, and the reply says how many came across.
  Only works that were **admitted** last week are carried (un-admitting is the only way to
  drop a post from a poll, and a carry-over that ignored it would undo that decision every
  week), and only a top-three place that actually scored retires — a week nobody voted in
  has no podium, so its whole field returns rather than three works being dropped for
  sorting first among the noughts. Carried works arrive **already admitted**, since a human
  admitted them once already; new nominations still start pending, so the moderation screen
  still shows exactly what nobody has ruled on. Their framing (`crops`) and the ballot's
  settings (`max_choices`, `allow_revote`) come too; last week's votes, winner and closed
  flag do not. Their photos are **copied** into the new poll's media directory — the page
  and the export both address a photo as `<poll id>/<file name>`, so a file left behind
  would render as a 404 on every carried card, and copying means clearing either week
  leaves the other intact. This happens only when the week's poll does not exist yet:
  re-collecting a week already under way must not resurrect what the moderator has since
  un-admitted. `voting.CARRY_OVER_SKIP_TOP` is the 3.
- **`/vote перенос`** (DM, administrators only, also the "🔁 Что перенесётся с прошлой
  недели" button) shows what that carry-over *would* bring across — last week's poll, the
  podium that retires with its vote counts, the works that return, and whether the
  carry-over would fire at all (it won't if this week's poll already exists). It is
  strictly a read: no poll is created, nothing is saved, no photo is copied. Checking what
  will happen must not be a way of making it happen, which is also what makes it safe to
  press on a live week. Already-known
  entries are left alone entirely — no re-fetch, no re-resolving who posted them, no
  re-downloading their photos — so re-collecting a poll that already has a dozen entries
  costs only whatever's actually new, and never re-touches what's already been admitted
  or voted on. The tradeoff: a post that gets deleted from the chat after being collected
  stays in the poll (nothing re-checks it) — un-admitting it by hand in
  `/vote выбрать` is the way to drop it.
- **`/vote выбрать`** (DM, administrators only) opens the moderation screen: every
  nomination (not just admitted ones), an "допустить" toggle instead of a vote button, a
  live count **and a proportional bar, relative to the current leader** on each card, and
  the buttons that close the vote or clear it entirely (below). Nothing is shown to
  ordinary voters -- or to an admin on the plain ballot -- until this has admitted it, so
  an unmoderated poll shows an empty page rather than everything anyone posted.
- **`/vote картинка`** (DM, administrators only, also the "🖼 Картинка итогов" button in
  the admin menu) renders the standings as **one tall picture** (`vote_image.py`, Pillow)
  and sends it back as a **file**. Deliberately the same board the Mini App shows — same
  colours, same three columns, same square thumbnail with the author's name and @tag under
  it, ranked by votes with the count in the corner — minus the vote button, which would be
  a lie about what a picture can do. Two rules it does not share with the page: the photo
  is **fitted** into its square rather than cropped to fill it (on the page a crop is a
  link to the full picture; here there is no tap, so an automatic crop would be all anyone
  ever sees) — framing a work by hand is what the cropping page below is for — and there is
  exactly one image however many works there are, it just gets longer, a row at a time.
  `/vote картинка 4` (and the "4 в ряд" button next to it) renders the same board four
  across: the cards keep their size and the picture gets wider, rather than the works
  getting smaller, which is the only reason to want the wider board. 2–6 columns are
  accepted and each non-default count is written to its own file, so rendering three-across
  and then four-across leaves you holding both. Sent as a document, not a photo, because Telegram re-encodes photos and
  refuses anything past 10000px of width+height or a 20:1 side ratio, which a long board
  hits; the file is also kept on disk under `voting/exports/` either way. Only **admitted**
  entries are drawn (it renders `poll.tally()`, the same ranking the page and the
  announcement text use, never re-sorted here). Rendering runs in a worker thread —
  decoding and scaling every photo in the poll is seconds of CPU that would otherwise stall
  every other chat the bot is serving. Fonts: the Docker image installs `fonts-dejavu-core`
  since `python:*-slim` ships none and Pillow has no Cyrillic face of its own;
  `VOTE_IMAGE_FONT`/`VOTE_IMAGE_FONT_BOLD` override the lookup on a host that keeps its
  fonts elsewhere.
- **The "✂️ Кадрировать" button** (DM, administrators only) opens a second Mini App page,
  `/vote/board` (`vote_web.BOARD_HTML`), for framing each work before the export. It shows
  the export's own board — same three columns, same square thumbnails, ranked by votes —
  and tapping any card opens a big editor where the photo is **dragged to pan and pinched
  (or wheel/slider) to zoom** inside its square, with thirds drawn over it. The grid behind
  is the live preview: what is on screen is what renders. Per card there is "Вписать"
  (fit whole, letterboxed) and "Заполнить" (fill the square, i.e. the ballot's own
  `object-fit: cover`), plus "Все: вписать"/"Все: заполнить" for the whole board and a
  3/4 column switch that re-lays the preview exactly as the export will. Then "Выгрузить
  картинку" renders it, sends the file to the admin's DM and offers a link to it.

  A crop is stored (`voting.Poll.crops`) as **a square in the photo's own pixel
  coordinates**, taken after the EXIF rotation both the browser and Pillow apply — that is
  the only reason the page and the renderer agree on what was framed. The square is allowed
  to hang off the edge of the photo, which is how "fit the whole thing" is expressed as a
  crop rather than as a second mode: one representation, one renderer, and a work nobody
  framed renders exactly as it did before cropping existed. Framing survives a
  `/vote собрать` re-collect for the same reason admitting does — it is work somebody did by
  hand. Names get one more pass before they are drawn: the board is rendered with a single
  font and no fallback chain, so emoji and the "fancy" Unicode alphabets people set as
  Telegram names would come out as hollow `.notdef` boxes. `vote_image.legible` NFKC-
  normalises first (𝓐𝓷𝓷𝓪 → Anna, a rescue rather than a deletion) and then drops whatever
  the loaded font genuinely has no glyph for, detected by comparing each character's
  bitmap against the one an unassigned codepoint draws. A name left with nothing promotes
  the `@tag` to its line rather than printing a blank card.
- **`/vote очистить`** (DM, administrators only) deletes the current poll outright --
  entries, votes, admitted flags, downloaded photos, settings, all of it -- behind a
  tap-to-confirm inline button, same as every other irreversible action in this bot. The
  next `/vote собрать` then starts a genuinely fresh poll rather than resetting the old
  one in place. The moderation screen has the same action as a button
  ("🗑 Очистить голосование"), also behind its own confirmation.
- **`/vote chat`** (DM, administrators only) drafts an announcement: asks for the text via
  a force-reply (same convention as every other short text prompt in this bot -- badge
  creation, cabinet's title/coin entry), and then, instead of sending it anywhere
  immediately, asks **where it goes** — "В чат" (the tracked chat itself), "В Папку
  художников" (`VOTE_ANNOUNCE_EXTRA_CHAT`, `@papkahudojnicov` by default), "В оба", or
  "Отмена". That question is the reason this is the one force-reply flow in the bot that
  keeps its entry alive past the reply: the drafted text has to survive in memory
  (`vote_chat_flows`, same 10-minute TTL, restarted when the destination question is
  asked) until a button is pressed, and is dropped only once the announcement is posted or
  cancelled. Each destination is posted to independently, so a bot that has been kicked
  out of one group still gets the announcement into the other, and the reply back to the
  admin names what went where and what failed with the Telegram error attached. The posted
  announcement is **never scheduled for auto-delete** — unlike the stats replies this
  codebase otherwise sweeps away as noise, the whole point of it is to still be there
  tomorrow. Leaving `VOTE_ANNOUNCE_EXTRA_CHAT` blank simply removes the second and third
  buttons rather than offering a destination that could not work.

  The button on that posted announcement is a plain `url`, not a `web_app` one: Telegram
  accepts a Mini App button only in a private chat and rejects one posted to a group. With
  `VOTE_MINIAPP_SHORT_NAME` set (BotFather's `/newapp` Direct Link Mini App short name)
  that url is `https://t.me/<bot>/<short name>?startapp=vote`, which opens the Mini App
  from the group in place; without it the url falls back to the `?start=vote` deep link
  into the DM, where the real `web_app` button is waiting one tap away. The same rule
  governs the button a bare `/vote` leaves in a group.

Which of these opens is decided server-side by a `?mode=admin` marker on the page's own
URL, checked against a real admin lookup on every request (`handle_poll`) -- not by trusting
whichever button the client happened to tap, so the moderation payload (full entry list,
per-entry counts) is never sent to anyone who isn't actually an administrator, however
they ask for it.

**One ballot per person**: the page authenticates via Telegram's signed `initData`
(`voting.verify_init_data`) — an HMAC over the payload Telegram itself attached when it
opened the Mini App, keyed off the bot token — so a vote is tied to a real Telegram user
id the server verified itself, not a cookie or an unauthenticated form. Voting again
replaces the previous ballot rather than adding to it. Who counts as an administrator here
is the same `_can_manage_chat` rule everything else in the bot uses: a Telegram admin of
the tracked chat, a `/badgeadmin`-delegated manager, or a hardcoded name in
`PRIVILEGED_MANAGEMENT_USERNAMES`.

**Closing the vote** (admin-only, "Закрыть голосование и объявить победителя") picks the
top-voted admitted entry (`voting.close_and_announce` — refuses if nothing has any votes
yet) and sends the **top 3** — name, vote count, medal emoji each, plus the winner's photo
and their post's own text — as a message. For now that message goes to whoever closed the
vote, in their own DM with the bot, not into the group; posting the announcement into the
chat itself is a manual copy-paste away until that's wired up directly. The poll records
who won (`Poll.winner_entry_id`) independently of whether the message actually sent, so a
delivery hiccup never loses the result, and re-closing (say, after adjusting which entries
are admitted) recomputes rather than refusing.

**Ballot settings**, set from the moderation screen alongside admitting entries: how many
entries one ballot may name (`max_choices`, unlimited by default) and whether resubmitting
replaces a previous ballot (`allow_revote`, on by default). Both are enforced server-side
in `vote_web.handle_ballot` — a ballot over the cap is rejected outright (400) rather than
silently truncated, and once `allow_revote` is off, a second ballot from the same person is
refused (409) instead of overwriting their first. The page reflects both: it blocks picking
past the cap client-side too (so the rejection is never the first the voter hears of it),
and shows "голос зафиксирован" instead of a vote button once a locked ballot is in.

Requires `WEBAPP_PUBLIC_URL` (a real `https://` domain — Telegram refuses to open a Mini
App over plain http) and, on a host with no persistent disk by default, `DATA_DIR` on a
Volume — see both in `.env.example`. Without `WEBAPP_PUBLIC_URL` set, `/vote` explains
that the voting page isn't configured instead of offering a button that wouldn't open.
Two optional settings shape where announcements go and how their button opens:
`VOTE_ANNOUNCE_EXTRA_CHAT` (the second group `/vote chat` may post to, `@papkahudojnicov`
by default — an `@username` is all Telegram's `sendMessage` needs, so no numeric id lookup
is involved) and `VOTE_MINIAPP_SHORT_NAME` (BotFather's `/newapp` short name, which has to
be created by hand once, pointing at `WEBAPP_PUBLIC_URL` + `/vote`).

### `/vote2` — the second voting system (v2), running beside the first

A **separate** system, not a replacement: `/vote` keeps working exactly as it did, and both
can run in the same week. Instead of a grid where you tick favourites, the arena shows
**two works at a time** and asks which is better — ten duels per voter by default.
`arena_core.py` (pure logic), `arena.py` (storage and session rules), `arena_web.py` (its
own Mini App at the `/arena` route, mounted onto the same server v1 uses via
`create_app(..., attach=...)`).

There is **no "Ничья" button**: it made not choosing the easiest answer on the screen, and a
ballot of ties says nothing. A tie is still a legal *pick* — `record_pick` accepts `TIE` and
`compute_standings` scores it half a point each — because ballots cast while the button
existed name it, and refusing it now would make an old ballot unreadable rather than merely
old.

The logic is a Python port of the reference implementation in [`import/`](import/) — same
pairing, same ranking, same rules — with one substitution: that module identifies a voter
by an invite code, and Telegram already knows who everybody is, so **the Telegram user id
is the code**. There are no codes to hand out or lose.

**Ranking is Bradley-Terry**, fitted by MM iteration and reported on the chess scale (1500
is the field average, +400 ≈ ten times more likely to win). A win counts for more when the
opponent is strong, so the table can rank two works that were never shown against each
other, and every row carries a **margin** — roughly one standard error. `/vote2 итоги` says
outright when first and second are not separated by more than noise, because a rating
without its error bar invites reading a 12-point gap as a result. The fit is
**order-independent by construction**: it refits from the whole vote table every time, so
the same votes in any order give identical output (there is a test; incremental Elo would
break it). Pairing deals in **rounds**, so exposure stays even — an under-exposed work gets
a misleadingly wide margin. `adaptive` mode instead seeds each pair on the work with the
widest error bar and matches it against a similar rating, keeping 30% random as a
corrective and falling back to pure random for the first 15 ballots, when the ratings are
still mostly prior.

Session rules, all enforced in `arena.py`: **one voter, one ballot, for ever**; a finished
ballot **never** reopens; re-entering **resumes** with the same pairs and picks rather than
re-dealing (or a voter could re-roll until they liked their matchups); a submit for any
position other than the one the server has is **returned unchanged**, so a double tap, a
retry or a stale tab cannot count twice.

A mistap is **taken back with «← Назад»**, as many times as needed, all the way to the first
pair — `undo_pick` drops the last entry of the picks list and `position` is derived from its
length, so there is no incremental state to unwind (the fit is order-independent anyway).
The one pair that cannot be taken back is the one that *finished* the ballot: an undo that
reopened a closed ballot would be a reopen under another name.

In the duel itself a work is shown as **all of its photos**, not just the first: a snap
scroller with a count, dots and arrows, and a **⛶ zoom** that opens the picture full screen
with pinch, double-tap and drag-to-pan. The gestures are handled by hand rather than left to
the browser — native pinch and native scroll-snap want opposite `touch-action` values, and an
unhandled pinch inside a Mini App drags the whole app down instead of magnifying anything.
Choosing is still **one tap on the work**; a tap is measured from `pointerdown` (the same
rule `vote_web.py`'s reel uses) so a swipe through the photos never casts a vote.

Somebody who opens the arena again after finishing is told **«Вы уже проголосовали»** rather
than being sent to a refused session, and gets the **top of the table** — picture, name,
rating — from `GET /arena/api/top`. That route is deliberately separate from the
administrators-only `/api/standings` and opens only once *your own* ballot is closed: a
running ranking in front of an unfinished voter is exactly the bias the pairing exists to
avoid, but behind a closed ballot it can no longer reach any of their picks.

Commands mirror `/vote` so knowing one is knowing the other — `/vote2` (duels, plus a
status panel for an administrator), `/vote2 выбрать` (moderation: admit works, pairs per
voter, pairing mode, open/closed), `/vote2 собрать` (scan `#итогинедели` into the arena's
**own** store and media), `/vote2 chat` (draft an announcement for the group), `/vote2 итоги`
(the table), `/vote2 очистить` (delete the arena and nothing else). `/голосование2` is
accepted too.

**This used to be `/arena`,** and that word now belongs to the pet game below. The voting
system underneath is unchanged — only re-spelled. Announcements already sitting in the
group carry a `?start=arena` deep link, so that payload still opens *this*: a button in a
message must not start opening a different feature than the text around it describes.

**What a plain voter sees is one button.** Bare `/vote2` in a DM gives an administrator the
status panel — standings, «Открыть арену», «Модерация», «Собрать», «Взять из v1»,
«Объявление», «Рейтинг», «Очистить» — and gives everybody else «Открыть арену» and nothing
else: no callback buttons, no `?mode=admin` link, not even the top three from the status
block (which is exactly what the Mini App withholds until you have finished voting). The
admin actions are refused twice over anyway — each callback is bound to the one user id it
was built for, and the command it replays re-checks admin status — but a button that only
ever answers "не для тебя" reads as a permission the reader has, so it is not shown at all.
Administrator here means a Telegram admin of the home chat, a runtime delegate, or a
`PRIVILEGED_MANAGEMENT_USERNAMES` name, the same gate the rest of this file uses. In a
group, everybody (administrators included) gets the same `?start=arena` deep link, since a
`web_app` button is private-chat only.

`/vote2 chat` reuses **v1's announcement composer** rather than growing a second one: the
draft is parked in the same `vote_chat_flows` dict, tagged `system: "arena"`, and only the
button on the finished post differs (`_announce_button`). One pending draft per admin across
both systems — they share the force-reply convention, so two live drafts would both be
waiting on a reply and the wrong one could swallow it. An untagged flow still means v1. The
arena's button is always the `?start=arena` deep link and never `VOTE_MINIAPP_SHORT_NAME`: a
Direct Link short name is registered against one url in BotFather, and that one is v1's page.

**What the two systems share is a process, a port, the admin/membership checks and the
announcement composer.**
Separate directory (`DATA_DIR/arena`), separate files, separate photos, separate
moderation, separate commands. `/vote2 импорт` is the only bridge and it runs one way, on
demand, **by copying**: it takes the works v1 has *admitted* into the arena (with their
photos), leaves v1's poll untouched, and they arrive **unadmitted** — this system moderates
for itself, and inheriting an admit decision made for a different vote is exactly the quiet
coupling that turns two systems into one. Clearing either one leaves the other whole;
there are tests for both directions.

### `/arena` — the pet game

A creature you buy, name, dress and level up, and send at other people's creatures. The
third game in the bot and the first one that spends coins on something **permanent** —
which is also why it exists: `economy.py` notes that with one rentable title as the only
drain, balances only ever grew. This is the sink.

**It spends the same coins `/stat` and `/shop` show, not a second currency.** That is the
whole reason chat activity funds the game rather than sitting beside it.

The loop is: buy a **клетка** (100), **приручить** a creature by sending a photo and a name
(50), spend coins on **Сила / Здоровье / Ловкость / Удача** (1–80 each), buy **оружие,
амулет, перчатки, сапоги**, build the **Ферма**, then fight. Opponents are drawn uniformly from every attackable
pet, with unlimited rerolls and no level or combat-power window. A win starts at 5–10 coins
and 100 pet XP, then moves from 75% for farming a pet 3+ levels below to 125% for beating
one 3+ levels above. An attacker who is 7+ levels above the target is stopped by the guard
instead: the attempt is spent, the attacker gets 5 pet XP, and no combat, gold, debit or
drop occurs. **Losing costs 30% of what the winner took**, with no debt. Each creature level
gives **+1 to every stat**, on top of whatever was bought.

The equipment catalogue contains **exactly 500 unique weapons** plus the other slot gear:
75 cursed, 250 common, 120 uncommon, 50 rare and 5 legendary. Each weapon code has at most
one owner in the entire chat; unequipped items can be gifted to another tracked pet or sold
back for a modest amount. Shared shop stock, arena drops and legendary pity all skip codes
already held by anyone in the chat. The daily storefront shows 10 rotating non-cursed
weapons with rarity filters. Cursed weapons only drop in the arena. The collection shows
only chat-wide discoveries and their current owner, never the
catalogue's total size or unknown placeholders. Items can be locked; rare and legendary sales/gifts require
a one-time server confirmation, while gifts require pet level 3 and have a 24-hour sender
cooldown plus an ID/code/timestamp audit trail.
Arena drops automatically fill an empty equipment slot or replace a weaker item in the
same slot; the previous item stays in the bag. Fight-result images show each pet's weapon,
rarity and icon-based bonuses directly under HP, followed by the equipped amulet and its
passive effect, with dividers separating equipment from the base-stat receipt.
The arena keeps an append-only changelog behind the 📰 Обновления button, which shows a red
dot until a member opens it. Entries shipped in `pets_updates.UPDATES` need a deploy;
administrators can append one from Telegram instead with `/arenanews` (or `/аренановости`),
which works in the group or the bot DM and takes a headline on the first line and an
optional body on the rest. Those entries are stored per chat, always sort after the shipped
ones, and are escaped rather than rendered as HTML, so typed markup cannot break the send.
Publishing one restores the red dot for everybody. The command is deliberately absent from
the registered Telegram command menu, like the other admin-only commands.
Administrators can use `/testfight` from the group or the bot DM to pit their pet against
a random existing opponent in the main chat. It runs the normal combat and result-image
pipeline but deliberately records no fight, XP, gold, drop, loss debit or banked-fight use;
the public caption labels it as a test and the message is sent with notifications enabled.
Weapon names use familiar household junk and concrete origins such as «с Авито», «из
гаража» and «на синей изоленте», with one-line descriptions; storefront entries are
separated by blank lines for quick scanning.
The shop's amulet/gloves/boots tabs open that slot's shop shelf — only the items actually
on sale, unpaginated, each with a buy button — rather than the slot's full catalogue. The
catalogue (`slot_view`, reachable from the shelf) mixes ~30 drop-only trophies with the two
purchasable items and sorts owned gear first, so which page the buy button landed on
depended on how much the player already owned; that is what made the shop look like it
sold nothing but weapons.
Shop weapon prices are tied to combat value rather than catalogue position: commons cost
10–20 coins, uncommon weapons roughly 50–70, and the five shop rares 130–155.
Every daily storefront injects an unowned 10-coin starter weapon when its normal rotation
has none, so the six-hour REFERENCE level-1 farm shift (14 coins) always buys one on its
own; a shorter shift pays less and may need a repeat trip or the hourly passive income to
top it up first. Higher rarities remain long-term goals, while resale stays at a low 20%
(rounded down, including below five coins for the starter tier).
The two purchasable accessories in each of the other three slots (amulet, gloves, boots)
price the same way -- previously hand-picked numbers up to 1,100 coins that predated
`shop_price_for_bonuses` and never followed the weapon rebalance; they now cost 10-170
coins depending on the same power-and-rarity formula (see PETS_BALANCE.md 6.0.3).
Rarity weights make a legendary about 0.94% of weapon drops; if an unowned legendary has
not appeared for 500 eligible wins, the next win guarantees one. Aggregate-only economy
telemetry tracks passive minting, sales, gifts, arena gold and drops.

The accessory drop catalogue adds 30 amulets, 30 boots and 30 gloves. Boots and gloves
use ordinary stat trade-offs; every amulet has a separate machine-readable passive shown
in the equipment UI and replayed deterministically from the fight seed. Passives cover
opening, attack, defence, healing and comeback hooks. Two legendary utility effects act
at settlement: Collector raises the item-drop chance from 8% to 10%, while Survivor
keeps 30% of the normal loss penalty. All 90 additions are drop-only. They share the
existing 8% item roll without replacing the five-weapon legendary pity pool.

At startup, the one-time `unique_weapons_202608` migration removes «Швабра на изоленте»
(`w003`) from every old inventory and grants each former owner 100 coins. Other historic
duplicate weapon codes are reduced to one copy, preferring an equipped copy and refunding
the removed copy at purchase price (shop) or resale value (drop).

The **Farm** sends a pet away for a player-chosen **1-8 hour** shift, picked from eight
buttons (four per row) when the run is started. While it is away the pet cannot start a
fight of its own -- but, unlike before, it is no longer immune to attack: any other player
can still find and fight it, same as any other pet. Its 10 levels yield 14–33 coins and
50–95 pet XP at the six-hour reference length; a shorter shift pays a bit less per hour
(0.85x at 1 h), a longer one a bit more (1.15x at 8 h), on top of scaling linearly with the
hours chosen. The item-find chance also scales with hours, from 0.5% at 1 h up to 6% at
8 h (6 h stays at the original 3%), and RARITY now scales with hours too: only 7-8 h shifts
can roll a legendary, and -- now that loot is rolled at settlement rather than reserved up
front -- a farm find can be a weapon, subject to the same chat-wide one-copy rule as every
other weapon. A well improves coins, a sprinkler improves XP, garden beds raise the base
find chance by +5 points at every length, and a tractor improves both rewards; none of them
change how long a shift takes, since that is now the player's choice. A run can also be
recalled early for a "❌ Забрать сейчас" button, which pays only for whole hours actually
worked (a cancel under one hour pays nothing) at THAT shorter length's rate -- quitting
early costs the long-run bonus, it does not just prorate it. Level and building bonuses are
snapshotted when a shift starts (so buying an upgrade mid-shift only affects the next trip),
while gold/XP/loot are rolled once, deterministically, at settlement -- reproducibly on a
retry -- and settled once after restarts, then announced persistently in the owner's bot DM.
The same 10 Farm levels now also passively bank 1–5 coins per complete hour with 24–480
coins of storage. Collection is lazy (opening an arena balance screen settles it), but the
checkpoint and credit share one atomic ledger write, so a retry or restart cannot pay the
same hour twice. The retired separate passive facility is removed from the menu; its old
upgrade costs are refunded once in full at startup.

**Everyone starts with room for 5 fights.** The bank gains **one fight every hour** until
it reaches that capacity. The cage adds up to 4 spaces, every two Farm levels add one (up
to 5), and each qualifying `#япокрасил` adds one space for the next seven calendar days.
Ordinary message volume never changes this. Telegram replays do not duplicate a painting
buff, and `/deletepokras` removes it. The arena always shows the current bank, its maximum
and the countdown to the next +1 fight; when full, it says so instead of showing a reset.

`/pet` prints the card — photo, level, stats, gear, fights and wins — and works **in the
group as well as the DM**, since it is the one screen meant to be shown off. `/arena` itself
is DM-only: every button on it spends the presser's coins, and a menu posted in the group
would put one member's wallet in front of 190 people.

Opponent cards show the creature and combat stats but deliberately omit its computed power
rating. Fight results use the full uncropped pet images and put a red/grey `remaining / max
HP` bar directly below each one, so the margin of victory is visible without reading the
combat transcript.

**The fight is the point, and the fight is written out.** Every blow is a line from a bank
of ~240 variants — dodges where the creature is distracted by a butterfly, crits it did not
see coming, blocks the armour ate. Those lines are the deliverable, so they carry two rules
the tests enforce: **no emoji** (the persona voice), and **no grammatical gender** — names
are user-supplied, so every template is written to read correctly whatever it is handed.
Numerals are the same trap: `92 очков` is wrong Russian, so a damage figure may only be
followed by a word that does not decline.

The maths lives in **one file**, `pets_config.py`, and nothing is duplicated out of it:

```text
cost(L -> L+1) = round(STAT_COST_BASE * L ** STAT_COST_EXPONENT)   # 1 coin -> 189
HP             = BASE_HP     + health   * HP_PER_POINT   * dominance
damage         = BASE_DAMAGE + strength * DAMAGE_PER_POINT * dominance
dodge/crit/armor = MAX * stat / (stat + K)                          # saturating, never 100%
```

Saturating curves rather than linear ones, because a linear chance either does nothing at
level 5 or hits 100% long before level 80. The **dominance bonus** is the asked-for rule: a
stat 30% above the opponent's gives 30% more — compared per stat, once, at the start, and
applied **only to the stat-derived part**, since `BASE_HP` and `BASE_DAMAGE` are a floor
everybody gets rather than a reward for out-scaling somebody.

Tuned against the chat's **measured** earn rate, so three stats to level 80 (~20,700 coins)
is ~5.5 months for a p75 member and ~2 for the chat's busiest — and 15 for somebody who
never writes. Fights land at **~20 blows, ten a side, at every level**: measured medians
10/10/10 at levels 1/40/80. Holding that flat is why `HP_PER_POINT` (19) so far outruns
`DAMAGE_PER_POINT` (2.2) — dodge and crit both grow with level, so HP has to grow faster
than damage just to keep the length constant.

The 500 weapons live as deterministic immutable data in `pets_weapon_catalog.py`; combat
and trade plumbing remain in the existing pet modules. Retuning the economy is still
editing constants and catalogue data rather than changing the fight engine.
**[`PETS_BALANCE.md`](PETS_BALANCE.md)** has the full tables, the reasoning, and the honest
list of what is still wrong.

Split the way `cabinet.py` and `poker.py` already are: `pets_config.py` (numbers),
`pets.py` (state, storage, wallet), `pets_combat.py` (the fight, deterministic given a
seed), `pets_flavor.py` (the jokes), `pets_ui.py` (every screen as a pure
`(text, keyboard)`), and only Telegram I/O in `bot_listener.py`.

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
| `/buttons` | DM | build a post with 1–5 buttons and live tap counters |
| `/replant` | DM | re-post the planting announcement, start the tree over |
| `/badgeadmin [-] @user` | DM | delegate custom-badge rights |
| `/badge` | DM | create, award and remove custom badges |
| `/weekwinner`, `/deletepokras` | DM | weekly winner badge; remove a figurine credit |

### Menu button and the fallback menu

The bot publishes its command list to Telegram at startup (`setMyCommands`), so the
client shows a tappable ☰ **Menu** next to the input field and nobody has to know a
command exists. `/arena` is first in both scopes. DMs then get `/cabinet /stat /top /shop
/tree /vote /pet`; groups then get `/stat /top /shop /tree /vote /pet /duel`. Wallet actions
belong in the DM where a balance isn't public,
and `/cabinet` is absent from the group menu on purpose — it only works in a DM, so a
group button for it would just answer "напиши мне в личку".

The two aliases are spelled without a space because Telegram only accepts `[a-z0-9_]` in
a registered command name: **`/top all` cannot be a menu entry at all**. Both spellings
work when typed — `/top all` = `/topall`, `/top pokras` = `/toppokras` = `/stat pokras`.
The procrastinator list is capped at `PROCRASTINATOR_LIST_SIZE` (10) names, on demand and
in the automatic digest alike: it is a public call-out, and past about ten names it stops
reading as a nudge and starts reading as a wall. Admin-only commands — `/badge`, `/weekwinner`, `/deletepokras`,
`/badgeadmin`, `/replant`, `/preview`, `/buttons`, and the two planting spellings — are deliberately
**not** advertised: putting them in front of all 190 members would invite a wave of "нужны
права администратора". Registration is best-effort: the bot starts fine without a menu.

Because `_match_allowed_chat` never matches a private chat, `/stat`, `/top`, `/shop` and
`/coins` used to be silent no-ops in a DM. They now fall back to the configured home chat
(`_stats_entry_for`), the same way `/cabinet` and the summary pipeline already did — a
published menu must not contain commands that do nothing where it is published.

**Any unhandled DM gets the menu back.** After every specific handler has declined —
commands, summary keywords, both force-reply flows — a private message
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

The work critique and the streak freeze were removed from the catalogue; their delivery
code and the freeze machinery are left in place, so re-listing either is adding one
`ShopItem` back — the same "disabled, not removed" convention the XP cooldown already
follows.

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

`/badge` itself: The bot shows these inline options:

- **Создать значок** asks for `<emoji> <name>`, for example `🎯 Меткий глаз`. What counts
  as an emoji is the character's Unicode **category** (`So`), plus the keycap and
  variation-selector marks — not a hand-written list of blocks, which is what refused
  `⭐ Майор` (U+2B50 sits outside every range that list named, as do ⌛, ⏰ and ⭕).
  `Sm` (`+`, `=`) deliberately does not qualify on its own, so a missing emoji is still
  caught.
- **Выдать значок** shows the chat's saved custom badges, then asks for the recipients.
- **🤫 Выдать без уведомления** is the same award with **no group announcement**. A
  separate button rather than a toggle on the menu: a toggle carries a state the
  administrator has to read back before tapping, and misreading it publishes something
  that was meant to stay quiet. The administrator's own DM confirmation is never
  silenced — it is the only thing telling them the award landed — and it ends with
  `Без объявления в чате.` so the choice is visible after the fact.
- **Забрать у участника** takes one badge from one member, leaving the definition alone.
  Not announced in the group: an award is good news worth sharing, having one taken away
  is not something to publish about somebody.
- **Удалить значок совсем** deletes the definition *and* every assignment of it, behind a
  confirmation that spells out how many members currently hold it. Assignments are
  cleared rather than left dangling — a leftover would be invisible but would still count
  towards somebody's collection total and would come back if the id were reused.

**Every screen below the menu has ◀️ Назад** — the same label the pet game and the cabinet
use, so every menu in the bot gets out the same way. It returns to the root menu *and*
forgets what the abandoned step had gathered (selected badge, the quiet flag), so backing
out of Выдать and into Удалить cannot act on what the previous step chose. The
irreversible delete confirmation carries it too: leaving `🗑 Да, удалить` as the only
button meant the only way *not* to delete was to ignore the message. On the steps that ask
for **text** the way back is the word `назад` rather than a button, because Telegram allows
one `reply_markup` per message and a force-reply cannot also carry an inline keyboard; the
prompt says so. `отмена` still drops the flow entirely — `назад` keeps it.

Custom definitions and assignments are persisted per chat under the existing stats
cache. Awarding the same badge to the same member twice is idempotent.

**Several recipients at once.** The recipient prompt accepts a list separated by commas,
semicolons or newlines — `@one, @two` or one name per line, up to 30 at a time:

```text
@one, @two
Алексей Белявский
```

Spaces are deliberately **not** a separator: plenty of members are tracked under a
two-word display name, and splitting on whitespace would go looking for people who do not
exist. Repeats are dropped, both by spelling (`@user` / `user`) and after resolution, so
naming the same person twice awards once and counts once.

Names that resolve to nobody do not discard the rest — the ones that were found are
awarded and the failures are listed back (`Не нашёл в статистике: @ghost`). Only when
*nothing* matched does the flow re-prompt instead of awarding.

A **new** award is announced in the group chat, once, however many people it went to:

```text
@user получил уникальный значок: 🎯 Меткий глаз
@one, @two, Алексей Белявский получили уникальный значок: 🎯 Меткий глаз
```

Only on a genuinely new award — re-running the flow must not post it again, and eight
recipients must not mean eight posts. Recipients are named by `@username` so they are
actually notified, falling back to their display name when they have none. Best-effort:
the badge is already recorded by the time this sends, so a failed announcement costs the
message, never the badge. The menu and its
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
   warning above), `SUMMARY_QUEUE_DELAY_SECONDS`,
   `TELEGRAM_BOT_TOKEN` (if replies should come from a bot account instead of this one --
   see above).
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
  bot or your account sent -- a summary, a `/stat`/`/top` reply, whatever -- deletes
  it almost immediately, as a one-tap cleanup shortcut. It never touches other people's
  messages, even though your account may have delete rights over the whole chat.
