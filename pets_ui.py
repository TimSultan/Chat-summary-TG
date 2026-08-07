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

CALLBACK_PREFIX = "pet"
# Telegram caps callback_data at 64 bytes. "pet:" + a 19-digit id + ":" + the longest
# action + ":" + the longest item code stays comfortably inside that.
MAX_CALLBACK_BYTES = 64

BACK_BUTTON = "◀️ Назад"


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


def _back_row(owner_id) -> list:
    return [{"text": BACK_BUTTON, "callback_data": callback_data(owner_id, "main")}]


def _money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".")


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
        lines.append(f"Клетка стоит {_money(C.CAGE_PRICE)} монет, приручение — {_money(C.TAME_PRICE)}.")
    elif not pet:
        lines.append(f"🏠 Клетка: уровень {cage} — пустая.")
        lines.append(f"Осталось приручить существо за {_money(C.TAME_PRICE)} монет.")
    else:
        left = pets.fights_left(entry, user_id, pets.today())
        lines.append(f"🐾 {_name(pet)} — уровень {pet.get('level', 1)}")
        lines.append(f"🏠 Клетка: уровень {cage}")
        lines.append(f"⚔️ Боёв сегодня осталось: {left}")
        lines.append(f"🏆 Боёв: {pet.get('fights', 0)} / побед: {pet.get('wins', 0)}")
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows = [
        [{"text": "🏠 Клетка", "callback_data": callback_data(user_id, "cage")}],
    ]
    if pet:
        rows.append([
            {"text": "🐾 Существо", "callback_data": callback_data(user_id, "pet")},
            {"text": "💪 Прокачка", "callback_data": callback_data(user_id, "train")},
        ])
        rows.append([
            {"text": "🎒 Инвентарь", "callback_data": callback_data(user_id, "bag")},
            {"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")},
        ])
        rows.append([{"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")}])
    elif cage:
        rows.append([{
            "text": f"🐣 Приручить существо — {_money(C.TAME_PRICE)}",
            "callback_data": callback_data(user_id, "tame"),
        }])
    rows.append([{"text": "🔄 Обновить", "callback_data": callback_data(user_id, "main")}])
    return "\n".join(lines), {"inline_keyboard": rows}


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
        lines.append(f"\nПокупка: {_money(C.CAGE_PRICE)} монет.")
    else:
        lines.append(f"Уровень {level} из {C.CAGE_MAX_LEVEL}.")
        lines.append(f"⚔️ Боёв в день: {C.DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[level - 1]}")
        lines.append(f"🪙 Прибавка к добыче: +{C.CAGE_GOLD_BONUS_PCT[level - 1]}%")
        if level < C.CAGE_MAX_LEVEL:
            nxt = C.CAGE_UPGRADE_COSTS[level]
            lines.append(
                f"\nСледующий уровень — {_money(nxt)} монет:"
                f" боёв в день {C.DAILY_FIGHTS + C.CAGE_BONUS_FIGHTS[level]},"
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
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    coins = pets.balance_for(entry, user_id, xp)
    equipped = pet.get("equipped", {})
    owned = pet.get("inventory", [])

    lines = ["🎒 <b>Инвентарь</b>\n"]
    for slot in C.SLOT_KEYS:
        code = equipped.get(slot)
        item = C.find_item(code) if code else None
        lines.append(
            f"{C.SLOT_EMOJI[slot]} {C.SLOT_NAMES[slot]}: "
            + (f"<b>{escape(item.name)}</b> — {_bonus_text(item)}" if item else "пусто")
        )
    lines.append(f"\n🪙 Монеты: {_money(coins)}")

    rows = []
    for slot in C.SLOT_KEYS:
        rows.append([{
            "text": f"{C.SLOT_EMOJI[slot]} {C.SLOT_NAMES[slot]}",
            "callback_data": callback_data(user_id, "slot", slot),
        }])
    if owned:
        lines.append(f"\n<i>В сумке: {len(owned)} предмет(ов).</i>")
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def slot_view(entry: str, user_id, xp: int, slot: str) -> tuple[str, dict]:
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
    worn = (pet.get("equipped") or {}).get(slot)

    lines = [f"{C.SLOT_EMOJI[slot]} <b>{escape(C.SLOT_NAMES[slot])}</b>\n"]
    rows = []
    for item in C.items_for_slot(slot):
        mark = " ✅" if item.code == worn else ""
        if item.code in owned:
            state = "в сумке"
        elif item.source == "drop":
            state = "только из боёв"
        else:
            state = f"{_money(item.price)} монет"
        lines.append(f"<b>{escape(item.name)}</b>{mark} — {_bonus_text(item)} · {state}")
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
        elif item.source == "shop":
            rows.append([{
                "text": f"Купить {item.name} — {_money(item.price)}",
                "callback_data": callback_data(user_id, "buy", item.code),
            }])
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows.append([{"text": "🎒 К инвентарю", "callback_data": callback_data(user_id, "bag")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def slot_of(code: str) -> str:
    """Which slot an item code belongs to, falling back to the first slot for a code that
    no longer exists in the catalogue -- an item can be retired from ITEMS while a player
    still has it in the bag, and a redraw must not blow up because of that."""
    item = C.find_item(code)
    return item.slot if item else C.SLOT_KEYS[0]


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

    lines = ["⚔️ <b>Арена</b>\n"]
    lines.append(f"{_name(pet)} — уровень {pet.get('level', 1)}")
    lines.append(f"Боёв сегодня осталось: {left}")
    lines.append(
        f"\nСоперник подбирается случайно среди существ"
        f" ±{C.OPPONENT_LEVEL_WINDOW} уровня."
    )
    if left <= 0:
        lines.append("\nНа сегодня всё. Заходи завтра — или улучши клетку, она даёт больше боёв.")

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
        [{"text": "🔍 Другой соперник", "callback_data": callback_data(user_id, "search")}],
        _back_row(user_id),
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


def fight_report(result, mine_key: str, names: dict, reward: dict | None) -> str:
    """The blow-by-blow. Every round is one line of flavour, then the outcome and what it
    paid. Long by design -- the log IS the game, the numbers are the receipt."""
    lines = [f"⚔️ <b>{escape(result.opening)}</b>\n"]
    for round_ in result.rounds:
        lines.append(escape(round_.text))
    lines.append("")
    if result.stopped_early:
        lines.append("<i>Судья остановил бой: слишком долго.</i>")
    lines.append(f"<b>{escape(result.closing)}</b>")

    won = result.winner == mine_key
    lines.append("")
    lines.append("🏆 <b>Победа</b>" if won else "💀 <b>Поражение</b>")
    if reward:
        if reward.get("gold"):
            lines.append(f"🪙 +{_money(reward['gold'])} монет")
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
        won = str(record.get("winner_id")) == str(user_id)
        other = record.get("defender_name") if attacked else record.get("attacker_name")
        owner = record.get("defender_owner") if attacked else record.get("attacker_owner")
        who = f"{escape(owner or '?')} — {escape(other or '?')}"
        outcome = "Победа" if won else "Поражение"
        gold = record.get("gold") or 0
        if won and gold:
            outcome += f", +{_money(gold)}"
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
        f"Сначала клетка ({_money(C.CAGE_PRICE)} монет), потом приручение"
        f" ({_money(C.TAME_PRICE)} монет)."
    )
    return text, {"inline_keyboard": [_back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    return text, {"inline_keyboard": [_back_row(user_id)]}
