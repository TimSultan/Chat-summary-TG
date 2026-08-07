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


def search_argument(rerolls_used: int, search_seed: int) -> str:
    """Carry a deterministic opponent-cycle token within Telegram's callback cap."""
    return f"{rerolls_used},{search_seed}"


def parse_search_argument(argument: str) -> tuple[int, int | None]:
    if not argument:
        return 0, None
    try:
        count_text, seed_text = argument.split(",", 1)
        count = int(count_text)
        seed = int(seed_text)
    except (TypeError, ValueError):
        return 0, None
    return max(0, count), seed if seed >= 0 else None


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
        lines.append(
            f"\n<i>В сумке: {_plural(len(owned), 'предмет', 'предмета', 'предметов')}.</i>"
        )
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
            state = _coins(item.price)
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
        f"\nСоперник подбирается по боевому рейтингу:"
        f" учитываются статы, уровень существа и снаряжение."
    )
    lines.append(
        f"Победа: {C.WIN_GOLD_MIN}–{C.WIN_GOLD_MAX} монет."
        f" Поражение: минус половина от этого."
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


def opponent_view(
    entry: str, user_id, opponent_id, xp: int, rerolls_used: int = 0,
    rerolls_allowed: int = C.MAX_OPPONENT_REROLLS, search_seed: int = 0,
) -> tuple[str, dict]:
    """The found opponent, with Напасть as a separate tap. Searching and attacking are two
    steps because the daily fight is spent by attacking, not by looking -- a player who
    searched and walked away has lost nothing."""
    mine = pets.get_pet(entry, user_id)
    theirs = pets.get_pet(entry, opponent_id)
    if not mine or not theirs:
        return fight_view(entry, user_id, xp)
    their_stats = pets.effective_stats(entry, opponent_id)
    my_power = pets.power_rating(entry, user_id)
    their_power = pets.power_rating(entry, opponent_id)

    lines = ["🔍 <b>Соперник найден</b>\n"]
    lines.append(f"🐾 <b>{_name(theirs)}</b> — уровень {theirs.get('level', 1)}")
    lines.append(f"Хозяин: {escape(theirs.get('owner_name') or 'кто-то')}")
    lines.append(
        f"Боёв: {theirs.get('fights', 0)} / побед: {theirs.get('wins', 0)}"
    )
    lines.append(f"Боевой рейтинг: {their_power} <i>(у тебя {my_power})</i>")
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
    if rerolls_used < rerolls_allowed:
        left = rerolls_allowed - rerolls_used
        rows.insert(1, [{
            "text": f"🔍 Другой соперник ({left})",
            "callback_data": callback_data(
                user_id, "search", search_argument(rerolls_used + 1, search_seed),
            ),
        }])
    else:
        lines.append(f"\n<i>Новых соперников: максимум {rerolls_allowed}.</i>")
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
        f" ({_coins(C.TAME_PRICE)})."
    )
    return text, {"inline_keyboard": [_back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    return text, {"inline_keyboard": [_back_row(user_id)]}
