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

from html import escape

import pets
import pets_config as C
import pets_flavor

CALLBACK_PREFIX = "pet"
# Telegram caps callback_data at 64 bytes. "pet:" + a 19-digit id + ":" + the longest
# action + ":" + the longest item code stays comfortably inside that.
MAX_CALLBACK_BYTES = 64

BACK_BUTTON = "◀️ Назад"
LEADERBOARD_PAGE_SIZE = 20
SLOT_PAGE_SIZE = 8
INVENTORY_PAGE_SIZE = 6
COLLECTION_PAGE_SIZE = 8
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
    else:
        left = pets.fights_left(entry, user_id, pets.today())
        lines.append(f"🐾 {_name(pet)} — уровень {pet.get('level', 1)}")
        lines.append(f"🏠 Клетка: уровень {cage}")
        lines.append(f"⚔️ Боёв сегодня осталось: {left}")
        lines.append(f"🏆 Боёв: {pet.get('fights', 0)} / побед: {pet.get('wins', 0)}")
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows = [
        [{"text": "🐹 Хомяколатор", "callback_data": callback_data(user_id, "hamsterator")}],
        [{"text": "🏠 Клетка", "callback_data": callback_data(user_id, "cage")}],
        [{"text": "🏆 Существа сервера", "callback_data": callback_data(user_id, "leaderboard")}],
    ]
    if pet:
        rows.append([
            {"text": "🐾 Существо", "callback_data": callback_data(user_id, "pet")},
            {"text": "💪 Прокачка", "callback_data": callback_data(user_id, "train")},
        ])
        rows.append([
            {"text": "🎒 Снаряжение", "callback_data": callback_data(user_id, "bag")},
            {"text": "🛒 Магазин", "callback_data": callback_data(user_id, "store")},
        ])
        rows.append([
            {"text": "📚 Коллекция", "callback_data": callback_data(user_id, "collection")},
            {"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")},
        ])
        rows.append([{"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")}])
    elif cage:
        rows.append([{
            "text": f"🐣 Приручить существо — {_money(C.TAME_PRICE)}",
            "callback_data": callback_data(user_id, "tame"),
        }])
    rows.append([{"text": "ℹ️ Как играть", "callback_data": callback_data(user_id, "info")}])
    rows.append([{"text": "🔄 Обновить", "callback_data": callback_data(user_id, "main")}])
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
    lines.append("3. Сражайся в боте через /arena: соперник выбирается случайно среди тех, кого сейчас можно атаковать. Боёв больше за активность в чате и клетку.")
    lines.append(
        f"4. Победа приносит {C.WIN_GOLD_MIN}–{C.WIN_GOLD_MAX} монет и опыт. "
        f"Поражение забирает только {round(C.LOSS_GOLD_SHARE * 100)}% награды, без долгов."
    )
    lines.append("5. Ненужную снятую экипировку можно продать или подарить владельцу другого существа.")
    lines.append("6. Хомяколатор копит монеты за полностью прошедшие часы, пока его склад не заполнится.")
    lines.append("\n<b>Статы</b>")
    lines.append("Сила увеличивает урон. Здоровье повышает HP. Ловкость даёт уклонение. Удача повышает шанс крита.")
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
        # The cage adds fights on top of what activity earned, so it is shown as a bonus
        # rather than as a total -- a total here would contradict the number on the arena
        # screen, which knows yesterday's activity and this screen does not.
        lines.append(f"⚔️ Боёв в день: +{C.CAGE_BONUS_FIGHTS[level - 1]}")
        lines.append(f"🪙 Прибавка к добыче: +{C.CAGE_GOLD_BONUS_PCT[level - 1]}%")
        if level < C.CAGE_MAX_LEVEL:
            nxt = C.CAGE_UPGRADE_COSTS[level]
            lines.append(
                f"\nСледующий уровень — {_coins(nxt)}:"
                f" боёв в день +{C.CAGE_BONUS_FIGHTS[level]},"
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


# -------------------------------------------------------------------- hamsterator


def hamsterator_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """Passive-income facility view; opening it collects any complete stored hours."""
    before = pets.passive_income_status(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)
    level = pets.hamsterator_level(entry, user_id)
    rate = C.HAMSTERATOR_GOLD_PER_HOUR[level]
    cap = C.HAMSTERATOR_STORAGE_CAP[level]
    lines = ["🐹 <b>Хомяколатор</b>\n"]
    if not pets.has_cage(entry, user_id):
        lines.append("Сначала нужна клетка: хомякам негде крутить монетный барабан.")
    else:
        lines.append(f"Уровень {level} из {C.HAMSTERATOR_MAX_LEVEL}.")
        lines.append(f"🪙 Добыча: +{rate} монет/ч.")
        lines.append(f"📦 Склад: до {_money(cap)} монет.")
        if before.get("stored"):
            lines.append(f"✅ Собрано сейчас: +{_money(before['stored'])}.")
        elif level:
            next_at = before["next_hour"].strftime("%H:%M")
            lines.append(f"⏱ Следующая монета — в {next_at}.")
        if level < C.HAMSTERATOR_MAX_LEVEL:
            cost = C.HAMSTERATOR_UPGRADE_COSTS[level]
            next_rate = C.HAMSTERATOR_GOLD_PER_HOUR[level + 1]
            next_cap = C.HAMSTERATOR_STORAGE_CAP[level + 1]
            lines.append(
                f"\nСледующий уровень — {_coins(cost)}: +{next_rate} монет/ч, "
                f"склад {_money(next_cap)}."
            )
    lines.append(f"\n🪙 У тебя: {_money(coins)}")
    rows = []
    if pets.has_cage(entry, user_id) and level < C.HAMSTERATOR_MAX_LEVEL:
        rows.append([{
            "text": f"⬆️ Улучшить — {_money(C.HAMSTERATOR_UPGRADE_COSTS[level])}",
            "callback_data": callback_data(user_id, "uphamsterator"),
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
    daily_weapon_codes = {item.code for item in C.daily_storefront_weapons(entry, pets.today())}

    # Owned gear must stay reachable even when its catalogue code lives on page 63.
    # Equipped first, then the rest of the bag, then unowned catalogue stock.
    all_items = sorted(
        C.items_for_slot(slot),
        key=lambda item: (item.code != worn, item.code not in owned, item.code),
    )
    total_pages = max(1, (len(all_items) + SLOT_PAGE_SIZE - 1) // SLOT_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = all_items[page * SLOT_PAGE_SIZE:(page + 1) * SLOT_PAGE_SIZE]
    lines = [f"{C.SLOT_EMOJI[slot]} <b>{escape(C.SLOT_NAMES[slot])}</b> · {page + 1}/{total_pages}\n"]
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
        elif item.source == "shop" and (
            item.slot != "weapon" or item.code in daily_weapon_codes
        ):
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


def _rarity_buttons(user_id, action: str, selected: str, *, paged: bool = False) -> list:
    short = {"all": "Все", "cursed": "☠️", "common": "⚪", "uncommon": "🟢", "rare": "🔵", "legendary": "🟡"}
    buttons = []
    for rarity in RARITY_FILTERS:
        text = short[rarity] + (" ✓" if rarity == selected else "")
        argument = collection_argument(rarity, 0) if paged else rarity
        buttons.append({"text": text, "callback_data": callback_data(user_id, action, argument)})
    return [buttons[:3], buttons[3:]]


def store_view(entry: str, user_id, xp: int, rarity: str = "all") -> tuple[str, dict]:
    """The 16-item daily weapon window plus direct routes to accessory shelves."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, _ = _rarity_argument(rarity)
    owned = set(pet.get("inventory", []))
    stock = C.daily_storefront_weapons(entry, pets.today())
    visible = [item for item in stock if rarity == "all" or item.rarity == rarity]
    lines = ["🛒 <b>Витрина дня</b>", "Сегодня в продаже 16 оружий. Завтра ассортимент сменится."]
    lines.append(f"Фильтр: <b>{RARITY_FILTER_NAMES[rarity]}</b>\n")
    if not visible:
        lines.append("Сегодня оружия этой редкости не завезли.")
    rows = _rarity_buttons(user_id, "store", rarity)
    for item in visible:
        state = "у тебя уже есть" if item.code in owned else _coins(item.price)
        label = C.RARITY_LABELS.get(item.rarity, item.rarity)
        lines.append(f"<b>{escape(item.name)}</b> · {label} · {_bonus_text(item)} · {state}")
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")
        if item.code not in owned:
            rows.append([{
                "text": f"Купить · {_money(item.price)}",
                "callback_data": callback_data(user_id, "buy", item.code),
            }])
    lines.append(f"\n🪙 Монеты: {_money(pets.balance_for(entry, user_id, xp))}")
    rows.extend([
        [{
            "text": f"{C.SLOT_EMOJI['amulet']} {C.SLOT_NAMES['amulet']}",
            "callback_data": callback_data(user_id, "slot", slot_argument("amulet")),
        }, {
            "text": f"{C.SLOT_EMOJI['gloves']} {C.SLOT_NAMES['gloves']}",
            "callback_data": callback_data(user_id, "slot", slot_argument("gloves")),
        }],
        [{
            "text": f"{C.SLOT_EMOJI['boots']} {C.SLOT_NAMES['boots']}",
            "callback_data": callback_data(user_id, "slot", slot_argument("boots")),
        }],
        [{"text": "🎒 Моё снаряжение", "callback_data": callback_data(user_id, "bag")}],
        [{"text": "📚 Коллекция", "callback_data": callback_data(user_id, "collection")}],
        _back_row(user_id),
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


def collection_view(entry: str, user_id, xp: int, argument: str = "") -> tuple[str, dict]:
    """A permanent 500-weapon book: owned, seen before, and still unknown."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, page = _rarity_argument(argument, with_page=True)
    weapons = sorted(C.items_for_slot("weapon"), key=lambda item: item.code)
    discovered = set(pet.get("discovered", []))
    owned = set(pet.get("inventory", []))
    lines = ["📚 <b>Книга оружия</b>"]
    lines.append(f"Открыто: <b>{len(discovered & {item.code for item in weapons})}/{len(weapons)}</b> · в сумке: {len(owned & {item.code for item in weapons})}")
    pity_progress = getattr(pets, "legendary_pity_progress", None)
    if callable(pity_progress):
        pity = pity_progress(entry, user_id)
        if pity.get("eligible"):
            lines.append(
                f"🟡 До гарантированной легендарки: {pity['wins_without_legend']}/{pity['threshold']} побед "
                f"(осталось {pity['remaining_wins']})."
            )
    for one_rarity in RARITY_FILTERS[1:]:
        group = [item for item in weapons if item.rarity == one_rarity]
        seen = sum(item.code in discovered for item in group)
        lines.append(f"{C.RARITY_LABELS.get(one_rarity, one_rarity)}: {seen}/{len(group)}")
    visible_all = [item for item in weapons if rarity == "all" or item.rarity == rarity]
    total_pages = max(1, (len(visible_all) + COLLECTION_PAGE_SIZE - 1) // COLLECTION_PAGE_SIZE)
    page = min(page, total_pages - 1)
    visible = visible_all[page * COLLECTION_PAGE_SIZE:(page + 1) * COLLECTION_PAGE_SIZE]
    lines.append(f"\n<b>{RARITY_FILTER_NAMES[rarity]}</b> · {page + 1}/{total_pages}")
    for item in visible:
        state = "✅ в сумке" if item.code in owned else "👁 открыто" if item.code in discovered else "▫️ ???"
        name = escape(item.name) if item.code in discovered else "Неизвестное оружие"
        lines.append(f"{state} · <b>{name}</b> · {C.RARITY_LABELS.get(item.rarity, item.rarity)}")
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
    return ", ".join(parts) or "без бонусов"


# ---------------------------------------------------------------------------- arena


def fight_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    left = pets.fights_left(entry, user_id, pets.today())

    messages, figurines = pets.yesterday_activity(entry, user_id, pets.today())
    allowance = pets.daily_allowance(entry, user_id, pets.today())

    lines = ["⚔️ <b>Арена</b>\n"]
    lines.append(f"{_name(pet)} — уровень {pet.get('level', 1)}")
    lines.append(f"Боёв сегодня осталось: {left} из {allowance}")
    # Spelled out rather than left as a number, because the allowance is the one thing in
    # the game that rewards chatting, and a limit nobody understands reads as arbitrary.
    lines.append(
        f"\n<i>Бои начисляются за вчерашний день: {C.BASE_DAILY_FIGHTS} базовых"
        f" + за сообщения + {C.FIGHTS_PER_FIGURINE} за каждый #япокрасил."
        f" Вчера у тебя: {_plural(messages, 'сообщение', 'сообщения', 'сообщений')},"
        f" работ — {figurines}.</i>"
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
    lines.append(
        "Если ты на 7+ уровней выше соперника, охранник остановит бой: "
        "без золота и дропа, зато +5 опыта."
    )
    if left <= 0:
        lines.append("\nНа сегодня всё. Пиши в чат — завтра боёв будет больше.")

    rows = []
    if left > 0:
        rows.append([{
            "text": "🔍 Найти соперника",
            "callback_data": callback_data(user_id, "search"),
        }])
    rows.append([{"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def opponent_view(entry: str, user_id, opponent_id, xp: int) -> tuple[str, dict]:
    """The found opponent, with Напасть as a separate tap. Searching and attacking are two
    steps because the daily fight is spent by attacking, not by looking -- a player who
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


def group_fight_result_view(
    result, attacker_id: str, attacker_name: str, defender_name: str, reward: dict,
    arena_url: str | None,
) -> tuple[str, dict | None]:
    """One-line public result; detailed receipts stay with the two fighters in private."""
    if result.is_draw:
        text = f"🤝 <b>{escape(attacker_name)} и {escape(defender_name)} сыграли вничью.</b>"
    else:
        winner_name = attacker_name if result.winner == str(attacker_id) else defender_name
        winner_reward = reward if result.winner == str(attacker_id) else {
            "gold": reward.get("opponent_gold", 0),
            "xp": reward.get("opponent_xp", 0),
        }
        text = (
            f"🏆 <b>{escape(pets_flavor.public_result_line(winner_name))}</b>\n"
            f"🪙 +{_coins(winner_reward.get('gold', 0))}  ✨ +{winner_reward.get('xp', 0)} опыта"
        )
    keyboard = {"inline_keyboard": [[{"text": "⚔️ Открыть арену", "url": arena_url}]]} if arena_url else None
    return text, keyboard


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
        guardian = bool(record.get("guardian_intervention"))
        draw = bool(record.get("draw"))
        won = str(record.get("winner_id")) == str(user_id)
        other = record.get("defender_name") if attacked else record.get("attacker_name")
        owner = record.get("defender_owner") if attacked else record.get("attacker_owner")
        who = f"{escape(owner or '?')} — {escape(other or '?')}"
        outcome = (
            f"Охранник вмешался, +{record.get('xp', C.GUARDIAN_XP)} опыта"
            if guardian else "Ничья" if draw else ("Победа" if won else "Поражение")
        )
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
        f" ({_coins(C.TAME_PRICE)})."
    )
    return text, {"inline_keyboard": [_back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    return text, {"inline_keyboard": [_back_row(user_id)]}
