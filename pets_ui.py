"""Every screen of the pet game, as pure functions.

The split is the one cabinet.py already established and for the same reason: each view is
a function returning `(text, keyboard)` that touches nothing but the local stores, so the
whole menu -- every price, every button, every refusal -- is testable without a bot token,
a network or a fake Telegram. All the I/O, and the one genuinely async step (working out
who pressed the button), lives in bot_listener.py.

Callback data is `pet:<owner_id>:<action>[:<argument>]`. The owner id travels inside the
button rather than in a server-side session, so a menu keeps working across a process
restart and a button can never be pressed by somebody it does not belong to -- the
handler compares the id in the data against the id of whoever tapped. Only the two steps
that need free text or a photo back (naming and re-photographing a creature) hold
temporary server-side state, because a force-reply prompt has nothing else to correlate
against.

Rendered with Telegram's HTML parse mode, so every user-controlled string -- a pet name
above all, since players choose it -- goes through html.escape here rather than at the
call site.
"""

from datetime import datetime, timezone
from html import escape

import pets
import pets_config as C
import pets_updates

CALLBACK_PREFIX = "pet"
# Telegram caps callback_data at 64 bytes. "pet:" + a 19-digit id + ":" + the longest
# action + ":" + the longest item code stays comfortably inside that.
MAX_CALLBACK_BYTES = 64

BACK_BUTTON = "◀️ Назад"
LEADERBOARD_PAGE_SIZE = 20
SLOT_PAGE_SIZE = 8
INVENTORY_PAGE_SIZE = 6
COLLECTION_PAGE_SIZE = 8
ARENA_NO_FIGHTS_NOTICE = "🚫 В запасе нет боёв."
RARITY_FILTERS = ("all", "cursed", "common", "uncommon", "rare", "legendary")
RARITY_FILTER_NAMES = {
    "all": "Все", "cursed": "Проклятые", "common": "Обычные",
    "uncommon": "Необычные", "rare": "Редкие", "legendary": "Легендарные",
}


def callback_data(owner_id, action: str, argument: str = "") -> str:
    parts = [CALLBACK_PREFIX, str(owner_id), action]
    if argument:
        parts.append(argument)
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """(owner_id, action, argument) or None when this isn't a pet button."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2], parts[3] if len(parts) > 3 else ""


def search_argument(current_opponent_id) -> str:
    """Remember only the current card, not an ever-growing reroll counter.

    Telegram user ids have at most 19 decimal digits, so even the longest search
    callback (owner id + action + opponent id) is safely below Telegram's 64-byte cap.
    """
    return str(current_opponent_id)


def parse_search_argument(argument: str) -> str | None:
    """Return a previously displayed opponent id, accepting no unbounded state."""
    candidate = str(argument or "")
    return candidate if candidate.isdecimal() and len(candidate) <= 19 else None


def _back_row(owner_id) -> list:
    return [{"text": BACK_BUTTON, "callback_data": callback_data(owner_id, "main")}]


def _money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".")


def _plural(amount: int, one: str, few: str, many: str) -> str:
    """A number with the noun in the case Russian actually wants: 1 монета, 22 монеты,
    25 монет. Same trap the battle log has to sidestep by using indeclinable words -- but
    here the number IS known at render time, so it can just be done properly.

    Note the 11-14 exception: 21 takes `one` and 11 takes `many`, which is why this checks
    `% 100` as well as `% 10`.
    """
    if amount % 10 == 1 and amount % 100 != 11:
        word = one
    elif 2 <= amount % 10 <= 4 and not (12 <= amount % 100 <= 14):
        word = few
    else:
        word = many
    return f"{_money(amount)} {word}"


def _coins(amount: int) -> str:
    return _plural(amount, "монета", "монеты", "монет")


def _name(pet: dict) -> str:
    return escape(pet.get("name") or "Существо")


# ------------------------------------------------------------------------ main menu


def main_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The landing screen. Deliberately shows the whole state of the account in six lines
    -- cage, creature, level, coins, fights left -- because every other screen is one tap
    away and re-reading this one is how a player checks whether anything changed."""
    coins = pets.balance_for(entry, user_id, xp)
    pet = pets.get_pet(entry, user_id)
    cage = pets.cage_level(entry, user_id)

    lines = ["🏟 <b>Арена</b>\n"]
    if not cage:
        lines.append("У тебя пока нет клетки, а значит и существа.")
        lines.append(f"Клетка стоит {_coins(C.CAGE_PRICE)}, приручение — {_coins(C.TAME_PRICE)}.")
    elif not pet:
        lines.append(f"🏠 Клетка: уровень {cage} — пустая.")
        lines.append(f"Осталось приручить существо за {_coins(C.TAME_PRICE)}.")
        lines.append("Существо должно быть твоей собственной раскрашенной фигуркой.")
    else:
        fights = pets.fight_allowance_breakdown(entry, user_id, pets.today())
        left = fights["available"]
        capacity = fights["capacity"]
        lines.append(f"🐾 {_name(pet)} — уровень {pet.get('level', 1)}")
        lines.append(f"🏠 Клетка: уровень {cage}")
        lines.append(f"⚔️ Боёв в запасе: {left} из {capacity}")
        lines.append(f"🏆 Боёв: {pet.get('fights', 0)} / побед: {pet.get('wins', 0)}")
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows = [[
        {"text": "🏠 Клетка", "callback_data": callback_data(user_id, "cage")},
        {"text": "🏆 Существа сервера", "callback_data": callback_data(user_id, "leaderboard")},
    ]]
    if pet:
        rows.append([
            {"text": "🐾 Существо", "callback_data": callback_data(user_id, "pet")},
            {"text": "💪 Прокачка", "callback_data": callback_data(user_id, "train")},
        ])
        rows.append([
            {"text": "🌾 Ферма", "callback_data": callback_data(user_id, "farm")},
            {"text": "🎒 Снаряжение", "callback_data": callback_data(user_id, "bag")},
        ])
        rows.append([
            {"text": "🛒 Магазин", "callback_data": callback_data(user_id, "store")},
            {"text": "📚 Коллекция", "callback_data": callback_data(user_id, "collection")},
        ])
        rows.append([
            {"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")},
            {"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")},
        ])
        rows.append([
            {"text": "🎰 Казино", "callback_data": callback_data(user_id, "casino")},
        ])
        notifications_enabled = pets.fight_result_notifications_enabled(entry, user_id)
        rows.append([
            {
                "text": "🔔 Результаты: вкл." if notifications_enabled else "🔕 Результаты: выкл.",
                "callback_data": callback_data(user_id, "fightnotify"),
            },
            {
                "text": "🔴 Обновления" if pets_updates.has_unread(entry, user_id) else "📰 Обновления",
                "callback_data": callback_data(user_id, "updates"),
            },
        ])
    elif cage:
        rows.append([{
            "text": f"🐣 Приручить свою фигурку — {_money(C.TAME_PRICE)}",
            "callback_data": callback_data(user_id, "tame"),
        }])
    if not pet:
        updates_button = "🔴 Обновления" if pets_updates.has_unread(entry, user_id) else "📰 Обновления"
        rows.append([{"text": updates_button, "callback_data": callback_data(user_id, "updates")}])
    rows.append([
        {"text": "ℹ️ Как играть", "callback_data": callback_data(user_id, "info")},
        {"text": "🔄 Обновить", "callback_data": callback_data(user_id, "main")},
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


def leaderboard_view(entry: str, user_id, page: int = 0) -> tuple[str, dict]:
    """A paginated server roster, ordered by the combat score matchmaking uses."""
    rows = pets.pet_leaderboard(entry)
    total_pages = max(1, (len(rows) + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    start = page * LEADERBOARD_PAGE_SIZE
    visible = rows[start:start + LEADERBOARD_PAGE_SIZE]
    lines = ["🏆 <b>Существа сервера</b>\n"]
    if not visible:
        lines.append("На сервере ещё никто не приручил существо.")
    else:
        for position, row in enumerate(visible, start=start + 1):
            username = row.get("owner_username")
            owner = f"@{username}" if username else row["owner_name"]
            lines.append(
                f"{position}. {escape(owner)} — <b>{escape(row['name'])}</b> — {row['power']}"
            )
    lines.append(f"\n<i>{page + 1}/{total_pages}</i>")

    keyboard = []
    navigation = []
    if page > 0:
        navigation.append({
            "text": "◀️", "callback_data": callback_data(user_id, "leaderboard", str(page - 1)),
        })
    if page + 1 < total_pages:
        navigation.append({
            "text": "▶️", "callback_data": callback_data(user_id, "leaderboard", str(page + 1)),
        })
    if navigation:
        keyboard.append(navigation)
    keyboard.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": keyboard}


def info_view(user_id) -> tuple[str, dict]:
    """A compact rules reference available before and after getting a pet."""
    lines = ["ℹ️ <b>Как играть</b>\n"]
    lines.append("Все настройки арены можно найти, написав /arena в личке бота.")
    lines.append(
        f"1. Купи клетку за {_coins(C.CAGE_PRICE)}, затем приручи своего покраса за {_coins(C.TAME_PRICE)}."
    )
    lines.append("2. Прокачивай Силу, Здоровье, Ловкость и Удачу; экипировка добавляет статы и Броню.")
    lines.append("3. Сражайся через /arena: соперник выбирается случайно. В запас приходит +1 бой каждый час; его максимум увеличивают клетка, ферма и свежие #япокрасил.")
    lines.append(
        f"4. Победа приносит {C.WIN_GOLD_MIN}–{C.WIN_GOLD_MAX} монет и опыт. "
        f"Поражение забирает только {round(C.LOSS_GOLD_SHARE * 100)}% награды, без долгов."
    )
    lines.append("5. Ненужную снятую экипировку можно продать или подарить владельцу другого существа.")
    lines.append("6. Каждый второй уровень фермы добавляет место в запасе боёв; все уровни улучшают смены и пассивную добычу монет.")
    lines.append("\n<b>Статы</b>")
    lines.append(
        "Сила увеличивает урон. Здоровье повышает HP. Ловкость даёт уклонение. "
        "Удача повышает шанс крита и шанс найти вещь — в бою и на ферме "
        f"(до +{round(C.LUCK_DROP_BONUS_MAX * 100)}% на максимуме)."
    )
    lines.append("\n<b>Особые преимущества</b>")
    lines.append(
        "Если один стат в 2 раза выше, чем у соперника, он иногда срабатывает как фирменный приём; "
        "в 3 раза — сильнее. За бой срабатывает только один такой приём на существо."
    )
    lines.append(
        "Сила наносит мощный стартовый удар, Здоровье гасит первый удар, Ловкость уклоняется или отвечает, "
        "Броня блокирует удар, а Удача даёт сильный, но не смертельный стартовый эффект."
    )
    lines.append("\n<b>Дуэли</b>: напиши /duel @user в общем чате или в личке бота. Одного и того же соперника можно вызвать раз в день.")
    return "\n".join(lines), {"inline_keyboard": [_back_row(user_id)]}


def casino_view(entry: str, user_id) -> tuple[str, dict]:
    """A placeholder with a real button behind it, on purpose.

    The button ships before the game does so the idea can be announced and reacted to
    without a half-built gambling loop being live in a chat where coins are already tight.
    Nothing here reads or writes the ledger.
    """
    lines = [
        "🎰 <b>Казино</b>\n",
        "Казик строится, заходите позже.",
    ]
    return "\n".join(lines), {"inline_keyboard": [_back_row(user_id)]}


def updates_view(entry: str, user_id, page: int = 0) -> tuple[str, dict]:
    """Newest-first, one concise release note per screen."""
    update, page, total = pets_updates.page(entry, page)
    if update is None:
        return "📰 <b>Обновления</b>\n\nПока нет опубликованных обновлений.", {
            "inline_keyboard": [_back_row(user_id)],
        }

    # Escaped, not trusted as markup: an entry written in chat with "/arenanews" is
    # whatever an admin typed, and a single stray "<" would fail the whole HTML send.
    # The shipped entries are plain text too, so nothing is lost by escaping both.
    lines = [f"📰 <b>Обновления</b>", ""]
    # A chat-authored entry may be title-only or body-only; neither should leave an empty
    # bold line or a stray blank behind.
    if update.title:
        lines.append(f"<b>{escape(update.title)}</b>")
    if update.text:
        lines.append(escape(update.text))
    lines.append(f"\n<i>{page + 1}/{total}</i>")
    navigation = []
    if page + 1 < total:
        navigation.append({
            "text": "◀️", "callback_data": callback_data(user_id, "updates", str(page + 1)),
        })
    if page > 0:
        navigation.append({
            "text": "▶️", "callback_data": callback_data(user_id, "updates", str(page - 1)),
        })
    keyboard = [navigation] if navigation else []
    keyboard.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": keyboard}


# ----------------------------------------------------------------------------- cage


def cage_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The cage is the game's convenience track: it was asked for as "buy, then upgrade"
    without saying what an upgrade buys, so each level is one more fight a day and a cut
    of the winnings -- things a player feels every day without them changing who wins a
    fight."""
    level = pets.cage_level(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)

    lines = ["🏠 <b>Клетка</b>\n"]
    if not level:
        lines.append("Клетки нет. Без неё существо негде держать.")
        lines.append(f"\nПокупка: {_coins(C.CAGE_PRICE)}.")
    else:
        lines.append(f"Уровень {level} из {C.CAGE_MAX_LEVEL}.")
        # The cage expands the shared fight bank; the arena screen shows its actual
        # current fill, while this screen only promises the permanent extra capacity.
        lines.append(f"⚔️ Мест в запасе боёв: +{C.CAGE_BONUS_FIGHTS[level - 1]}")
        lines.append(f"🪙 Прибавка к добыче: +{C.CAGE_GOLD_BONUS_PCT[level - 1]}%")
        if level < C.CAGE_MAX_LEVEL:
            nxt = C.CAGE_UPGRADE_COSTS[level]
            lines.append(
                f"\nСледующий уровень — {_coins(nxt)}:"
                f" мест в запасе +{C.CAGE_BONUS_FIGHTS[level]},"
                f" добыча +{C.CAGE_GOLD_BONUS_PCT[level]}%."
            )
        else:
            lines.append("\nЭто максимальный уровень.")
    lines.append(f"\n🪙 У тебя: {_money(coins)}")

    rows = []
    if not level:
        rows.append([{
            "text": f"Купить клетку — {_money(C.CAGE_PRICE)}",
            "callback_data": callback_data(user_id, "buycage"),
        }])
    elif level < C.CAGE_MAX_LEVEL:
        rows.append([{
            "text": f"⬆️ Улучшить — {_money(C.CAGE_UPGRADE_COSTS[level])}",
            "callback_data": callback_data(user_id, "upcage"),
        }])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


# ----------------------------------------------------------------------------- farm


FARM_FEATURE_LABELS = {
    "well": "🪣 Колодец",
    "sprinkler": "💦 Поливалка",
    "beds": "🥕 Грядки",
    "tractor": "🚜 Трактор",
}
FARM_FEATURE_EFFECTS = {
    "well": "+25% монет с каждой смены",
    "sprinkler": "+25% опыта с каждой смены",
    "beds": "+5% к шансу найти вещь",
    "tractor": "+20% монет и опыта",
}


def _farm_duration(seconds: int) -> str:
    """A short, stable Russian countdown for a farm run.

    The core persists UTC timestamps, whereas this screen is intentionally a pure view;
    accepting an already rounded number of seconds keeps timezone conversion and payout
    decisions out of the Telegram layer.
    """
    seconds = max(0, int(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def _fight_refresh_duration(seconds: int) -> str:
    """Round upward so a positive remainder is never displayed as zero minutes."""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "меньше минуты"
    total_minutes = (seconds + 59) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def _farm_seconds_left(status: dict) -> int:
    """Read both the public core status and older/recovered timestamp-shaped records."""
    direct = status.get("seconds_left")
    if direct is not None:
        return max(0, int(direct))
    ends_at = status.get("ends_at")
    if isinstance(ends_at, datetime):
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        return max(0, int((ends_at - datetime.now(timezone.utc)).total_seconds()))
    return 0


def _farm_feature_status(status: dict, feature: str) -> dict:
    """Normalise the small presentation shape while farm records stay backward-compatible."""
    features = status.get("features") or status.get("upgrades") or {}
    raw = features.get(feature, {}) if isinstance(features, dict) else {}
    if isinstance(raw, bool):
        costs = status.get("feature_costs") or {}
        raw = {
            "level": int(raw), "max_level": 1,
            "next_cost": (costs.get(feature) if not raw else None),
            "effect": FARM_FEATURE_EFFECTS.get(feature, ""),
        }
    elif isinstance(raw, int):
        raw = {"level": raw}
    return raw if isinstance(raw, dict) else {}


def farm_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """A player-chosen 1-8 hour expedition, its one active-run lock, and every permanent
    upgrade.

    Rewards are deliberately settled by the background worker (or, for an early recall, by
    cancel_farm handing off to that same settlement path) rather than by rendering this
    screen. Opening the menu therefore cannot pay a run twice or make a DM notification
    disappear after a restart.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)

    status = pets.farm_status(entry, user_id)
    passive_before = pets.passive_income_status(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)
    level = int(status.get("level", status.get("farm_level", 0)) or 0)
    max_level = int(status.get("max_level", 10) or 10)
    active = bool(status.get("running"))
    lines = ["🌾 <b>Ферма</b>\n"]
    lines.append(f"🐾 Работник: <b>{_name(pet)}</b>")
    lines.append(f"🏡 Уровень фермы: {level} из {max_level}.")
    if level > 0:
        lines.append(
            f"🪙 Пассивно: +{passive_before['rate']} монет/ч · "
            f"склад до {_money(passive_before['cap'])}."
        )
        if passive_before.get("stored"):
            lines.append(f"✅ Пассивно собрано сейчас: +{_money(passive_before['stored'])}.")
        else:
            lines.append(f"⏱ Следующее начисление — в {passive_before['next_hour'].strftime('%H:%M')}.")

    if active:
        remaining = _farm_seconds_left(status)
        planned = int(status.get("planned_hours") or 0)
        worked = int(status.get("worked_hours") or 0)
        lines.append(
            f"\n⏳ Питомец на ферме ({planned} ч). Вернётся через <b>{_farm_duration(remaining)}</b>."
        )
        # No more attack immunity: a farming pet cannot pick a fight itself, but it is an
        # ordinary target for everyone else's.
        lines.append("Сам он в бой не пойдёт, пока работает, — но напасть на него можно.")
        lines.append(f"Полностью отработано: {worked} ч из {planned}.")
        reward = status.get("reward") or {}
        if reward:
            lines.append(
                f"Смена принесёт: 🪙 {_money(int(reward.get('gold', 0) or 0))} · "
                f"✨ {int(reward.get('xp', 0) or 0)} опыта."
            )
        lines.append("Забрать раньше срока можно — но заплатит смена только за целые отработанные часы.")
    elif status.get("ready"):
        lines.append("\n✅ Смена закончилась. Награда уже едет в личные сообщения.")
    else:
        if level <= 0:
            lines.append("\nСначала построй ферму. Первый уровень откроет смены от 1 до 8 часов.")
        else:
            lines.append(
                "\nВыбери длину смены: чем дольше пропадает питомец, тем больше монет, "
                "опыта и шанс привезти находку получше."
            )
            lines.append("<i>смена — 🪙 монет · ✨ опыта · 🎁 шанс находки</i>")
            for row in status.get("hour_previews", []):
                drop_pct = float(row.get("drop_chance", 0.0) or 0.0) * 100
                lines.append(
                    f"{row['hours']} ч — 🪙 {_money(int(row['gold']))} · "
                    f"✨ {int(row['xp'])} · 🎁 {drop_pct:g}%"
                )

    next_cost = status.get("next_level_cost")
    if level < max_level:
        next_level = level + 1
        bonus = status.get("next_level_bonus")
        suffix = f" · {escape(str(bonus))}" if bonus else ""
        lines.append(
            f"\nСледующий уровень: {next_level}"
            + (f" — {_coins(int(next_cost))}" if next_cost is not None else "")
            + suffix
        )
    else:
        lines.append("\n🏆 Ферма прокачана полностью.")

    lines.append("\n<b>Апгрейды участка</b>")
    for feature, label in FARM_FEATURE_LABELS.items():
        data = _farm_feature_status(status, feature)
        feature_level = int(data.get("level", 0) or 0)
        feature_max = int(data.get("max_level", 1) or 1)
        effect = data.get("effect") or data.get("description") or ""
        effect_text = f" — {escape(str(effect))}" if effect else ""
        lines.append(f"{label}: {feature_level}/{feature_max}{effect_text}")

    lines.append(f"\n🪙 У тебя: {_money(coins)}")
    rows = []
    if status.get("can_start"):
        # Four per row -- two rows of four -- so all eight choices fit without a single
        # row running past what Telegram comfortably shows on a phone.
        hour_row = []
        for hours in C.FARM_HOUR_CHOICES:
            hour_row.append({
                "text": f"{hours} ч",
                "callback_data": callback_data(user_id, "farmstart", str(hours)),
            })
            if len(hour_row) == 4:
                rows.append(hour_row)
                hour_row = []
        if hour_row:
            rows.append(hour_row)
    if status.get("can_cancel"):
        rows.append([{
            "text": "❌ Забрать сейчас",
            "callback_data": callback_data(user_id, "farmcancel"),
        }])
    if level < max_level:
        cost_text = f" — {_money(int(next_cost))}" if next_cost is not None else ""
        rows.append([{
            "text": f"🏡 {'Построить ферму' if level == 0 else 'Улучшить ферму'}{cost_text}",
            "callback_data": callback_data(user_id, "upfarm"),
        }])
    for feature, label in FARM_FEATURE_LABELS.items():
        if level <= 0:
            break
        data = _farm_feature_status(status, feature)
        feature_level = int(data.get("level", 0) or 0)
        feature_max = int(data.get("max_level", 1) or 1)
        if feature_level >= feature_max:
            continue
        cost = data.get("next_cost", data.get("cost"))
        cost_text = f" — {_money(int(cost))}" if cost is not None else ""
        rows.append([{
            "text": f"⬆️ {label}{cost_text}",
            "callback_data": callback_data(user_id, "farmup", feature),
        }])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


# ------------------------------------------------------------------------- training


def train_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """One row per stat, with the price of the NEXT point written on the button. Showing
    the price on the button rather than in the text is the whole design: the cost curve is
    the thing a player is deciding against, and making them read it off a table above and
    match it to a button below is how a menu gets misread."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    coins = pets.balance_for(entry, user_id, xp)
    levels = pet.get("stats", {})
    effective = pets.effective_stats(entry, user_id)

    lines = [f"💪 <b>Прокачка</b> — {_name(pet)}\n"]
    for key in C.STAT_KEYS:
        level = levels.get(key, C.STAT_MIN_LEVEL)
        # The effective value differs from the purchased level by the pet's own level and
        # its gear, so both are printed: one is what was paid for, the other is what
        # actually fights.
        lines.append(
            f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {level}"
            f" <i>(в бою {effective.get(key, level)})</i>"
        )
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {effective.get('armor', 0)} <i>(из снаряжения)</i>")
    # Luck is the one stat whose payoff is invisible in a fight log, so its current find
    # bonus is spelled out where the points are actually bought.
    luck_bonus = C.luck_drop_multiplier(effective.get("luck", levels.get("luck", C.STAT_MIN_LEVEL))) - 1
    lines.append(
        f"\n🍀 Удача сейчас даёт <b>+{luck_bonus * 100:.0f}%</b> к шансу найти вещь"
        " — и в бою, и на ферме."
    )
    lines.append(f"\n🪙 Монеты: {_money(coins)}")
    lines.append(f"\n<i>Уровни: {C.STAT_MIN_LEVEL}–{C.STAT_MAX_LEVEL}. Чем выше, тем дороже следующий пункт.</i>")

    rows = []
    for key in C.STAT_KEYS:
        level = levels.get(key, C.STAT_MIN_LEVEL)
        if level >= C.STAT_MAX_LEVEL:
            rows.append([{
                "text": f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]} — максимум",
                "callback_data": callback_data(user_id, "noop"),
            }])
            continue
        one = C.stat_upgrade_cost(level)
        ten = C.total_stat_cost(min(level + 10, C.STAT_MAX_LEVEL), level)
        row = [{
            "text": f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]} +1 — {_money(one)}",
            "callback_data": callback_data(user_id, "up", key),
        }]
        # The +10 button is not a discount, just fewer taps: it charges the sum of the ten
        # individual steps, and buys as many as the wallet reaches.
        if level + 1 < C.STAT_MAX_LEVEL:
            row.append({
                "text": f"+10 — {_money(ten)}",
                "callback_data": callback_data(user_id, "up10", key),
            })
        rows.append(row)
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


# ------------------------------------------------------------------------ inventory


def bag_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The equipment hub, deliberately separate from the 500-item catalogue.

    The old screen opened the full weapon catalogue from the only inventory button.
    That made a player with two weapons wade through 60+ pages of things they did not
    own.  The hub answers the useful questions first (what is worn, how many things
    are in the bag), then makes the three destinations explicit: bag, daily shop, and
    collection.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    coins = pets.balance_for(entry, user_id, xp)
    equipped = pet.get("equipped", {})
    owned = pet.get("inventory", [])

    effective = pets.effective_stats(entry, user_id)
    lines = ["🎒 <b>Снаряжение</b>", "Выбирай вещи из своей сумки; новые — в магазине.", ""]
    for slot in C.SLOT_KEYS:
        code = equipped.get(slot)
        item = C.find_item(code) if code else None
        lines.append(
            f"{C.SLOT_EMOJI[slot]} {C.SLOT_NAMES[slot]}: "
            + (f"<b>{escape(item.name)}</b> — {_bonus_text(item)}" if item else "пусто")
        )
    lines.append(
        "\n<i>Итог: " + " · ".join(
            f"{C.STAT_EMOJI[key]} {effective.get(key, 1)}" for key in C.STAT_KEYS
        ) + f" · {C.ARMOR_EMOJI} {effective.get('armor', 0)}</i>"
    )
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows = []
    for slot in C.SLOT_KEYS:
        slot_owned = sum(
            C.find_item(code) is not None and C.find_item(code).slot == slot
            for code in owned
        )
        rows.append([{
            "text": f"{C.SLOT_EMOJI[slot]} Моя сумка · {slot_owned}",
            "callback_data": callback_data(user_id, "bagitems", slot_argument(slot)),
        }])
    rows.append([{
        "text": "🛒 Магазин дня", "callback_data": callback_data(user_id, "store"),
    }, {
        "text": "📚 Коллекция", "callback_data": callback_data(user_id, "collection"),
    }])
    if owned:
        lines.append(
            f"\n<i>В сумке: {_plural(len(owned), 'предмет', 'предмета', 'предметов')}.</i>"
        )
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def slot_argument(slot: str, page: int = 0) -> str:
    return f"{slot},{max(0, int(page))}"


def parse_slot_argument(argument: str) -> tuple[str, int]:
    slot, _, raw_page = str(argument or "").partition(",")
    return slot, int(raw_page) if raw_page.isdecimal() else 0


def _buyable_here(item, daily_weapon_codes) -> bool:
    """Whether tapping "Купить" on this item would work right now.

    Shared by the sort key below and the button-rendering loop so the two can never
    disagree: a weapon is only really for sale while it sits in today's ten-item
    window (see ``daily_storefront_weapons``); every other shop item is always for
    sale, since amulets/gloves/boots have no rotating storefront.
    """
    return item.source == "shop" and (item.slot != "weapon" or item.code in daily_weapon_codes)


def shop_slot_view(entry: str, user_id, xp: int, slot: str) -> tuple[str, dict]:
    """The shop's shelf for one non-weapon slot: what is on sale, and nothing else.

    ``slot_view`` is the full catalogue for a slot -- hundreds of entries, most of them
    drop-only trophies -- and reaching it from the 🛒 shop was the whole problem: whether
    the two buyable accessories landed on page one depended on how much the player already
    owned, so an active player still opened the shop onto a wall of "только из боёв" with
    no button anywhere. Weapons never had that problem because they have their own
    storefront (``store_view``). This is the same thing for the other three slots: a short,
    unpaginated shelf, so "buy an amulet" is always exactly two taps.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    if slot not in C.SLOT_KEYS or slot == "weapon":
        return store_view(entry, user_id, xp)
    owned = set(pet.get("inventory", []))
    stock = sorted(C.items_for_slot(slot, "shop"), key=lambda item: (item.price, item.code))

    lines = [f"🛒 <b>{escape(C.SLOT_NAMES[slot])}</b> {C.SLOT_EMOJI[slot]}\n"]
    rows = []
    if not stock:
        lines.append("Этот слот целиком выпадает из боёв — купить его нельзя.")
    for number, item in enumerate(stock, 1):
        label = C.RARITY_LABELS.get(getattr(item, "rarity", "common"), "⚪ Обычное")
        lines.append(f"<b>{number}. {escape(item.name)}</b> · {label}")
        lines.append(_bonus_icon_text(item))
        lines.append("✅ Уже у тебя" if item.code in owned else f"🪙 {_money(item.price)}")
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")
        lines.append("")
        if item.code not in owned:
            rows.append([{
                "text": f"Купить {item.name} — {_money(item.price)}",
                "callback_data": callback_data(user_id, "buy", item.code),
            }])
    if stock and all(item.code in owned for item in stock):
        lines.append("Здесь всё уже куплено. Остальное в этом слоте выпадает только из боёв.")
    lines.append(f"🪙 Монеты: {_money(pets.balance_for(entry, user_id, xp))}")

    rows.append([{
        # The full catalogue stays one tap away: seeing the trophy you cannot buy is the
        # point of that screen, it just should not be what the shop opens onto.
        "text": "📖 Весь каталог слота",
        "callback_data": callback_data(user_id, "slot", slot_argument(slot)),
    }])
    rows.append([{"text": "🛒 К витрине оружия", "callback_data": callback_data(user_id, "store")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def slot_view(entry: str, user_id, xp: int, slot: str, page: int = 0) -> tuple[str, dict]:
    """Everything that can go in one slot: what is worn, what is owned, what is for sale
    and what can only drop. The drop-only items are listed with no button on purpose --
    a player should be able to see that the good weapon exists and cannot be bought."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    if slot not in C.SLOT_KEYS:
        return main_view(entry, user_id, xp)
    coins = pets.balance_for(entry, user_id, xp)
    owned = set(pet.get("inventory", []))
    locked = set(pet.get("locked_items", []))
    worn = (pet.get("equipped") or {}).get(slot)
    daily_weapon_codes = {item.code for item in pets.daily_storefront_weapons(entry, pets.today())}

    # Owned gear must stay reachable even when its catalogue code lives on page 63, so it
    # keeps the first two sort tiers. But an amulet/gloves/boots catalogue is otherwise
    # ~30 drop-only trophies to 2 shop items, and those trophies' codes ("amulet_...",
    # "bt..", "gl..") often sort ahead of the shop items' names -- "bead" and "springs"
    # landed on page 4+ behind a wall of "только из боёв" entries nobody could buy, which
    # read in production as "the shop only sells weapons". Buyable-right-now stock is now
    # its own tier ahead of everything drop-only, so a player who taps into an empty slot
    # always sees something with a working "Купить" button on page one.
    all_items = sorted(
        C.items_for_slot(slot),
        key=lambda item: (
            item.code != worn,
            item.code not in owned,
            not _buyable_here(item, daily_weapon_codes),
            item.code,
        ),
    )
    total_pages = max(1, (len(all_items) + SLOT_PAGE_SIZE - 1) // SLOT_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = all_items[page * SLOT_PAGE_SIZE:(page + 1) * SLOT_PAGE_SIZE]
    lines = [f"{C.SLOT_EMOJI[slot]} <b>{escape(C.SLOT_NAMES[slot])}</b> · {page + 1}/{total_pages}\n"]
    if not any(item.code not in owned and _buyable_here(item, daily_weapon_codes) for item in all_items):
        # Everything purchasable is either already owned or (for weapons) off today's
        # window -- say so once, up top, instead of letting a page full of "только из
        # боёв" entries imply the shop sells nothing here at all.
        lines.append(
            "Сейчас купить здесь нечего: то, что продаётся, уже у тебя в сумке, "
            "а остальное — трофеи только из боёв.\n"
        )
    rows = []
    for item in visible:
        mark = " ✅" if item.code == worn else ""
        lock_mark = " 🔒" if item.code in locked else ""
        if item.code in owned:
            state = "в сумке"
        elif item.source == "drop":
            state = "только из боёв"
        elif item.slot == "weapon" and item.code not in daily_weapon_codes:
            state = "не на витрине сегодня"
        else:
            state = _coins(item.price)
        lines.append(f"<b>{escape(item.name)}</b>{mark}{lock_mark} — {_bonus_text(item)} · {state}")
        rarity = C.RARITY_LABELS.get(getattr(item, "rarity", "common"), "⚪ Обычное")
        lines[-1] = f"<b>{escape(item.name)}</b>{mark}{lock_mark} · {rarity} · {_bonus_text(item)} · {state}"
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")
        lines.append("")

        if item.code == worn:
            rows.append([{
                "text": f"Снять {item.name}",
                "callback_data": callback_data(user_id, "unequip", slot),
            }])
        elif item.code in owned:
            rows.append([{
                "text": f"Надеть {item.name}",
                "callback_data": callback_data(user_id, "equip", item.code),
            }])
            rows.append([{
                "text": "🔓 Открепить" if item.code in locked else "🔒 Закрепить",
                "callback_data": callback_data(user_id, "lock", item.code),
            }, {
                "text": "🎁 Подарить",
                "callback_data": callback_data(user_id, "gift", item.code),
            }])
            rows.append([{
                "text": f"💰 Продать · {_money(C.resale_value(item))}",
                "callback_data": callback_data(user_id, "sell", item.code),
            }])
        elif _buyable_here(item, daily_weapon_codes):
            rows.append([{
                "text": f"Купить {item.name} — {_money(item.price)}",
                "callback_data": callback_data(user_id, "buy", item.code),
            }])
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows.append([{"text": "🎒 К инвентарю", "callback_data": callback_data(user_id, "bag")}])
    navigation = []
    if page:
        navigation.append({"text": "◀️", "callback_data": callback_data(user_id, "slot", slot_argument(slot, page - 1))})
    if page + 1 < total_pages:
        navigation.append({"text": "▶️", "callback_data": callback_data(user_id, "slot", slot_argument(slot, page + 1))})
    if navigation:
        rows.append(navigation)
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def bag_items_view(entry: str, user_id, xp: int, slot: str, page: int = 0) -> tuple[str, dict]:
    """A practical, owned-items-only bag page.

    ``slot_view`` remains as a compatibility catalogue for old messages and direct
    callbacks, while every new route enters this view. It is intentionally compact:
    equipment actions only appear for items the player actually owns.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    if slot not in C.SLOT_KEYS:
        return bag_view(entry, user_id, xp)

    owned = [
        item for code in pet.get("inventory", [])
        if (item := C.find_item(code)) is not None and item.slot == slot
    ]
    locked = set(pet.get("locked_items", []))
    worn = (pet.get("equipped") or {}).get(slot)
    owned.sort(key=lambda item: (item.code != worn, item.code))
    total_pages = max(1, (len(owned) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = owned[page * INVENTORY_PAGE_SIZE:(page + 1) * INVENTORY_PAGE_SIZE]

    noun = _plural(len(owned), "предмет", "предмета", "предметов").split(" ", 1)[1]
    lines = [
        f"🎒 <b>Моя сумка · {escape(C.SLOT_NAMES[slot])}</b>",
        f"{len(owned)} {noun} · {page + 1}/{total_pages}",
    ]
    rows = []
    if not visible:
        lines.append("\nЗдесь пока пусто. Новое оружие появляется в магазине дня или после победы.")
    for number, item in enumerate(visible, start=page * INVENTORY_PAGE_SIZE + 1):
        is_worn = item.code == worn
        mark = " ✅ надето" if is_worn else ""
        lock_mark = " 🔒" if item.code in locked else ""
        label = C.RARITY_LABELS.get(item.rarity, item.rarity)
        lines.append(f"\n{number}. <b>{escape(item.name)}</b>{mark}{lock_mark}")
        lines.append(f"{label} · {_bonus_text(item)}")
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")

        if is_worn:
            rows.append([{
                "text": f"Снять · {item.name}",
                "callback_data": callback_data(user_id, "unequip", slot),
            }])
        else:
            rows.append([{
                "text": f"Надеть · {item.name}",
                "callback_data": callback_data(user_id, "equip", item.code),
            }])
        rows.append([{
            "text": "🔓 Открепить" if item.code in locked else "🔒 Закрепить",
            "callback_data": callback_data(user_id, "lock", item.code),
        }])
        # A lock is a safety control, not merely a warning: keep destructive actions
        # out of the convenient page as well as enforcing the same rule in pets.py.
        if not is_worn and item.code not in locked:
            rows.append([{
                "text": "🎁 Подарить",
                "callback_data": callback_data(user_id, "gift", item.code),
            }])
            rows.append([{
                "text": f"💰 Продать · {_money(C.resale_value(item))}",
                "callback_data": callback_data(user_id, "sell", item.code),
            }])

    navigation = []
    if page:
        navigation.append({
            "text": "◀️", "callback_data": callback_data(
                user_id, "bagitems", slot_argument(slot, page - 1),
            ),
        })
    if page + 1 < total_pages:
        navigation.append({
            "text": "▶️", "callback_data": callback_data(
                user_id, "bagitems", slot_argument(slot, page + 1),
            ),
        })
    if navigation:
        rows.append(navigation)
    rows.extend([
        [{"text": "🛒 В магазин дня", "callback_data": callback_data(user_id, "store")}],
        [{"text": "🎒 К снаряжению", "callback_data": callback_data(user_id, "bag")}],
        _back_row(user_id),
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


def slot_of(code: str) -> str:
    """Which slot an item code belongs to, falling back to the first slot for a code that
    no longer exists in the catalogue -- an item can be retired from ITEMS while a player
    still has it in the bag, and a redraw must not blow up because of that."""
    item = C.find_item(code)
    return item.slot if item else C.SLOT_KEYS[0]


def _rarity_argument(argument: str, with_page: bool = False) -> tuple[str, int]:
    rarity, _, raw_page = str(argument or "").partition(",")
    rarity = rarity if rarity in RARITY_FILTERS else "all"
    page = int(raw_page) if with_page and raw_page.isdecimal() else 0
    return rarity, max(0, page)


def collection_argument(rarity: str = "all", page: int = 0) -> str:
    return f"{rarity if rarity in RARITY_FILTERS else 'all'},{max(0, int(page))}"


def confirmation_argument(code: str, token: str) -> str:
    """Compact and callback-safe code/token pair for a one-time confirmation."""
    return f"{code},{token}"


def parse_confirmation_argument(argument: str) -> tuple[str, str]:
    code, separator, token = str(argument or "").partition(",")
    if not separator or not code.isalnum() or not token.isalnum() or len(token) > 16:
        return "", ""
    return code, token


def _rarity_buttons(
    user_id, action: str, selected: str, *, paged: bool = False, include_cursed: bool = True,
) -> list:
    short = {"all": "Все", "cursed": "☠️", "common": "⚪", "uncommon": "🟢", "rare": "🔵", "legendary": "🟡"}
    buttons = []
    for rarity in RARITY_FILTERS:
        if rarity == "cursed" and not include_cursed:
            continue
        text = short[rarity] + (" ✓" if rarity == selected else "")
        argument = collection_argument(rarity, 0) if paged else rarity
        buttons.append({"text": text, "callback_data": callback_data(user_id, action, argument)})
    return [buttons[:3], buttons[3:]]


def store_view(entry: str, user_id, xp: int, rarity: str = "all") -> tuple[str, dict]:
    """The 10-item daily weapon window plus direct routes to accessory shelves."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, _ = _rarity_argument(rarity)
    if rarity == "cursed":
        rarity = "all"
    owned = set(pet.get("inventory", []))
    stock = pets.daily_storefront_weapons(entry, pets.today())
    visible = [item for item in stock if rarity == "all" or item.rarity == rarity]
    lines = [
        "🛒 <b>Витрина дня</b>",
        f"Сегодня в продаже {C.DAILY_STOREFRONT_SIZE} оружий. Завтра ассортимент сменится.",
    ]
    lines.append(f"Фильтр: <b>{RARITY_FILTER_NAMES[rarity]}</b>\n")
    if not visible:
        lines.append("Сегодня оружия этой редкости не завезли.")
    rows = _rarity_buttons(user_id, "store", rarity, include_cursed=False)
    purchase_buttons = []
    for number, item in enumerate(visible, 1):
        label = C.RARITY_LABELS.get(item.rarity, item.rarity)
        lines.append(f"<b>{number}. {escape(item.name)}</b> · {label}")
        lines.append(_bonus_icon_text(item))
        lines.append(
            "✅ Уже у тебя" if item.code in owned else f"🪙 {_money(item.price)}"
        )
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")
        # Keep each weapon visually separate in Telegram's dense proportional font.
        lines.append("")
        if item.code not in owned:
            purchase_buttons.append({
                "text": str(number),
                "callback_data": callback_data(user_id, "buy", item.code),
            })
    if purchase_buttons:
        lines.append("Нажми номер оружия, чтобы купить.")
        # The full 10-item window occupies exactly three compact rows (4 + 4 + 2).
        # Filtered/partly-owned windows keep the same maximum of three rows.
        buttons_per_row = (len(purchase_buttons) + 2) // 3
        rows.extend(
            purchase_buttons[index:index + buttons_per_row]
            for index in range(0, len(purchase_buttons), buttons_per_row)
        )
    lines.append(f"🪙 Монеты: {_money(pets.balance_for(entry, user_id, xp))}")
    rows.extend([
        # These lead to the shop SHELF for each slot, not to the slot's full catalogue:
        # a button on the 🛒 screen has to open something you can actually buy.
        [{
            "text": f"{C.SLOT_EMOJI['amulet']} {C.SLOT_NAMES['amulet']}",
            "callback_data": callback_data(user_id, "shopslot", "amulet"),
        }, {
            "text": f"{C.SLOT_EMOJI['gloves']} {C.SLOT_NAMES['gloves']}",
            "callback_data": callback_data(user_id, "shopslot", "gloves"),
        }],
        [{
            "text": f"{C.SLOT_EMOJI['boots']} {C.SLOT_NAMES['boots']}",
            "callback_data": callback_data(user_id, "shopslot", "boots"),
        }],
        [{"text": "🎒 Моё снаряжение", "callback_data": callback_data(user_id, "bag")}],
        [{"text": "📚 Коллекция", "callback_data": callback_data(user_id, "collection")}],
        _back_row(user_id),
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


def collection_view(entry: str, user_id, xp: int, argument: str = "") -> tuple[str, dict]:
    """Chat-wide discovered weapons and their current owners; unknowns stay hidden."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, page = _rarity_argument(argument, with_page=True)
    collection = []
    for record in pets.discovered_weapon_collection(entry):
        item = C.find_item(record["code"])
        if item is not None and (rarity == "all" or item.rarity == rarity):
            collection.append((item, record["owners"]))
    lines = ["📚 <b>Открытое оружие этого чата</b>"]
    visible_all = collection
    total_pages = max(1, (len(visible_all) + COLLECTION_PAGE_SIZE - 1) // COLLECTION_PAGE_SIZE)
    page = min(page, total_pages - 1)
    visible = visible_all[page * COLLECTION_PAGE_SIZE:(page + 1) * COLLECTION_PAGE_SIZE]
    lines.append(f"\n<b>{RARITY_FILTER_NAMES[rarity]}</b> · страница {page + 1}")
    if not visible:
        lines.append("Пока ничего не открыто.")
    for number, (item, owners) in enumerate(visible, page * COLLECTION_PAGE_SIZE + 1):
        labels = [
            f"@{escape(owner['username'])}" if owner.get("username")
            else escape(owner.get("name") or "кто-то")
            for owner in owners
        ]
        shown = labels[:5]
        if len(labels) > len(shown):
            shown.append(f"ещё {len(labels) - len(shown)}")
        owner_label = ", ".join(shown) if shown else "сейчас ни у кого"
        owner_word = "Владелец" if len(labels) <= 1 else "Владельцы"
        lines.append(
            f"{number}. <b>{escape(item.name)}</b> · {C.RARITY_LABELS.get(item.rarity, item.rarity)}\n"
            f"{owner_word}: {owner_label}"
        )
    rows = _rarity_buttons(user_id, "collection", rarity, paged=True)
    navigation = []
    if page:
        navigation.append({"text": "◀️", "callback_data": callback_data(user_id, "collection", collection_argument(rarity, page - 1))})
    if page + 1 < total_pages:
        navigation.append({"text": "▶️", "callback_data": callback_data(user_id, "collection", collection_argument(rarity, page + 1))})
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "🛒 Витрина", "callback_data": callback_data(user_id, "store")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def valuable_item(item) -> bool:
    return item is not None and item.rarity in {"rare", "legendary"}


def item_confirmation_view(entry: str, user_id, xp: int, action: str, code: str, token: str) -> tuple[str, dict]:
    """Second, deliberate tap before a rare item leaves the inventory."""
    item = C.find_item(code)
    pet = pets.get_pet(entry, user_id)
    if not item or not pet or item.code not in pet.get("inventory", []):
        return notice_view(user_id, "Этого предмета уже нет в сумке.")
    verb = "продать" if action == "sell" else "подарить"
    execute = "sellok" if action == "sell" else "giftok"
    lines = [
        "⚠️ <b>Подтверждение</b>",
        f"{C.RARITY_LABELS.get(item.rarity, item.rarity)} «{escape(item.name)}».",
        f"Точно {verb}? Это действие нельзя отменить.",
    ]
    rows = [[{
        "text": f"Да, {verb}",
        "callback_data": callback_data(user_id, execute, confirmation_argument(item.code, token)),
    }, {
        "text": "Отмена",
        "callback_data": callback_data(user_id, "bagitems", slot_argument(slot_of(item.code))),
    }]]
    return "\n".join(lines), {"inline_keyboard": rows}


def _bonus_text(item) -> str:
    """"+6 Сила, -3 Ловкость" -- signed, because several items trade one stat for another
    and an unsigned list would read as all upside."""
    parts = []
    for key, value in item.bonuses.items():
        label = C.ARMOR_NAME if key == "armor" else C.STAT_NAMES.get(key, key)
        parts.append(f"{value:+d} {label}")
    effect = getattr(item, "effect", None)
    effect_text = effect.get("text") if isinstance(effect, dict) else None
    if effect_text:
        parts.append(f"🧿 {escape(str(effect_text))}")
    return ", ".join(parts) or "без бонусов"


def _bonus_icon_text(item) -> str:
    """Compact stat-only spelling used by the numbered daily storefront."""
    parts = []
    for key, value in item.bonuses.items():
        emoji = C.ARMOR_EMOJI if key == "armor" else C.STAT_EMOJI.get(key, "•")
        parts.append(f"{emoji} {value:+d}")
    if isinstance(getattr(item, "effect", None), dict) and item.effect.get("code"):
        parts.append("🧿")
    return "  ".join(parts) or "—"


# ---------------------------------------------------------------------------- arena


def fight_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    breakdown = pets.fight_allowance_breakdown(entry, user_id, pets.today())
    left = breakdown["available"]
    capacity = breakdown["capacity"]
    farming = pets.is_farming(entry, user_id)

    lines = ["⚔️ <b>Арена</b>\n"]
    lines.append(f"{_name(pet)} — уровень {pet.get('level', 1)}")
    lines.append(f"⚔️ В запасе боёв: <b>{left} из {capacity}</b>")
    if left >= capacity:
        lines.append("✅ Запас полный.")
    else:
        lines.append(
            f"⏳ Следующий +1 бой через: "
            f"{_fight_refresh_duration(breakdown['seconds_until_next'])}"
        )
    lines.append(
        "\n<b>Максимум запаса:</b> "
        f"{breakdown['base']} база"
        f" + {breakdown['cage_bonus']} клетка"
        f" + {breakdown['farm_bonus']} ферма"
        f" + {breakdown['paint_bonus']} #япокрасил"
        f" = {capacity}."
    )
    lines.append(
        f"<i>Каждая #япокрасил даёт +1 к максимуму на 7 дней. "
        f"Активных работ: {breakdown['recent_figurines']}.</i>"
    )
    lines.append(
        "\nСоперник выбирается случайно из всех существ, которых сейчас можно атаковать. "
        "Ограничений по уровню и боевой силе нет."
    )
    lines.append(
        f"Базовая награда за победу: {C.WIN_GOLD_MIN}–{C.WIN_GOLD_MAX} монет и {C.WIN_XP} опыта;"
        f" за соперника ниже уровнем — меньше, выше — больше (до ±25%)."
        f" Поражение: минус {round(C.LOSS_GOLD_SHARE * 100)}% от этого."
    )
    if farming:
        lines.append(
            "\n🌾 Питомец на ферме — сам он подождёт с боями, но напасть на него "
            "по-прежнему можно."
        )
    elif left <= 0:
        lines.append(f"\n<b>{ARENA_NO_FIGHTS_NOTICE}</b>")
        lines.append("Следующий бой появится после указанного выше отсчёта.")

    rows = []
    if left > 0 and not farming:
        rows.append([{
            "text": "🔍 Найти соперника",
            "callback_data": callback_data(user_id, "search"),
        }])
    rows.append([{"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def opponent_view(entry: str, user_id, opponent_id, xp: int) -> tuple[str, dict]:
    """The found opponent, with Напасть as a separate tap. Searching and attacking are two
    steps because a banked fight is spent by attacking, not by looking -- a player who
    searched and walked away has lost nothing."""
    mine = pets.get_pet(entry, user_id)
    theirs = pets.get_pet(entry, opponent_id)
    if not mine or not theirs:
        return fight_view(entry, user_id, xp)
    their_stats = pets.effective_stats(entry, opponent_id)

    lines = ["🔍 <b>Соперник найден</b>\n"]
    lines.append(f"🐾 <b>{_name(theirs)}</b> — уровень {theirs.get('level', 1)}")
    lines.append(f"Хозяин: {escape(theirs.get('owner_name') or 'кто-то')}")
    lines.append(
        f"Боёв: {theirs.get('fights', 0)} / побед: {theirs.get('wins', 0)}"
    )
    lines.append("")
    for key in C.STAT_KEYS:
        lines.append(f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {their_stats.get(key, 1)}")
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {their_stats.get('armor', 0)}")

    rows = [
        [{
            "text": "⚔️ Напасть",
            "callback_data": callback_data(user_id, "attack", str(opponent_id)),
        }],
        _back_row(user_id),
    ]
    rows.insert(1, [{
        "text": "🔍 Другой соперник",
        "callback_data": callback_data(user_id, "search", search_argument(opponent_id)),
    }])
    return "\n".join(lines), {"inline_keyboard": rows}


def fight_report(result, mine_key: str, names: dict, reward: dict | None) -> str:
    """Short caption for the composite result image; the image carries the full receipt."""
    lines = [f"<b>{escape(result.closing)}</b>"]
    if result.stopped_early:
        lines.append("<i>Решение по урону после 10 атак.</i>")

    won = result.winner == mine_key
    if result.is_draw:
        lines.append("🤝 <b>Ничья</b>")
    else:
        lines.append("🏆 <b>Победа</b>" if won else "💀 <b>Поражение</b>")
    if reward:
        if reward.get("draw"):
            lines.append(f"✨ +{reward['xp']} опыта")
        else:
            if reward.get("gold"):
                lines.append(f"🪙 +{_coins(reward['gold'])}")
            if reward.get("loss_gold"):
                lines.append(f"🪙 −{_coins(reward['loss_gold'])}")
            if reward.get("xp"):
                lines.append(f"✨ +{reward['xp']} опыта")
        if reward.get("levels_gained"):
            lines.append(
                f"⬆️ Новый уровень: {reward.get('level')} — +{reward['levels_gained']} ко всем статам"
            )
        dropped = reward.get("dropped_item")
        if dropped:
            item = C.find_item(dropped)
            if item:
                lines.append(f"🎁 Выпало: <b>{escape(item.name)}</b> — {_bonus_text(item)}")
                if reward.get("auto_equipped"):
                    lines.append(f"⚡ Автоматически надето в слот «{escape(C.SLOT_NAMES[item.slot])}».")
    return "\n".join(lines)


def battle_log(result) -> str:
    """The complete readable fight transcript sent after a persistent result image."""
    lines = ["<b>Лог боя</b>", escape(result.opening)]
    if result.accident:
        lines.append(f"<b>{escape(result.accident)}</b>")
    else:
        lines.extend(escape(round_.text) for round_ in result.rounds)
        lines.append(f"<b>{escape(result.closing)}</b>")
    return "\n".join(lines)


def fight_report_keyboard(user_id) -> dict:
    return {"inline_keyboard": [
        [{"text": "⚔️ Ещё бой", "callback_data": callback_data(user_id, "search")}],
        _back_row(user_id),
    ]}


# -------------------------------------------------------------------------- history


def history_view(entry: str, user_id) -> tuple[str, dict]:
    """"Вы атаковали X (Поражение)" / "X атаковал вас (Победа, +40)" -- phrased from the
    reader's side, because a shared fight log written neutrally is unreadable at a glance:
    the first thing anybody wants to know is whether they started it and whether they won.
    """
    rows_data = pets.history(entry, user_id)
    lines = ["📜 <b>Последние бои</b>\n"]
    if not rows_data:
        lines.append("Боёв пока не было.")
    for record in rows_data:
        attacked = str(record.get("attacker_id")) == str(user_id)
        draw = bool(record.get("draw"))
        won = str(record.get("winner_id")) == str(user_id)
        other = record.get("defender_name") if attacked else record.get("attacker_name")
        owner = record.get("defender_owner") if attacked else record.get("attacker_owner")
        who = f"{escape(owner or '?')} — {escape(other or '?')}"
        outcome = "Ничья" if draw else ("Победа" if won else "Поражение")
        gold = record.get("gold") or 0
        lost = record.get("loss_gold") or 0
        # Bare numbers here, with no noun to agree with -- the line is already dense and
        # "(Победа, +45)" reads fine next to a column of them.
        if draw:
            outcome += f", +{C.DRAW_XP} опыта"
        elif won and gold:
            outcome += f", +{_money(gold)}"
        elif lost:
            outcome += f", −{_money(lost)}"
        if attacked:
            lines.append(f"⚔️ Вы напали: {who} ({outcome})")
        else:
            lines.append(f"🛡 На вас напали: {who} ({outcome})")

    rows = [[{"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")}], _back_row(user_id)]
    return "\n".join(lines), {"inline_keyboard": rows}


# ------------------------------------------------------------------------- pet card


def pet_card(entry: str, user_id, pet: dict) -> str:
    """What /pet prints, in a DM or in the group. Deliberately self-contained text with no
    buttons: it is sent as a photo caption when the creature has a picture, and a caption
    cannot carry a menu that would work for whoever else is reading the group."""
    effective = pets.effective_stats(entry, user_id)
    levels = pet.get("stats", {})
    level = pet.get("level", 1)
    fights = pet.get("fights", 0)
    wins = pet.get("wins", 0)

    lines = [f"🐾 <b>{_name(pet)}</b>"]
    lines.append(f"Хозяин: {escape(pet.get('owner_name') or 'кто-то')}")
    lines.append(f"⭐ Уровень {level}")
    need = C.pet_xp_for_next_level(level)
    if need:
        lines.append(f"✨ Опыт: {pet.get('xp', 0)} / {need}")
    lines.append("")
    for key in C.STAT_KEYS:
        purchased = levels.get(key, C.STAT_MIN_LEVEL)
        total = effective.get(key, purchased)
        extra = f" <i>(+{total - purchased})</i>" if total != purchased else ""
        lines.append(f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {total}{extra}")
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {effective.get('armor', 0)}")

    equipped = pet.get("equipped") or {}
    worn = [C.find_item(code) for code in equipped.values() if code]
    worn = [item for item in worn if item]
    lines.append("")
    if worn:
        lines.append("🎒 " + ", ".join(escape(item.name) for item in worn))
    else:
        lines.append("🎒 Без снаряжения")
    rate = f" ({round(100 * wins / fights)}%)" if fights else ""
    lines.append(f"🏆 Боёв: {fights} / побед: {wins}{rate}")
    return "\n".join(lines)


# --------------------------------------------------------------------- shared bits


def pet_view(entry: str, user_id) -> tuple[str, dict]:
    """The owner's own view of their creature: the same card /pet prints, plus the two
    buttons only the owner has any business pressing."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rows = [
        [
            {"text": "✏️ Переименовать", "callback_data": callback_data(user_id, "rename")},
            {"text": "🖼 Сменить фото", "callback_data": callback_data(user_id, "photo")},
        ],
        _back_row(user_id),
    ]
    return pet_card(entry, user_id, pet), {"inline_keyboard": rows}


def no_pet_view(user_id) -> tuple[str, dict]:
    text = (
        "У тебя ещё нет существа.\n\n"
        f"Сначала клетка ({_coins(C.CAGE_PRICE)}), потом приручение"
        f" ({_coins(C.TAME_PRICE)}).\n"
        "Существо должно быть твоей собственной раскрашенной фигуркой."
    )
    return text, {"inline_keyboard": [_back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    return text, {"inline_keyboard": [_back_row(user_id)]}
