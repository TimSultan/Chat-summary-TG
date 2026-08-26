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

from datetime import date, datetime, timedelta, timezone
from html import escape
from collections import Counter

import economy
import casino
import donations
import pets
import pets_combat
import pets_config as C
import pets_scroll_catalog as SCROLLS
import pets_updates
import quests
import stats

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
RARITY_FILTERS = ("all", "cursed", "common", "rare", "legendary")
RARITY_FILTER_NAMES = {
    "all": "Все", "cursed": "Проклятые", "common": "Обычные",
    "rare": "Редкие", "legendary": "Легендарные",
}

PERSONAL_PAINT_TARGET_NAMES = {
    "weapon": "оружие", "shield": "щит", "boots": "ботинки",
    "amulet": "амулет", "vial": "лечебный пузырёк", "scroll": "свиток",
}


def _personal_paint_bonus_text(target: str) -> str:
    if target == "scroll":
        return "сила полезных чисел свитка +30%; шанс и длительность не меняются"
    if target == "vial":
        return "сила лечения +30%; порог и частота срабатывания не меняются"
    return "положительные статы предмета +30%"


def _quest_benefit_text(card: dict) -> str:
    """One plain-language outcome line shared by Telegram quest shelves/details."""
    reward = card.get("reward") or {}
    tool = reward.get("tool_masterwork")
    if tool == "shovel":
        return "После приёмки: бесконечная лопата и +50% золота с каждой смены — навсегда."
    if tool == "pickaxe":
        return "После приёмки: бесконечная кирка и +50% ко всей добыче — навсегда."
    if tool == "farmer":
        return (
            "После приёмки навсегда: +25% опыта со смены фермы. "
            "А вместе с фигуркой шахтёра — ферма и карьер работают одновременно."
        )
    if tool == "miner":
        return (
            "После приёмки навсегда: +25% опыта из карьера. "
            "А вместе с фигуркой фермера — ферма и карьер работают одновременно."
        )
    target = str(reward.get("personal_paint_target") or "")
    target_name = PERSONAL_PAINT_TARGET_NAMES.get(target)
    if target_name:
        return (
            f"После приёмки навсегда: персональная руна для типа «{target_name}», "
            f"{_personal_paint_bonus_text(target)}; фото покраса можно поставить картинкой цели."
        )
    if reward.get("magic_guaranteed"):
        return "После приёмки: случайная магия и случайная руна."
    return ""


def _scroll_effect_lines(spell: dict, painted: bool = False) -> tuple[str, ...]:
    if not painted:
        return SCROLLS.effect_lines(spell)
    effects = pets_combat.resolved_scroll_effects(spell, True)
    return SCROLLS.effect_lines({**dict(spell), "effects": effects})


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
    # Some actions carry a compound argument (`mob-code:tier`, for example). Callback
    # data itself is already separated into owner/action/argument by the first three
    # fields, so the argument has to be joined back together rather than silently losing
    # everything after its first colon.
    return parts[1], parts[2], ":".join(parts[3:])


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


def main_view(
    entry: str, user_id, xp: int, webapp_url: str | None = None, quest_mod: bool = False,
    quest_pending: int = 0, finance_admin: bool = False,
) -> tuple[str, dict]:
    """The landing screen. Deliberately shows the whole state of the account in six lines
    -- cage, creature, level, coins, fights left -- because every other screen is one tap
    away and re-reading this one is how a player checks whether anything changed.

    `webapp_url` puts the Mini App (pets_web.py) at the top as a web_app button. Passed in
    rather than built here because only the caller knows both the configured public URL and
    that this particular message is going to a PRIVATE chat -- Telegram rejects a web_app
    button anywhere else. Absent, the menu is exactly what it was: the whole game is still
    playable from these buttons, and the page is an alternative, not a replacement.
    """
    coins = pets.balance_for(entry, user_id, xp)
    pet = pets.get_pet(entry, user_id)
    cage = pets.cage_level(entry, user_id)

    lines = ["🏟 <b>Арена</b>\n"]
    if not cage:
        lines.append("У тебя пока нет клетки, а значит и существа.")
        lines.append(f"Клетка стоит {_coins(C.CAGE_PRICE)}, приручение — {_coins(C.TAME_PRICE)}.")
    elif not pet:
        lines.append(f"🏠 Базовая клетка: уровень {cage} — готова.")
        lines.append("Создай существо бесплатно.")
        lines.append("Это должна быть твоя покрашенная работа: она будет участвовать в боях против других игроков.")
    else:
        fights = pets.fight_allowance_breakdown(entry, user_id, pets.today())
        left = fights["available"]
        capacity = fights["capacity"]
        lines.append(f"🐾 {_name(pet)} — уровень {pet.get('level', 1)}")
        element = pet.get("element")
        if element in C.CHARACTER_ELEMENTS:
            lines.append(
                f"{C.CHARACTER_ELEMENT_ICONS[element]} Элемент: "
                f"{escape(C.CHARACTER_ELEMENT_NAMES[element])}"
            )
        else:
            lines.append("⚪ <b>Выбери элемент</b> — он даст +10% урона в выгодном матче.")
        # Announced before anything else about the creature: a level waiting to be bought
        # is the one thing on this screen the player can act on right now.
        ready = pets.level_up_status(entry, user_id)
        if ready.get("pending"):
            more = f" (ещё {ready['pending'] - 1})" if ready["pending"] > 1 else ""
            lines.append(
                f"⬆️ <b>Уровень {ready['level'] + 1} готов{more}</b> — "
                f"+{ready['stat_bonus']} ко всем статам за {ready['cost']} 💎"
            )
        lines.append(f"🏠 Клетка: уровень {cage}")
        lines.append(f"⚔️ Боёв в запасе: {left} из {capacity}")
        lines.append(f"🏆 Боёв: {pet.get('fights', 0)} / побед: {pet.get('wins', 0)}")
    lines.append(f"🪙 Монеты: {_money(coins)}")

    rows = []
    if pet and pet.get("element") not in C.CHARACTER_ELEMENTS:
        rows.append([{
            "text": "✨ Выбрать элемент персонажа",
            "callback_data": callback_data(user_id, "elementmenu"),
        }])
    # Above even «Открыть игру»: it is a reward the player has already earned and only has
    # to collect, and burying that under navigation is how it goes unnoticed for a week.
    if pet and pets.level_up_status(entry, user_id).get("pending"):
        rows.append([{
            "text": (f"⬆️ Поднять уровень · {C.PET_LEVEL_UP_RUBY_COST} 💎 "
                     f"(+{C.PET_LEVEL_STAT_BONUS} ко всем статам)"),
            "callback_data": callback_data(user_id, "claimlevel"),
        }])
    if webapp_url:
        # First, and alone on its row: it is the whole game rather than one more screen.
        rows.append([{"text": "🎮 Открыть игру", "web_app": {"url": webapp_url}}])
    quest_button = ("❗ " if quests.has_available_quests(entry, user_id) else "") + "📜 Квесты"
    # An owed reward outranks a mere unread mark: it says what is waiting, not just that
    # something is. Same split as the Mini App's HUD gift -- reading a note clears the
    # "❗" but leaves the 🎁 until the diamonds are actually taken.
    owed = pets_updates.claimable(entry, user_id)
    owed_rubies = sum(row.reward_rubies for row in owed)
    owed_tickets = sum(row.reward_tickets for row in owed)
    prize = " · ".join(part for part in (
        f"{owed_rubies} 💎" if owed_rubies else "",
        f"{owed_tickets} 🎫" if owed_tickets else "",
    ) if part)
    if prize:
        updates_button = f"🎁 Обновления · {prize}"
    elif pets_updates.has_unread(entry, user_id):
        updates_button = "❗ 📰 Обновления"
    else:
        updates_button = "📰 Обновления"

    # Play first, account utilities second. These four rows remain in the same order for
    # newcomers and established players, so muscle memory survives taming a creature.
    rows.append([
        {"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")},
        {"text": "🌾 Ферма", "callback_data": callback_data(user_id, "farm")},
    ])
    rows.append([
        {"text": quest_button, "callback_data": callback_data(user_id, "quests")},
        {"text": "🎰 Казино", "callback_data": callback_data(user_id, "casino")},
    ])
    rows.append([
        {"text": "🛒 Магазин", "callback_data": callback_data(user_id, "store")},
        {"text": "🎒 Снаряжение", "callback_data": callback_data(user_id, "bag")},
    ])

    # A claimed bonus is no longer a dead menu entry for the rest of the day.
    if economy.daily_bonus_status(entry, user_id).get("can_claim"):
        rows.append([{
            "text": "🎁 Забрать ежедневный бонус",
            "callback_data": callback_data(user_id, "dailybonus"),
        }])

    if pet:
        rows.append([
            {"text": "🖼 Существо", "callback_data": callback_data(user_id, "pet")},
            {"text": "💪 Прокачка", "callback_data": callback_data(user_id, "train")},
        ])
        rows.append([
            {"text": "📬 Почта", "callback_data": callback_data(user_id, "mail")},
            {"text": "🏆 Существа сервера", "callback_data": callback_data(user_id, "leaderboard")},
        ])
        rows.append([{
            "text": "🕳 Подземелье", "callback_data": callback_data(user_id, "dungeon"),
        }])
        notifications_enabled = pets.fight_result_notifications_enabled(entry, user_id)
        rows.append([
            {
                "text": "🔔 Результаты: вкл." if notifications_enabled else "🔕 Результаты: выкл.",
                "callback_data": callback_data(user_id, "fightnotify"),
            },
            {
                "text": updates_button,
                "callback_data": callback_data(user_id, "updates"),
            },
        ])
    else:
        rows.append([
            {"text": "🖼 Существо", "callback_data": callback_data(user_id, "pet")},
            {"text": "🏆 Существа сервера", "callback_data": callback_data(user_id, "leaderboard")},
        ])
        rows.append([
            {"text": "📬 Почта", "callback_data": callback_data(user_id, "mail")},
            {"text": updates_button, "callback_data": callback_data(user_id, "updates")},
        ])
    if quest_mod:
        # Only drawn for somebody who can actually review. Whether that is true needs a
        # Telegram round trip, so the CALLER decides and passes it in -- this module stays
        # pure, and the routes behind the button re-check for themselves regardless.
        pending_marker = "🔴 " if quest_pending else ""
        if webapp_url:
            rows.append([{
                "text": f"{pending_marker}🛡 Проверка квестов",
                "web_app": {"url": f"{webapp_url}?view=review"},
            }])
        rows.append([{
            "text": f"{pending_marker}🛡 Модераторы квестов",
            "callback_data": callback_data(user_id, "questmods"),
        }])
    if finance_admin and webapp_url:
        # A graph belongs in the Mini App, but its entry should also be reachable from
        # the Telegram menu admins already use. The web route independently re-checks
        # the financial-admin gate before returning a single transaction.
        rows.append([{
            "text": "🕵️ Денежный аудит",
            "web_app": {"url": f"{webapp_url}?view=economy"},
        }])
    # Three across, so the last row of the menu is three small buttons rather than two
    # and then a full-width one. The collection is a standing offer: it should be
    # findable without ever taking up the width of something you actually came here to
    # press. Telegram sizes a row's buttons equally, so sharing the row IS the size --
    # which is also why the label loses the word "проект" here and keeps it on the
    # screen it opens.
    rows.append([
        {"text": "ℹ️ Как играть", "callback_data": callback_data(user_id, "info")},
        {"text": "🔄 Обновить", "callback_data": callback_data(user_id, "main")},
        {"text": "💜 Поддержать", "callback_data": callback_data(user_id, "support")},
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
    """The few ideas a newcomer needs; individual screens teach their own details."""
    lines = [
        "ℹ️ <b>Как играть</b>\n",
        "Открой /arena в личке бота — здесь живёт вся игра.",
        "\n<b>1. Создай своё существо</b>",
        "Создай существо из своей покрашенной работы. "
        "<b>На картинке существа должен быть именно твой собственный покрас</b> — "
        "загрузи фотографию своей раскрашенной миниатюры; она будет участвовать в боях против других игроков.",
        "\n<b>2. Развивай его</b>",
        "Прокачивай характеристики, находи оружие и экипировку, собирай подходящий комплект.",
        "\n<b>3. Играй</b>",
        "Сражайся с игроками и мобами, выполняй квесты, отправляй существо на ферму "
        "и рискуй монетами в казино.",
        "\n<b>4. Получай награды</b>",
        "Монеты, опыт и вещи приходят за активность в чате, победы, квесты и ферму. "
        "Каждый экран сам подскажет, что можно сделать дальше.",
    ]
    return "\n".join(lines), {"inline_keyboard": [_back_row(user_id)]}


def dungeon_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    state = pets.dungeon_status(entry, user_id)
    if not state.get("available"):
        rows = []
        if state.get("active"):
            rows.append([{"text": "🚪 Вернуться", "callback_data": callback_data(user_id, "dungeonquit")}])
        rows.append(_back_row(user_id))
        return (
            "🕳 <b>Подземелье закрыто</b>\n\n"
            f"{escape(str(state.get('closed_notice') or 'Подземелье временно закрыто.'))}",
            {"inline_keyboard": rows},
        )
    if not state.get("active"):
        power = int(state.get("power", 0))
        needed = int(state.get("min_power", 1000))
        tickets = int(state.get("tickets", 0) or 0)
        lines = ["🕳 <b>Подземелье</b>", ""]
        # The last descent's receipt, first and in full. This is the screen a player is
        # dropped on the instant a run ends, so it is the only place a dead run's takings
        # can still be read -- and until now it printed the entry price instead.
        finished = dungeon_finished_text(state.get("last_haul"))
        if finished:
            lines.extend([finished, ""])
        lines.extend([f"⚡ Сила: <b>{_money(power)}</b> / {_money(needed)}",
             (f"🎫 Билетов в подземелье: <b>{tickets}</b>" if tickets else f"Вход: <b>{state.get('entry_cost', 15)} 💎</b>"),
                 "Состав этажей меняется, боссы каждые пять этажей. Здоровье не восстанавливается после боя."])
        rows = [[{"text": (f"⚔️ Войти · билет ({tickets})" if tickets else f"⚔️ Войти · {state.get('entry_cost', 15)} 💎"), "callback_data": callback_data(user_id, "dungeonenter")}]]
        rows.append(_back_row(user_id))
        return "\n".join(lines), {"inline_keyboard": rows}
    lines = [f"🕳 <b>{escape(str(state['theme']))}</b>", f"Этаж {state['floor']} · ❤️ {state['hp']} / {state['max_hp']}", escape(str(state.get('description') or '')), ""]
    rows = []
    # Above the floor, and above its buttons: the find is the one thing on this screen
    # that was not here a moment ago. It is never a gate -- the enemies below stay
    # playable with the box still standing, and the next descent clears it either way.
    chest_lines, chest_rows = dungeon_chest_block(state.get("chest"), user_id)
    lines.extend(chest_lines)
    rows.extend(chest_rows)
    healers_alive = int(state.get("healers_alive", 0) or 0)
    if healers_alive:
        lines.append(
            f"<i>✚ Целителей в живых: {healers_alive}. Пока они стоят, павшие поднимаются "
            f"снова и с них уже ничего не падает.</i>"
        )
    revived = set(state.get("revived") or [])
    for enemy in state.get("encounters", []):
        marker = "✅" if enemy.get("cleared") else (
            "✚" if enemy.get("healer") else "👑" if enemy.get("boss") else "⚔️")
        raised = " <i>(поднят)</i>" if enemy["index"] in revived and not enemy.get("cleared") else ""
        lines.append(f"{marker} {escape(str(enemy['name']))}{raised}" + (f" — {escape(str(enemy['hint']))}" if enemy.get("hint") else ""))
        # The rule of the fight, on its own line and never folded into the flavour: it is
        # the one thing a player has to act on before pressing attack.
        if enemy.get("weakness") and not enemy.get("cleared"):
            lines.append(f"   ⚠️ <b>{escape(str(enemy['weakness']))}</b>")
        # The stat block, only while the fight is still ahead: on a cleared row it is
        # noise about somebody already lying down.
        if enemy.get("stat_line") and not enemy.get("cleared"):
            lines.append(f"   <i>{escape(str(enemy['stat_line']))}</i>")
        if not enemy.get("cleared"):
            # The Phoenix is the one encounter that is not settled by the press that
            # starts it: its button opens the fight screen, where every turn is answered
            # by hand. Everything above -- the name, the hint, the weakness, the stat
            # line -- is the same card as any other boss's.
            if enemy.get("boss") and enemy.get("gimmick") == "reincarnate":
                rows.append([{"text": "⚔️ В бой", "callback_data": callback_data(user_id, "phoenixstart")}])
            elif enemy.get("boss") and enemy.get("gimmick") == "gatekeeper":
                rows.append([{"text": "⚔️ В бой", "callback_data": callback_data(user_id, "gatekeeperstart")}])
            else:
                rows.append([{"text": f"⚔️ {enemy['name']}", "callback_data": callback_data(user_id, "dungeonfight", str(enemy['index']))}])
    lines.extend(dungeon_haul_block(state))
    if state.get("can_rest"):
        # Healing is bought in the shop, and only there. Two buttons sitting on the floor
        # screen said the same two things the shelf already says, in a second vocabulary
        # -- and a shelf that is about to grow cannot have half of itself mirrored on the
        # screen in front of it.
        lines.append("\n<i>Лечение и припасы — в лавке.</i>")
        # Gear AND scrolls, side by side. A boss states the damage it is weak to, and the
        # answer to that line lives in one of these two screens -- a floor screen that
        # offers only the bag hides half of the reaction it is inviting.
        # Progress is the primary action after a clear: one button, one full Telegram
        # row, before the optional shop/equipment controls below it.
        rows.append([{
            "text": "⬇️ Спуститься",
            "callback_data": callback_data(user_id, "dungeondescend"),
        }])
        rows.append([
            {"text": "🧪 Лавка", "callback_data": callback_data(user_id, "dungeonshop")},
            {"text": "🎒 Снаряжение", "callback_data": callback_data(user_id, "bag")},
            {"text": "📜 Свитки", "callback_data": callback_data(user_id, "skills")},
        ])
        # On the deepest floor there is, the descent button becomes the finish line: it
        # still ends the run, but it says what it is doing rather than promising a floor
        # 46 that does not exist.
        # No finish line any more. Past the built bosses the roster repeats and the payout
        # stops growing, so the screen says what actually changes rather than pretending
        # the descent has ended.
        if int(state.get("floor", 1) or 1) >= int(state.get("reward_cap_floor", 0) or 0):
            lines.append(
                "\n♾ <b>Дальше боссы идут по кругу, а награда больше не растёт.</b>"
                " Спускайся ради глубины, а не ради денег."
            )
        # Leaving throws the run away, so it is never the wide button. Telegram sizes a
        # row's buttons equally, which means the only way to make it small is to keep it
        # sharing a row -- on the left, away from the one thumb reaches for.
        rows.append([{"text": "🚪 Выйти", "callback_data": callback_data(user_id, "dungeonquit")}])
    else:
        # Mid-floor the two reaction screens ride along with the exit rather than letting
        # it stretch across the whole width on its own.
        rows.append([
            {"text": "🚪", "callback_data": callback_data(user_id, "dungeonquit")},
            {"text": "🎒 Снаряжение", "callback_data": callback_data(user_id, "bag")},
            {"text": "📜 Свитки", "callback_data": callback_data(user_id, "skills")},
        ])
    return "\n".join(lines), {"inline_keyboard": rows}


# How many lines of the running commentary a fight screen keeps. The telegraph is what a
# player has to read; an unbounded transcript above it competes with the one line that
# matters and pushes the buttons off a phone screen.
PHOENIX_LOG_LINES = 4


def phoenix_view(entry: str, user_id, xp: int, state: dict | None = None) -> tuple[str, dict]:
    """The hand-fought Phoenix, drawn entirely from the state the game hands back.

    Nothing on this screen is decided here, the buttons above all: the engine says which
    moves are on offer this turn and its label is printed verbatim, because a client that
    assembled its own button set would start lying about the fight the first time a move
    changed. For the same reason nothing here reads the telegraph and comments on it --
    working out which answer the telegraph is asking for IS the boss.

    A state may be passed in because of the last frame only. Winning or losing clears the
    fight out of the run, so a redraw that always re-read the store would lose the scene
    the player just earned; the callback hands over the state it was answered with, and
    every other caller reads the live one.
    """
    if state is None:
        state = pets.phoenix_state(entry, user_id)
    if not isinstance(state, dict):
        return (
            "🔥 <b>Бой с фениксом не идёт.</b>\n\n"
            "Вернись на этаж — оттуда его можно начать заново.",
            {"inline_keyboard": [
                [{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}],
                _back_row(user_id),
            ]},
        )
    boss_hp = max(0, int(state.get("boss_hp", 0) or 0))
    boss_max = max(1, int(state.get("boss_max_hp", 0) or 1))
    hero_hp = max(0, int(state.get("hero_hp", 0) or 0))
    hero_max = max(1, int(state.get("hero_max_hp", 0) or 1))
    lines = [
        f"🔥 <b>{escape(str(state.get('boss_name') or 'Феникс'))}</b> · "
        f"фаза {2 if int(state.get('phase', 1) or 1) >= 2 else 1}",
        f"👹 {_money(boss_hp)} / {_money(boss_max)}",
        f"❤️ {_money(hero_hp)} / {_money(hero_max)}",
    ]
    # Both marks ride on one line above the prose: they are the state of the fight, and a
    # player checks them at a glance before reading anything else.
    marks = []
    burn = max(0, int(state.get("burn", 0) or 0))
    if burn:
        marks.append(f"🔥 Горение × {burn}")
    if state.get("vulnerable"):
        marks.append("💥 <b>УЯЗВИМ</b>")
    if marks:
        lines.append(" · ".join(marks))
    scene = str(state.get("scene") or "").strip()
    if scene:
        lines.extend(["", escape(scene)])
    # What the LAST answer cost, above the next telegraph rather than below it. The two
    # blocks are read in the order they happen: a player who has just lost 2,073 health
    # needs to know that before being asked to read the next move, not after -- and a
    # mistake is marked, because "герой теряет 2073" looks identical whether the block
    # was mistimed or the move was the one that punishes blocking.
    grade = str(state.get("grade") or "")
    history = [str(line).strip() for line in (state.get("log") or ()) if str(line).strip()]
    if history:
        # Telegram has no colour, so a mistake is marked and its opening line carries the
        # weight. Bolding every line of it would shout the arithmetic as loudly as the
        # sentence explaining what went wrong, which is the half that teaches.
        mark = "💢" if grade == "bad" else ("✅" if grade == "perfect" else "▫️")
        lines.append("")
        for index, line in enumerate(history[-PHOENIX_LOG_LINES:]):
            body = escape(line)
            if index == 0:
                body = f"<b>{body}</b>" if grade == "bad" else f"<i>{body}</i>"
                lines.append(f"{mark} {body}")
            else:
                lines.append(f"<i>{body}</i>")
    telegraph = str(state.get("telegraph") or "").strip()
    if telegraph:
        # The one block on this screen that has to be read rather than skimmed, so it
        # stands alone and in bold -- and deliberately with nothing under it, because any
        # line explaining it would answer the question the boss is asking.
        lines.extend(["", f"⚠️ <b>{escape(telegraph)}</b>"])
    rows = []
    record = pets.get_pet(entry, user_id) or {}
    if max(0, int((record.get("phoenix_record") or {}).get("wins", 0) or 0)) >= 10:
        rows.append([{"text": "⚡ Автобой", "callback_data": callback_data(user_id, "phoenixauto")}])
    if state.get("over"):
        lines.extend([
            "",
            "🏆 <b>Феникс повержен.</b>" if state.get("won")
            else "💀 <b>Феникс тебя одолел.</b>",
        ])
        # Reused rather than reformatted: a boss kill pays through the ordinary dungeon
        # payout, so it should read exactly like every other receipt in the run.
        receipt_text = dungeon_reward_text({"reward": state.get("reward")})
        if receipt_text:
            lines.append(receipt_text)
        rows.append([{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}])
        return "\n".join(lines), {"inline_keyboard": rows}
    pair = []
    for move in state.get("actions") or ():
        code = str((move or {}).get("code") or "")
        if not code:
            continue
        pair.append({
            "text": str(move.get("label") or code),
            "callback_data": callback_data(user_id, "phoenixact", code),
        })
        # Two to a row: the direction moves always arrive as a pair, and reading them side
        # by side is how a player picks between them.
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if not rows:
        # A live fight always offers something. If it somehow does not, the way out is a
        # button rather than a screen with no exit at all.
        rows.append([{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def gatekeeper_view(entry: str, user_id, xp: int,
                    state: dict | None = None) -> tuple[str, dict]:
    """The prediction, locks and step clock of the hand-fought Steel Gatekeeper."""
    if state is None:
        state = pets.gatekeeper_state(entry, user_id)
    if not isinstance(state, dict):
        return (
            "⚙️ <b>Бой со Стальным привратником не идёт.</b>\n\n"
            "Вернись на этаж — оттуда бой можно начать заново.",
            {"inline_keyboard": [[{
                "text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon"),
            }]]},
        )
    boss_hp = max(0, int(state.get("boss_hp", 0) or 0))
    boss_max = max(1, int(state.get("boss_max_hp", 1) or 1))
    hero_hp = max(0, int(state.get("hero_hp", 0) or 0))
    hero_max = max(1, int(state.get("hero_max_hp", 1) or 1))
    locks = " ".join("🔓" if opened else "🔒" for opened in state.get("locks", []))
    steps = " ".join("●" if filled else "○" for filled in state.get("steps", []))
    lines = [
        f"⚙️ <b>{escape(str(state.get('boss_name') or 'Стальной привратник'))}</b>",
        f"❤️ Герой: <b>{_money(hero_hp)} / {_money(hero_max)}</b>",
        f"👹 Привратник: <b>{_money(boss_hp)} / {_money(boss_max)}</b>",
        f"🔐 Замки: {locks}",
        f"👣 Шаги: {steps}",
    ]
    if state.get("is_emergency_mode"):
        lines.append("🚨 <b>АВАРИЙНЫЙ РЕЖИМ</b> · система больше не забывает")
    # The working, then the conclusion. Both are shown: the conclusion is only worth
    # trusting because the row above it is the evidence it was drawn from, and a counter
    # the player cannot see would make this a coin flip -- see the pets_gatekeeper docstring.
    observed = [str(row) for row in (state.get("observed_icons") or [])]
    if observed:
        lines.append("🔍 Замки помнят: <b>" + " ".join(escape(row) for row in observed) + "</b>")
    if state.get("prediction"):
        band = str(state.get("confidence_band") or "")
        share = round(max(0.0, min(1.0, float(state.get("confidence") or 0))) * 100)
        label = escape(str(state.get("prediction_label") or ""))
        if band == "committed":
            lines.append(f"🎯 <b>Ожидает: {label}</b> · уверенность {share}%")
        else:
            lines.append(f"👁 Присматривается к: <b>{label}</b> · уверенность {share}%")
    # The second thing it has ready, and never a hidden one. If two answers are dangerous
    # the player has to be able to see both before choosing.
    if state.get("covered"):
        lines.append(
            f"🛑 <b>{escape(str(state.get('covered_label') or ''))}</b> — перекрыто заранее"
        )
    hint = str(state.get("prediction_hint") if state.get("prediction")
               else state.get("adaptation_hint") or "")
    if hint:
        lines.append(f"<i>{escape(hint)}</i>")
    if state.get("trick_hint"):
        lines.append(f"<i>{escape(str(state['trick_hint']))}</i>")
    if state.get("step_hint"):
        lines.append(f"<i>{escape(str(state['step_hint']))}</i>")
    if state.get("shield_disrupted"):
        lines.append("💥 <b>Щит дестабилизирован:</b> следующий блок слабее.")
    scene = str(state.get("scene") or "").strip()
    if scene:
        lines.extend(["", escape(scene)])
    history = [str(row).strip() for row in state.get("log", []) if str(row).strip()]
    if history:
        lines.append("")
        lines.extend(f"<i>{escape(row)}</i>" for row in history[-6:])
    if state.get("is_core_open"):
        lines.extend(["", "🔓 <b>ЯДРО ОТКРЫТО.</b> Выбери полноценный удар."])
    elif state.get("telegraph"):
        lines.extend(["", f"⚠️ <b>{escape(str(state['telegraph']))}</b>"])

    rows = []
    if state.get("over"):
        lines.extend(["", "🏆 <b>Привратник повержен.</b>" if state.get("won")
                      else "💀 <b>Привратник завершил расчёт.</b>"])
        receipt_text = dungeon_reward_text({"reward": state.get("reward")})
        if receipt_text:
            lines.append(receipt_text)
        rows.append([{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}])
    else:
        pair = []
        for action in state.get("actions") or []:
            code = str(action.get("code") or "")
            if not code:
                continue
            pair.append({
                "text": str(action.get("label") or code),
                "callback_data": callback_data(user_id, "gatekeeperact", code),
            })
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        if not rows:
            rows.append([{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def character_element_view(entry: str, user_id) -> tuple[str, dict]:
    """One-time affinity choice with the complete strong/weak/neutral rule visible."""
    state = pets.character_element_status(entry, user_id)
    selected = state.get("selected")
    lines = [
        "✨ <b>Элемент персонажа</b>",
        f"Сильный элемент наносит <b>+{state['bonus_pct']}% урона</b> слабому. "
        "Против остальных элементов бонуса нет.",
        "<i>Выбор постоянный: изменить элемент потом нельзя.</i>",
        "",
    ]
    rows = []
    for row in state["choices"]:
        lines.append(
            f"{row['icon']} <b>{escape(row['name'])}</b> → сильнее {escape(row['strong_name'])}; "
            f"слабее {escape(row['weak_name'])}; остальные нейтральны."
        )
        if selected is None:
            rows.append([{
                "text": f"{row['icon']} Выбрать {row['name']}",
                "callback_data": callback_data(user_id, "elementset", row["code"]),
            }])
    if selected in C.CHARACTER_ELEMENTS:
        lines.append(
            f"\nВыбрано: {C.CHARACTER_ELEMENT_ICONS[selected]} "
            f"<b>{escape(C.CHARACTER_ELEMENT_NAMES[selected])}</b>."
        )
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def dungeon_shop_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The dungeon shop, rendered straight off the shelf the game hands over.

    Nothing about a row is decided here: its price, its currency, what is left of it and
    whether this runner can afford it all arrive already answered (pets.dungeon_shop), so
    the Mini App and this screen cannot disagree about what is on sale -- and a new line
    of stock is a row of data rather than a change to either client.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    state = pets.dungeon_status(entry, user_id)
    if not state.get("active"):
        return notice_view(user_id, "Лавка открыта только во время забега.")
    stock = state.get("shop") or []
    lines = [
        "🧪 <b>Лавка подземелья</b>",
        f"💎 Алмазы: {pets.ruby_balance(entry, user_id)}",
        f"❤️ Здоровье: {state.get('hp', 0)} / {state.get('max_hp', 0)}",
        "",
    ]
    rows = []
    if not state.get("can_rest"):
        lines.append("<i>Лавка открывается, когда этаж зачищен.</i>")
    for item in stock:
        # Already working is not a thing to price: a totem the run is carrying shown at
        # 💎 10 is an offer the buy handler would refuse anyway, and it tells the player
        # nothing about whether they are protected. Once it has BURNED the row goes back
        # to being an ordinary purchase -- that replacement is what the shelf is for.
        if item.get("held"):
            lines.append(f"{item['icon']} <b>{escape(str(item['name']))}</b> — <b>активен</b>")
            lines.append("<i>Сработает один раз, когда здоровье кончится.</i>")
            continue
        left = item.get("left")
        ration = "" if left is None else f" · осталось {left}"
        lines.append(
            f"{item['icon']} <b>{escape(str(item['name']))}</b> — "
            f"{'💎' if item['currency'] == 'ruby' else '🪙'} {item['price']}{ration}"
        )
        lines.append(f"<i>{escape(str(item['description']))}</i>")
        if not state.get("can_rest"):
            continue
        if item.get("sold_out"):
            rows.append([{"text": f"{item['icon']} {item['name']} — кончилось",
                          "callback_data": callback_data(user_id, "dungeonshop")}])
        else:
            price = "💎" if item["currency"] == "ruby" else "🪙"
            rows.append([{
                "text": f"{item['icon']} {item['name']} · {price} {item['price']}"
                        + ("" if item.get("affordable") else " — не хватает"),
                "callback_data": callback_data(user_id, "dungeonbuy", str(item["code"])),
            }])
    rows.append([{"text": "◀️ На этаж", "callback_data": callback_data(user_id, "dungeon")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def dungeon_chest_block(chest: dict | None, user_id) -> tuple[list[str], list[list[dict]]]:
    """The between-floors find, as lines and one row of buttons.

    Two states and no third: a closed box whose kind is still a secret, and a mimic that
    has already bitten. A plain chest never reaches a second state -- the press that opens
    it also empties it.
    """
    if not isinstance(chest, dict) or not chest.get("present"):
        return [], []
    if not chest.get("revealed"):
        return ([
            "🧰 <b>На площадке между этажами стоит сундук.</b>",
            f"<i>Крышка поддаётся. Такие иногда кусаются — до {int(chest.get('bite_percent', 15))}% здоровья.</i>",
            "",
        ], [[
            {"text": "🧰 Открыть", "callback_data": callback_data(user_id, "dungeonchest", "open")},
            {"text": "🚶 Мимо", "callback_data": callback_data(user_id, "dungeonchest", "leave")},
        ]])
    revealed = [
        f"🦷 <b>{escape(str(chest.get('name') or 'Мимик'))}</b> · ур. {int(chest.get('level', 1) or 1)}",
    ]
    # Before the flavour, not after it: whether to finish a mimic off is a decision about
    # these five numbers, and the line explaining what it looks like is not.
    if chest.get("stat_line"):
        revealed.append(f"<i>{escape(str(chest['stat_line']))}</i>")
    revealed.extend([
        f"<i>{escape(str(chest.get('hint') or ''))}</i>",
        "Он уже укусил. Дальше — твоё дело: добить или отойти.",
        "",
    ])
    return (revealed, [[
        {"text": "⚔️ Драться", "callback_data": callback_data(user_id, "dungeonchest", "fight")},
        {"text": "🚶 Уйти", "callback_data": callback_data(user_id, "dungeonchest", "leave")},
    ]])


# How many drop names a summary spells out before it starts counting instead.
HAUL_NAMES_SHOWN = 6


def haul_line(haul: dict | None) -> str:
    """One line of everything a tally holds, or "" when it holds nothing.

    Names the drops rather than counting them: "🎁 2" tells a player nothing they wanted
    to know, and the whole point of the summary is what they walked away with.
    """
    haul = haul or {}
    bits = []
    if int(haul.get("gold", 0) or 0):
        bits.append(f"🪙 {_money(int(haul['gold']))}")
    if int(haul.get("xp", 0) or 0):
        bits.append(f"✨ {_money(int(haul['xp']))}")
    if int(haul.get("rubies", 0) or 0):
        bits.append(f"💎 {int(haul['rubies'])}")
    for icon, key in (("🎁", "items"), ("📜", "scrolls"), ("🔮", "runes")):
        names = [str(name) for name in (haul.get(key) or []) if name]
        if not names:
            continue
        # A hundred-kill run collects more names than a Telegram message can hold, and a
        # wall of them is unreadable long before it is too long. The count is the part
        # that matters once there are more than a handful.
        if len(names) > HAUL_NAMES_SHOWN:
            shown = ", ".join(names[:HAUL_NAMES_SHOWN])
            bits.append(f"{icon} {escape(shown)} и ещё {len(names) - HAUL_NAMES_SHOWN}")
        else:
            bits.append(f"{icon} {escape(', '.join(names))}")
    return " · ".join(bits)


def dungeon_haul_block(state: dict) -> list[str]:
    """Nothing, deliberately.

    The floor screen used to carry a running «за этаж» and «за поход» tally, so the same
    ever-growing list of item names was reprinted on every redraw of every floor. The
    haul is the number you walk out with: it is reported once, when the run ends, by
    dungeon_finished_text.

    Kept as a function rather than deleted at the call site, so the floor view has one
    obvious place to grow a summary back if it is ever wanted again.
    """
    return []


def haul_praise(haul: dict | None) -> str:
    """Credit for what a descent brought back, sized to the haul.

    Dying deep with full pockets is the best thing that can happen in the dungeon and it
    used to be reported like a failure. The line is deliberately plain: it says the run
    paid, without pretending the ending was anything other than what it was.
    """
    haul = haul or {}
    gold = int(haul.get("gold", 0) or 0)
    drops = sum(len(haul.get(key) or []) for key in ("items", "scrolls", "runes"))
    if not gold and not drops and not int(haul.get("rubies", 0) or 0):
        return ""
    if gold >= 5000 or drops >= 5:
        return "Отличная нажива."
    return "Хорошая нажива."


def dungeon_finished_text(haul: dict | None) -> str:
    """The closing receipt, shown once the run is over however it ended.

    Always says something. An empty tally used to return "" and the whole screen said
    nothing at all about a run that had just ended, which reads as a lost receipt rather
    than as an empty one.
    """
    haul = haul or {}
    line = haul_line(haul)
    verdict = "🏁 Поход окончен" if haul.get("won") else "☠️ Поход оборвался"
    floor = int(haul.get("floor", 0) or 0)
    where = f" на этаже {floor}" if floor else ""
    kills = int(haul.get("kills", 0) or 0)
    lines = [f"{verdict}{where}. Побед: {kills}."]
    if line:
        lines.append(f"🎒 <b>Всего за поход:</b> {line}")
        praise = haul_praise(haul)
        if praise:
            lines.append(f"<i>{praise}</i>")
    else:
        lines.append("<i>Из подземелья ты вышел с пустыми руками.</i>")
    return "\n".join(lines)


def dungeon_reward_text(receipt: dict | None) -> str:
    """Compact dungeon reward receipt for the Telegram floor redraw."""
    reward = (receipt or {}).get("reward") or {}
    if not reward:
        return ""
    bits = []
    if reward.get("gold"):
        bits.append(f"🪙 +{_money(int(reward['gold']))}")
    if reward.get("xp"):
        bits.append(f"✨ +{_money(int(reward['xp']))} опыта")
    if receipt.get("rubies"):
        bits.append(f"💎 +{int(receipt['rubies'])}")
    lines = ["Получено: " + " · ".join(bits)] if bits else []
    # A chest empties several items at once, so this field is a LIST there and a single
    # drop after an ordinary kill. Both are walked the same way rather than the receipt
    # quietly printing only the first thing a box held.
    dropped = receipt.get("dropped") or {}
    for row in (dropped if isinstance(dropped, (list, tuple)) else [dropped]):
        if isinstance(row, dict) and row.get("name"):
            equipped = " (надето)" if row.get("auto_equipped") else ""
            lines.append(f"🎁 Предмет: «{escape(str(row['name']))}»{equipped}")
    scroll = receipt.get("scroll") or {}
    if scroll.get("granted"):
        lines.append(
            f"✨ Магия: {escape(str(scroll.get('icon') or '✨'))} "
            f"«{escape(str(scroll.get('name') or 'Новое заклинание'))}»"
        )
    rune = receipt.get("rune") or {}
    for row in (rune if isinstance(rune, (list, tuple)) else [rune]):
        if isinstance(row, dict) and row.get("granted"):
            # By its Russian name, not its internal code: "air +1" is the loot table
            # leaking onto a receipt a player is meant to read.
            element = str(row.get("element") or "")
            lines.append(
                f"🔮 Руна: {escape(pets.RUNE_NAMES.get(element, element or 'магия'))} "
                f"+{int(row['granted'])}"
            )
    return "\n".join(lines)


def casino_view(entry: str, user_id, xp: int = 0) -> tuple[str, dict]:
    """The coin-only casino lobby."""
    coins = pets.balance_for(entry, user_id, xp)
    active = casino.active_game(entry, user_id)
    if active:
        game = str(active.get("kind") or "")
        return (
            f"🎰 <b>Казино</b>\n\n🪙 Монеты: <b>{_money(coins)}</b>\n"
            "У тебя есть незавершённая игра — продолжи её.",
            {"inline_keyboard": [[
                {"text": f"▶️ Продолжить {_CASINO_GAME_NAMES.get(game, 'игру')}",
                 "callback_data": callback_data(user_id, "cpoker")},
            ], _back_row(user_id)]},
        )
    lines = [
        "🎰 <b>Казино</b>\n",
        f"🪙 Монеты: <b>{_money(coins)}</b>",
        "Победа возвращает удвоенную ставку; в напёрстках — x3. Играем только на монеты.",
        "Выбери игру: два режима покера, напёрстки или больше / меньше.",
    ]
    rows = [
        [
            {"text": "🃏 Покер · классика", "callback_data": callback_data(user_id, "cgame", "poker")},
        ],
        [
            {"text": "🧠 Покер · живой соперник", "callback_data": callback_data(user_id, "cgame", "poker_ai")},
        ],
        [
            {"text": "🥥 Напёрстки", "callback_data": callback_data(user_id, "cgame", "shell")},
            {"text": "↕️ Больше / меньше", "callback_data": callback_data(user_id, "cgame", "highlow")},
        ],
        _back_row(user_id),
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


_CASINO_GAME_NAMES = {
    "poker": "🃏 Покер · классика", "poker_ai": "🧠 Покер · живой соперник",
    "shell": "🥥 Напёрстки", "highlow": "↕️ Больше / меньше",
}


def casino_bet_view(entry: str, user_id, xp: int, game: str) -> tuple[str, dict]:
    if game not in casino.GAMES:
        return casino_view(entry, user_id, xp)
    coins = pets.balance_for(entry, user_id, xp)
    descriptions = {
        "poker": "Техасский холдем: дилер всегда поддерживает ставку. Рейз равен входной ставке.",
        "poker_ai": (
            "Соперник на каждую раздачу тайно выбирает стиль, оценивает только свои карты и стол, "
            "может чекнуть, коллировать, повышать или сбросить."
        ),
        "shell": "После ставки выбери один из трёх напёрстков.",
        "highlow": "Угадай: следующая карта будет выше или ниже открытой. Равная карта проигрывает.",
    }
    stakes = casino.POKER_BET_AMOUNTS if game in {"poker", "poker_ai"} else casino.BET_AMOUNTS
    rows = [[
        {"text": f"🪙 {stake}", "callback_data": callback_data(user_id, "cbet", f"{game}:{stake}")}
        for stake in stakes
    ]]
    if game in {"poker", "poker_ai"}:
        rows.append([{
            "text": "📚 Комбинации", "callback_data": callback_data(user_id, "ccombos", game),
        }])
    if game == "poker_ai":
        rows.append([{
            "text": "🎭 Стили соперника", "callback_data": callback_data(user_id, "cpokerstyles"),
        }])
    rows.append([{"text": "◀️ Игры", "callback_data": callback_data(user_id, "casino")}])
    return (
        f"{_CASINO_GAME_NAMES[game]}\n\n{descriptions[game]}\n"
        f"🪙 У тебя: <b>{_money(coins)}</b>\nВыбери ставку:",
        {"inline_keyboard": rows},
    )


def casino_shell_view(entry: str, user_id, xp: int, stake: int) -> tuple[str, dict]:
    coins = pets.balance_for(entry, user_id, xp)
    rows = [[
        {"text": f"🥥 {cup}", "callback_data": callback_data(user_id, "cshell", f"{stake}:{cup}")}
        for cup in (1, 2, 3)
    ], [{"text": "◀️ Ставки", "callback_data": callback_data(user_id, "cgame", "shell")}]]
    return (
        f"🥥 <b>Напёрстки</b>\n\nСтавка: {_money(stake)} · монет: {_money(coins)}\n"
        "Где шарик? Выбери напёрсток.", {"inline_keyboard": rows},
    )


def casino_highlow_view(
    entry: str, user_id, xp: int, stake: int, open_card: int | None = None
) -> tuple[str, dict]:
    coins = pets.balance_for(entry, user_id, xp)
    if open_card is None:
        open_card = casino.draw_highlow_open_card()
    rows = [[
        {"text": "⬇️ Меньше", "callback_data": callback_data(user_id, "chighlow", f"{stake}:{open_card}:low")},
        {"text": "⬆️ Больше", "callback_data": callback_data(user_id, "chighlow", f"{stake}:{open_card}:high")},
    ], [{"text": "◀️ Ставки", "callback_data": callback_data(user_id, "cgame", "highlow")}]]
    return (
        f"↕️ <b>Больше / меньше</b>\n\nСтавка: {_money(stake)} · монет: {_money(coins)}\n"
        f"Открыта карта: <b>{casino.highlow_card_text(open_card)}</b>. Что будет дальше?",
        {"inline_keyboard": rows},
    )


def casino_poker_view(
    entry: str, user_id, xp: int, state: dict | None = None, notice: str = ""
) -> tuple[str, dict]:
    state = state or casino.active_game(entry, user_id)
    if not state or state.get("kind") != "poker":
        return casino_view(entry, user_id, xp)
    hand = casino.poker_snapshot(state)
    stage = hand["stage"]
    opponent = hand["mode"] == "opponent"
    text = (
        f"{'🧠' if opponent else '🃏'} <b>Покер · на столе {stage} карт</b>\n\n"
        f"Общая ставка: <b>{_money(hand['pot'])}</b>\n"
        f"Стол: <b>{' · '.join(hand['board_cards'])}</b>\n"
        f"Твои карты: <b>{' · '.join(hand['player_cards'])}</b>\n\n"
    )
    if opponent and hand.get("last_action"):
        text += f"<i>{escape(hand['last_action'])}</i>\n\n"
    if notice:
        text += notice
    if hand["to_call"]:
        prompt = f"Соперник повысил. Поддержать ещё {hand['to_call']}?"
        primary = f"🃏 Колл +{hand['to_call']}"
    else:
        prompt = "Вскрыть карты?" if stage == 5 else "Открыть следующую карту?"
        primary = "🃏 Колл" if not opponent else "✋ Чек"
    raise_cost = hand["base_stake"] + hand["to_call"]
    raise_label = f"Рейз +{hand['base_stake']}"
    if hand["to_call"]:
        raise_label += f" · внести {raise_cost}"
    return text + prompt, {"inline_keyboard": [
        [{"text": primary, "callback_data": callback_data(user_id, "cpoker")}],
        [{
            "text": raise_label,
            "callback_data": callback_data(user_id, "cpoker", f"raise:{hand['base_stake']}"),
        }],
        [{"text": "🏳 Сбросить карты", "callback_data": callback_data(user_id, "cpoker", "fold")}],
    ]}


def casino_combinations_view(user_id, game: str = "poker") -> tuple[str, dict]:
    """Poker cheat sheet, strongest hand first, using the evaluator's own table."""
    lines = [
        "📚 <b>Комбинации в покере</b>",
        "От сильнейшей к слабейшей:\n",
    ]
    for number, row in enumerate(casino.POKER_COMBINATIONS, 1):
        lines.append(f"<b>{number}. {row['name']}</b> — {row['description']}")
    lines.extend([
        "",
        "Если комбинации одинаковые, сначала сравниваются карты самой комбинации, "
        "затем кикеры. Если совпали все пять карт — ничья.",
    ])
    game = "poker_ai" if game == "poker_ai" else "poker"
    return "\n".join(lines), {"inline_keyboard": [[{
        "text": "◀️ К покеру", "callback_data": callback_data(user_id, "cgame", game),
    }]]}


def casino_poker_styles_view(user_id) -> tuple[str, dict]:
    """Explain the four conventional opponent archetypes without revealing this hand."""
    lines = [
        "🎭 <b>Стили соперника</b>",
        "На каждую раздачу стиль выбирается заново и остаётся тайной до её конца.\n",
    ]
    for row in casino.POKER_STYLES:
        lines.append(f"<b>{escape(row['name'])}</b> — {escape(row['short'])}")
    lines.extend([
        "",
        "Тайтовый игрок чаще выбрасывает слабые руки, лузовый играет шире. "
        "Агрессивный чаще повышает и блефует, пассивный предпочитает чек и колл.",
    ])
    return "\n".join(lines), {"inline_keyboard": [[{
        "text": "◀️ К ставкам", "callback_data": callback_data(user_id, "cgame", "poker_ai"),
    }]]}


def casino_result_view(entry: str, user_id, xp: int, result: dict) -> tuple[str, dict]:
    if not result.get("ok"):
        stake = int(result.get("stake", 0) or 0)
        if result.get("error") == "active":
            active = result.get("active") or {}
            return (
                "🎰 <b>Казино</b>\n\nСначала закончи начатую игру.",
                {"inline_keyboard": [[
                    {"text": "▶️ Продолжить", "callback_data": callback_data(user_id, "cpoker")},
                ]]},
            )
        return (
            f"🎰 <b>Казино</b>\n\nНе хватает монет на ставку {_money(stake)}. "
            f"У тебя: {_money(int(result.get('balance', 0) or 0))}.",
            {"inline_keyboard": [[{"text": "◀️ К играм", "callback_data": callback_data(user_id, "casino")}]]},
        )
    game = str(result.get("game") or "")
    poker_mode = str(result.get("mode") or "classic")
    game_title = "poker_ai" if game == "poker" and poker_mode == "opponent" else game
    lines = [f"{_CASINO_GAME_NAMES.get(game_title, '🎰 Казино')}", ""]
    if game == "poker":
        style = result.get("opponent_style") or {}
        if style:
            lines.append(f"Стиль соперника: <b>{escape(style.get('name') or '')}</b>")
            lines.append(f"<i>{escape(style.get('short') or '')}</i>\n")
        if result.get("folded_by"):
            lines.extend([
                "Карты на столе:",
                f"<b>{' · '.join(result.get('board_cards') or [])}</b>\n",
                "Твои карты:",
                f"<b>{' · '.join(result.get('player_cards') or [])}</b>\n",
                f"<b>{result.get('comparison') or ''}</b>",
            ])
        else:
            player_combination = (result.get("player_combination") or {}).get("name") or "Не определена"
            dealer_combination = (result.get("dealer_combination") or {}).get("name") or "Не определена"
            opponent_word = "соперника" if poker_mode == "opponent" else "дилера"
            lines.extend([
                "Карты на столе:",
                f"<b>{' · '.join(result.get('board_cards') or [])}</b>\n",
                "Твои карты:",
                f"<b>{' · '.join(result.get('player_cards') or [])}</b>",
                f"Твоя комбинация: <b>{player_combination}</b>\n",
                f"Карты {opponent_word}:",
                f"<b>{' · '.join(result.get('dealer_cards') or [])}</b>",
                f"Комбинация {opponent_word}: <b>{dealer_combination}</b>\n",
                f"<b>{result.get('comparison') or ''}</b>",
            ])
    elif game == "shell":
        cups = ["🥥", "🥥", "🥥"]
        cups[int(result.get("ball", 0)) - 1] = "🟢"
        lines.append(" ".join(cups))
    elif game == "highlow":
        choice = "больше" if result.get("choice") == "high" else "меньше"
        lines.append(
            f"Было: <b>{casino.highlow_card_text(result.get('open_card'))}</b>. "
            f"Ты выбрал «{choice}». Выпало: <b>{casino.highlow_card_text(result.get('card'))}</b>."
        )
    if result.get("won"):
        victory = "Получено" if game == "poker" else "Победа! Получено"
        lines.append(f"\n🎉 {victory} {_money(int(result['payout']))}.")
    elif result.get("draw"):
        lines.append(f"\n🤝 Ничья — ставка {_money(int(result['payout']))} возвращена.")
    elif result.get("folded_by") == "player":
        lines.append(f"\n🏳 Сброшено: {_money(int(result['stake']))} осталось в банке.")
    else:
        lines.append(f"\n💨 Не повезло: ставка {_money(int(result['stake']))} проиграна.")
    lines.append(f"🪙 Осталось: <b>{_money(int(result['balance']))}</b>")
    again_game = "poker_ai" if game == "poker" and poker_mode == "opponent" else game
    rows = [[{"text": "🔁 Ещё раз", "callback_data": callback_data(user_id, "cgame", again_game)}]]
    if game == "poker":
        rows[0].append({
            "text": "📚 Комбинации", "callback_data": callback_data(user_id, "ccombos", again_game),
        })
    rows.append([{"text": "◀️ Игры", "callback_data": callback_data(user_id, "casino")}])
    return "\n".join(lines), {"inline_keyboard": rows}


# Telegram keyboards get unusable past a handful of full-width rows, and the shelf is 35
# quests long. The rest are one tap away in the Mini App, which is where browsing belongs.
REAL_QUEST_BUTTONS = 8

QUEST_DIFFICULTY_NAMES = {1: "новичок", 2: "просто", 3: "средне", 4: "сложно", 5: "жёстко"}
QUEST_TOOL_NAMES = {"brush": "кисть", "airbrush": "аэрограф", "any": "кисть или аэрограф"}


def quest_pips(level: int) -> str:
    level = min(max(0, int(level or 0)), 5)
    return "●" * level + "○" * (5 - level)


def _legacy_quests_view(entry: str, user_id, kind: str = "paint") -> tuple[str, dict]:
    """One quest slot: what it is, what it pays, and the hashtag that submits it.

    Both kinds render through here because both are dealt the same way -- the only
    differences are the heading, the verb ("что красим" vs "что делаем") and whether the
    line under the title names a tool or a badge. The hashtag is last on purpose: it is
    the only part a player has to copy, and it is what turns a finished thing into a
    submission a moderator can see.
    """
    paint = kind != "real"
    board = quests.daily_quest(entry, user_id) if paint else quests.real_quest(entry, user_id)
    quest = board.get("quest")
    other = "real" if paint else "paint"
    other_button = {
        "text": "🌍 Квест в реале" if paint else "🎯 Челлендж дня",
        "callback_data": callback_data(user_id, "quests", other),
    }
    lines = ["🎯 <b>Челлендж дня — покрас</b>\n" if paint else "🌍 <b>Квест в реале</b>\n"]
    if not quest:
        lines.append(
            "Все квесты в реале пройдены — новые откроются, когда отдохнут старые."
            if board.get("status") == "exhausted"
            else "На сегодня всё — сдано. Новый придёт завтра."
        )
        return "\n".join(lines), {"inline_keyboard": [[other_button], _back_row(user_id)]}

    reward = quest.get("reward") or {}
    difficulty = int(quest.get("difficulty", 1) or 1)
    lines.append(f"<b>{escape(quest['title'])}</b>")
    detail = (
        QUEST_TOOL_NAMES.get(quest.get("tool"), quest.get("tool", "")) if paint
        else (f"значок «{escape(quest['badge'])}»" if quest.get("badge") else "без значка")
    )
    lines.append(
        f"{quest_pips(difficulty)} {QUEST_DIFFICULTY_NAMES.get(difficulty, '')} · {detail}"
    )
    lines.append(
        f"\n<b>{'Что красим' if paint else 'Что делаем'}:</b> {escape(quest['subject'])}"
    )
    lines.append(escape(quest["technique"]))
    lines.append(f"\n💡 {escape(quest['hint'])}")
    lines.append(
        f"\n<b>Награда:</b> 🪙 {_money(int(reward.get('gold', 0)))} · "
        f"✨ {int(reward.get('xp', 0))} опыта · 🎟 {int(reward.get('tickets', 0))} билет"
        f" · 🎁 шанс находки {round(float(reward.get('drop_chance', 0)) * 100)}%"
    )
    if reward.get("scroll_chance"):
        lines.append(
            f"📜 Новый свиток: {round(float(reward['scroll_chance']) * 100)}%, "
            f"если не выпал раньше — гарантирован на "
            f"{int(reward.get('scroll_pity', 0))}-м принятом квесте сложности 4–5."
        )
    if not board.get("has_pet", True):
        # Said here rather than discovered at payout: two of the four reward legs need a
        # creature to land in (see quests._pay), and somebody deciding whether to spend an
        # evening on this is owed that before they start, not after.
        lines.append(
            "\n⚠️ Опыт и предмет снаряжения начислить некуда — сначала приручи существо. "
            "Монеты, билет и найденный свиток сохранятся в любом случае."
        )
    if board.get("status") == "review":
        lines.append("\n⏳ Работа на проверке у модератора.")
    else:
        if not paint:
            lines.append(f"\n<b>Нужно показать:</b> {escape(quest.get('proof') or '')}")
        lines.append(
            f"\nВыложи фото в чат с хештегом <code>{escape(quest['hashtag'])}</code> — "
            "модератор посмотрит и начислит награду."
        )

    rows = []
    rerolls = int(board.get("rerolls_left", 0) or 0)
    if rerolls and board.get("status") != "review":
        # The warning goes in the TEXT, not on the button: a callback label is one short
        # line and this is a trade-off, not a name. Rerolling costs difficulty, so it has
        # to be readable before the tap rather than explained by the result.
        top = difficulty >= max(catalog_difficulties())
        lines.append(
            "\n⚠️ Реролл даёт квест на ступень сложнее"
            + (" — но выше пятой ступени некуда, придёт другой такой же."
               if top else " — и награда тоже вырастет.")
        )
        rows.append([{
            "text": f"🎲 Реролл, сложнее ({rerolls})",
            "callback_data": callback_data(user_id, "questreroll", kind),
        }])
    rows.append([other_button])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def _quest_timer(seconds: int) -> str:
    minutes = max(0, (int(seconds or 0) + 59) // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


QUEST_KINDS = ("paint", "real", "rune", "gear")
QUEST_TAB_LABELS = {
    "paint": "🎯 Три квеста на покрас",
    "real": "🌍 Квест в реале",
    "rune": "🕳 Магия подземелья",
    "gear": "⚔️ Покрасы для арены",
}


def quest_board_for(entry: str, user_id, kind: str):
    """The board one tab is dealt from. Single source of truth for the mapping."""
    if kind == "real":
        return quests.real_quest(entry, user_id)
    if kind == "rune":
        return quests.rune_quest(entry, user_id)
    if kind == "gear":
        return quests.gear_quest(entry, user_id)
    return quests.daily_quest(entry, user_id)


def quests_view(entry: str, user_id, kind: str = "paint") -> tuple[str, dict]:
    """Compact quest shelf: three readable cards and three matching buttons."""
    kind = kind if kind in QUEST_KINDS else "paint"
    board = quest_board_for(entry, user_id, kind)
    cards = board.get("quests") or []
    paint = kind != "real"
    title = ("🕳 <b>Магия подземелья · элементы и инструменты</b>" if kind == "rune"
             else "⚔️ <b>Покрасы для арены · 5 карточек</b>" if kind == "gear"
             else "🎯 <b>Квесты на покрас · 3 карточки</b>" if paint
             else "🌍 <b>Квест в реале</b>")
    lines = [title]
    if board.get("auto_refresh"):
        lines.append(
            f"\n⏳ Новая подборка через "
            f"<b>{_quest_timer(board.get('seconds_until_refresh', 0))}</b>."
        )
    else:
        lines.append("\n🕰 Без дедлайна: квесты не обновятся сами.")
    if kind == "rune":
        lines.append(
            "Элементальный квест даёт случайную руну и магию. Квесты кирки, лопаты "
            "и фигурок сразу улучшают инструмент навсегда."
        )
    elif kind == "gear":
        lines.append(
            "Каждый квест здесь — персональный покрас на вещь, с которой ты выходишь "
            "в бой: фотография твоей работы на предмете и +30% к его полезным "
            "характеристикам. Это единственные квесты, которые меняют исход боя."
        )
    buttons = []
    for index, card in enumerate(cards, 1):
        status = card.get("status", "open")
        marker = "❗" if status == "open" else ("⏳" if status == "review" else "✅")
        subject = " ".join(str(card.get("subject") or "").split())
        if len(subject) > 105:
            subject = subject[:104].rstrip() + "…"
        lines.extend([
            f"\n<b>{index}. {marker} {escape(card.get('title') or 'Квест')}</b>",
            f"{quest_pips(card.get('difficulty', 1))} · {escape(subject)}",
        ])
        benefit = _quest_benefit_text(card)
        if benefit:
            lines.append(f"🎁 <b>{escape(benefit)}</b>")
        buttons.append([{
            "text": f"{index}. {marker} {str(card.get('title') or 'Квест')[:45]}",
            "callback_data": callback_data(user_id, "questdetail", f"{kind}:{card.get('code')}"),
        }])
    if not cards:
        lines.append("\nПока доступных заданий нет.")
    if board.get("reroll_available"):
        lines.append("\n🎲 Можно обновить всю эту группу сейчас.")
        reroll_label = "🎲 Реролл группы"
    elif board.get("reroll_at_label"):
        lines.append(
            f"\nСледующий реролл в <b>{escape(board['reroll_at_label'])}</b> по Москве."
        )
        reroll_label = f"⏳ Реролл в {board['reroll_at_label']}"
    else:
        lines.append("\nРеролл будет доступен после проверки отправленных работ.")
        reroll_label = "⏳ Работы на проверке"
    buttons.append([{
        "text": reroll_label,
        "callback_data": callback_data(user_id, "questreroll", kind),
    }])
    for other in QUEST_KINDS:
        if other != kind:
            buttons.append([{
                "text": QUEST_TAB_LABELS[other],
                "callback_data": callback_data(user_id, "quests", other),
            }])
    buttons.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": buttons}


def quest_detail_view(entry: str, user_id, kind: str, code: str) -> tuple[str, dict]:
    """Full brief and a practical step-by-step tutorial for one selected card."""
    kind = kind if kind in QUEST_KINDS else "paint"
    board = quest_board_for(entry, user_id, kind)
    card = next((row for row in board.get("quests", []) if row.get("code") == code), None)
    if card is None:
        return quests_view(entry, user_id, kind)
    paint = kind != "real"
    reward = card.get("reward") or {}
    difficulty = int(card.get("difficulty", 1) or 1)
    status = card.get("status", "open")
    scroll_reward = (
        f"\n📜 Новый свиток: {round(float(reward['scroll_chance']) * 100)}%, "
        f"если не выпал раньше — гарантирован на "
        f"{int(reward.get('scroll_pity', 0))}-м принятом квесте сложности 4–5."
        if reward.get("scroll_chance") else ""
    )
    benefit = _quest_benefit_text(card)
    specialist_paint = str(card.get("code") or "").startswith("rune_paint_")
    if specialist_paint:
        how_lines = [
            "1. Возьми новую, ещё не опубликованную работу и выполни три шага из блока «Техника».",
            f"2. Сделай чёткое фото ({escape(card.get('proof') or 'готового результата')}) и "
            f"выложи его в чат с хештегом <code>{escape(card.get('hashtag') or '')}</code>.",
        ]
    else:
        how_lines = [
            "1. Используй новую, ещё не опубликованную работу и подготовь нужную деталь.",
            f"2. {'Повтори технику из описания и сверь результат с подсказкой.' if paint else 'Выполни действие полностью, затем сверь результат с подсказкой.'}",
            f"3. Сделай чёткое фото ({escape(card.get('proof') or 'готового результата')}) и "
            f"выложи его в чат с хештегом <code>{escape(card.get('hashtag') or '')}</code>.",
        ]
    lines = [
        f"{'🎯' if paint else '🌍'} <b>{escape(card.get('title') or 'Квест')}</b>",
        f"{quest_pips(difficulty)} {QUEST_DIFFICULTY_NAMES.get(difficulty, '')}",
        f"\n<b>{'Что красим' if paint else 'Что делаем'}:</b> {escape(card.get('subject') or '')}",
        (f"\n🎁 <b>Что получишь:</b> {escape(benefit)}" if benefit else ""),
        f"\n<b>Техника:</b> {escape(card.get('technique') or '')}",
        f"\n💡 <b>Подсказка:</b> {escape(card.get('hint') or '')}",
        f"\n<b>{'Как сдать' if specialist_paint else 'Как выполнить'}:</b>",
        *how_lines,
        f"\n<b>Награда:</b> 🪙 {_money(int(reward.get('gold', 0)))} · ✨ {int(reward.get('xp', 0))} опыта · "
        f"🎟 {int(reward.get('tickets', 0))} · 🎁 {round(float(reward.get('drop_chance', 0)) * 100)}%",
        scroll_reward,
        (f"\n⏳ До обновления: <b>{_quest_timer(board.get('seconds_until_refresh', 0))}</b>"
         if board.get("auto_refresh") else "\n🕰 Дедлайна нет — квест останется здесь."),
    ]
    if status == "review":
        lines.append("\n⏳ Работа уже на проверке у модератора.")
    elif status == "done":
        lines.append("\n✅ Квест принят и завершён.")
    rows = []
    rows.append([{
        "text": "◀️ К карточкам",
        "callback_data": callback_data(user_id, "quests", kind),
    }])
    return "\n".join(lines), {"inline_keyboard": rows}


def quest_review_view(entry: str, user_id) -> tuple[str, dict]:
    """The oldest submitted quest, ready to decide without opening the Mini App."""
    rows = quests.pending(entry)
    lines = [f"🛡 <b>Проверка квестов · {len(rows)}</b>\n"]
    if not rows:
        lines.append("Все заявки разобраны.")
        return "\n".join(lines), {"inline_keyboard": _back_row(user_id)}
    row = rows[0]
    author = row.get("author_name") or row.get("author_username") or row.get("user_id")
    lines.extend([
        f"<b>{escape(str(author))}</b>",
        f"🎯 {escape(str(row.get('title') or row.get('code') or 'Квест'))}",
        escape(str(row.get("subject") or "")),
        escape(str(row.get("technique") or "")),
        f"💡 {escape(str(row.get('hint') or ''))}" if row.get("hint") else "",
        f"<b>Показать:</b> {escape(str(row.get('proof') or ''))}" if row.get("proof") else "",
        "\nОткрой работу в чате и выбери решение.",
    ])
    keyboard = [[
        {"text": "✅ Принять", "callback_data": callback_data(user_id, "questaccept", row["id"])},
        {"text": "❌ Отклонить", "callback_data": callback_data(user_id, "questreject", row["id"])},
    ]]
    link = stats.figurine_message_link(None, row.get("chat_id"), row.get("message_id"))
    if link:
        keyboard.append([{"text": "📷 Открыть работу в чате", "url": link}])
    keyboard.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": keyboard}


def quest_submission_summary(submission: dict) -> str:
    """Which work, by whom -- the two lines an alert keeps once its buttons are gone."""
    title = str(submission.get("title") or submission.get("code") or "Квест")
    author = str(submission.get("author_name") or submission.get("author_username") or "Игрок")
    return f"<b>{escape(title)}</b>\nАвтор: {escape(author)}"


def quest_submission_notification_view(
    moderator_id, submission: dict, webapp_url: str | None = None,
) -> tuple[str, dict]:
    """A moderator's private alert for one newly submitted quest.

    The review queue itself remains the source of truth: these controls only open its
    two existing review surfaces.  Keeping the moderator id in the Telegram callback
    preserves the same forwarded-message protection as the rest of the pet menu.
    """
    lines = [
        "🎯 Новая заявка на проверку квеста.",
        quest_submission_summary(submission),
    ]
    keyboard = []
    if webapp_url:
        separator = "&" if "?" in webapp_url else "?"
        keyboard.append([{
            "text": "🖥 Проверить в вебе",
            "web_app": {"url": f"{webapp_url}{separator}view=review"},
        }])
    keyboard.append([{
        "text": "📲 Проверить в Telegram",
        "callback_data": callback_data(moderator_id, "questreview"),
    }])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def quest_mods_view(entry: str, user_id, can_appoint: bool) -> tuple[str, dict]:
    """Who may review quests here, and -- for a full admin -- how to change that.

    `can_appoint` is threaded in rather than worked out here because the answer needs a
    Telegram round trip (is this person a chat administrator?) and this module is pure.
    It draws the LINE this screen exists to hold: a delegated moderator can review, and
    can see who else can, but cannot appoint further moderators. Only a chat admin or a
    hardcoded delegate can widen the list -- otherwise one appointment quietly becomes
    the power to hand out the same appointment forever.
    """
    listed = quests.moderators(entry)
    lines = ["🛡 <b>Модераторы квестов</b>\n"]
    lines.append(
        "Могут принимать и отклонять работы по квестам здесь и в мини-приложении. "
        "Больше ничего: ни значков, ни настроек чата."
    )
    if listed:
        lines.append("\n<b>Назначенные:</b>")
        for row in listed:
            handle = f" (@{escape(row['username'])})" if row.get("username") else ""
            lines.append(f"• {escape(row.get('display_name') or row['user_id'])}{handle}")
    else:
        lines.append("\nПока никого не назначили.")
    lines.append(
        "\n<i>Администраторы чата и так могут проверять квесты — их сюда добавлять "
        "не нужно.</i>"
    )

    pending = quests.pending_count(entry)
    rows = [[{
        "text": ("🔴 " if pending else "") + f"🛡 Проверить заявки · {pending}",
        "callback_data": callback_data(user_id, "questreview"),
    }]]
    if can_appoint:
        rows.append([{
            "text": "➕ Добавить модератора",
            "callback_data": callback_data(user_id, "questmodadd"),
        }])
        for row in listed[:8]:
            name = row.get("display_name") or row["user_id"]
            rows.append([{
                "text": f"➖ Убрать: {name}"[:60],
                "callback_data": callback_data(user_id, "questmoddel", str(row["user_id"])),
            }])
    else:
        lines.append("\nДобавлять и убирать может только администратор чата.")
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def catalog_difficulties():
    """The difficulty ladder, so the reroll warning knows where the top rung is."""
    return quests.DIFFICULTIES


def daily_bonus_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The one screen on this menu that owes nothing to having a pet: chat activity is
    most members' only income, so this reads/writes the shared coin ledger in economy.py
    directly rather than going through pets.py's balance_for/spend wrappers, which all
    assume a cage exists.

    economy.daily_bonus_status() already carries both "what today is worth" and "what
    tomorrow is worth" in the same call regardless of whether today has been claimed yet
    (see its docstring) -- that symmetry is why this view needs no branching to decide
    WHICH number to show where, only whether the claim button should be there at all.
    """
    status = economy.daily_bonus_status(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)

    lines = ["🎁 <b>Ежедневный бонус</b>\n", "Мастерская платит просто за то, что ты заглянул."]
    if status["can_claim"]:
        lines.append(f"\nСегодня ещё не забрано: <b>{_coins(status['amount'])}</b>.")
        if status["streak"]:
            lines.append(
                f"Серия — {_plural(status['streak'], 'день', 'дня', 'дней')} подряд:"
                f" заберёшь сегодня, станет {status['next_streak']}."
            )
    else:
        lines.append(
            f"\nСегодня уже забрано: <b>{_coins(status['amount'])}</b>."
            f" Серия — {_plural(status['next_streak'], 'день', 'дня', 'дней')}."
        )
        lines.append("Пропустишь день — серия смоется, как непросохший грунт под дождём.")
    lines.append(f"Завтра: {_coins(status['tomorrow'])}.")

    lines.append("\n<b>Серия по дням</b>")
    # The table exists to make the streak feel worth protecting, not just to list numbers
    # -- so it always marks where THIS visit sits, even past day 7 where the payout is
    # already flat (min() below pins the marker to the last row instead of falling off it).
    marked_day = min(status["next_streak"], len(economy.DAILY_BONUS_BY_STREAK))
    for day, amount in enumerate(economy.DAILY_BONUS_BY_STREAK, start=1):
        marker = " ← сегодня" if day == marked_day else ""
        lines.append(f"{day} д. — {_money(amount)}{marker}")
    lines.append("<i>Дальше — потолок серии, дальше он не растёт.</i>")
    lines.append(f"\n🪙 Монеты: {_money(coins)}")

    rows = []
    if status["can_claim"]:
        rows.append([{
            "text": f"🎁 Забрать {_money(status['amount'])}",
            "callback_data": callback_data(user_id, "dailybonusclaim"),
        }])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


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
    keyboard = []
    # The reward for THIS note, above the pager: it belongs to the entry on screen, and a
    # player who pages away must not be able to press a button meant for another one.
    if update.reward_rubies > 0:
        if update.id in pets_updates.claimed_ids(entry, user_id):
            lines.insert(-1, f"\n🎁 Награда получена: {update.reward_rubies} 💎")
        else:
            lines.insert(-1, f"\n🎁 За эту новость — {update.reward_rubies} 💎")
            keyboard.append([{
                "text": f"🎁 Забрать награду · {update.reward_rubies} 💎",
                # The id rather than the page number: pages shift when a note ships, and a
                # stale button must never pay out the wrong entry's reward.
                "callback_data": callback_data(user_id, "newsclaim", update.id),
            }])
    navigation = []
    if page + 1 < total:
        navigation.append({
            "text": "◀️", "callback_data": callback_data(user_id, "updates", str(page + 1)),
        })
    if page > 0:
        navigation.append({
            "text": "▶️", "callback_data": callback_data(user_id, "updates", str(page - 1)),
        })
    if navigation:
        keyboard.append(navigation)
    keyboard.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": keyboard}


# ----------------------------------------------------------------------------- cage


def cage_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The free cage is the game's convenience track: each upgrade adds a fight and a cut
    of the winnings -- things a player feels every day without them changing who wins a
    fight."""
    level = pets.cage_level(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)

    lines = ["🏠 <b>Клетка</b>\n"]
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
    if level < C.CAGE_MAX_LEVEL:
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


def _drop_pct(chance) -> str:
    """A find chance a human can hold in their head.

    The raw number carries six decimals off the luck multiplier; «7.76203%» is noise
    dressed as precision. Rounded to whole percent, with <1% kept as «<1%» rather than
    collapsing to a flat 0 that would read as impossible.
    """
    try:
        value = float(chance or 0.0) * 100
    except (TypeError, ValueError):
        return "0%"
    if 0 < value < 1:
        return "<1%"
    return f"{round(value)}%"


def _busy_elsewhere_line(where: str) -> str:
    """One creature, one place -- said the same way from both sides of the screen."""
    return (
        f"🔒 Существо {where}. В двух местах сразу — только когда покрашены "
        "обе фигурки: фермера и шахтёра."
    )


def _feature_summary(status: dict) -> str:
    """Four plot upgrades on ONE line: bought ones are a row of icons, the rest are named.

    They used to take four lines listing a percentage each, permanently, long after every
    one of them was bought and there was nothing left to decide.
    """
    owned, missing = [], []
    for feature, label in FARM_FEATURE_LABELS.items():
        data = _farm_feature_status(status, feature)
        if int(data.get("level", 0) or 0) >= int(data.get("max_level", 1) or 1):
            owned.append(label.split(" ", 1)[0])
        else:
            missing.append(label)
    if not missing:
        return "Апгрейды: " + " ".join(owned) + " — все куплены"
    if not owned:
        return "Апгрейды не куплены: " + ", ".join(missing)
    return "Апгрейды: " + " ".join(owned) + " · не хватает " + ", ".join(missing)


# Every owned thing in «Хозяйство» is written as a pair of lines: what it IS, then an
# italic line for what to do about it. Squeezing both onto one line is what turned this
# block into a wall -- a sentence ending in «+50%.» followed by another sentence starting
# with «🎨 Покрась» reads as one run-on string at Telegram's line length.
def _shovel_lines(status: dict) -> list[str]:
    if status.get("shovel_upgraded"):
        return ["🪏 Лопата — руническая, ∞ зарядов, +50% золота за смену"]
    runs = int(status.get("shovel_runs", 0) or 0)
    return [
        f"🪏 Лопата — зарядов {runs}, +25% золота за смену",
        "<i>🎨 покрась в NMM в «Квестах»: станет бесконечной, +50%</i>",
    ]


def _pickaxe_lines(quarry: dict) -> list[str]:
    if quarry.get("pickaxe_upgraded"):
        return ["⛏ Кирка — руническая, ∞ зарядов, +50% ко всей добыче"]
    runs = int(quarry.get("pickaxe_runs", 0) or 0)
    return [
        f"⛏ Кирка — зарядов {runs}",
        "<i>🎨 покрась в NMM в «Квестах»: станет бесконечной, +50% ко всей добыче</i>",
    ]


FIGURINE_LABELS = {"farmer": "🧑‍🌾 Фигурка фермера", "miner": "⛏️ Фигурка шахтёра"}


def _figurine_lines(status: dict) -> list[str]:
    """The pair, and what the pair is FOR -- the point is only made by owning both."""
    painted = status.get("figurines") or {}
    if status.get("parallel_work"):
        return ["🧑‍🌾⛏️ Обе фигурки покрашены — ферма и карьер работают одновременно"]
    marks = " · ".join(
        f"{label} — {'есть' if painted.get(key) else 'нет'}"
        for key, label in FIGURINE_LABELS.items()
    )
    return [
        marks,
        "<i>🎨 покрась обе в «Квестах»: ферма и карьер пойдут одновременно.</i>",
        "<i>Каждая сама по себе даёт +25% опыта со своей работы.</i>",
    ]


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
    quarry = pets.quarry_status(entry, user_id)
    meadow_tickets = pets.meadow_tickets(entry, user_id)
    passive_before = pets.passive_income_status(entry, user_id)
    coins = pets.balance_for(entry, user_id, xp)
    level = int(status.get("level", status.get("farm_level", 0)) or 0)
    max_level = int(status.get("max_level", 10) or 10)
    active = bool(status.get("running"))

    # The screen is built as four fixed blocks -- work, plot, wallet, timers -- in that
    # order, because that is the order the questions come in: what can I send it to do,
    # what can I buy, what do I have, and only then how long is left. Timers last is the
    # rule the whole layout hangs on: they are the one part that changes by itself, so
    # putting them at the top pushed everything a player actually presses off the screen.
    lines = [f"🌾 <b>Ферма</b> · уровень {level} из {max_level}"]
    lines.append(f"🐾 Работник: <b>{_name(pet)}</b>")

    lines.append("\n<b>🌾 Смена</b>")
    if active:
        planned = int(status.get("planned_hours") or 0)
        worked = int(status.get("worked_hours") or 0)
        reward = status.get("reward") or {}
        lines.append(f"⏳ Идёт {planned} ч · отработано {worked} ч")
        if reward:
            lines.append(
                f"Принесёт 🪙 {_money(int(reward.get('gold', 0) or 0))} · "
                f"✨ {int(reward.get('xp', 0) or 0)}"
            )
        # No more attack immunity: a farming pet cannot pick a fight itself, but it is an
        # ordinary target for everyone else's.
        lines.append("<i>В бой сам не пойдёт, но напасть на него можно.</i>")
        lines.append("<i>Забрать раньше — заплатят только за целые часы.</i>")
        if status.get("can_ticket"):
            # The distinction that matters, and the one a player will not assume: unlike
            # «Забрать сейчас», a ticket costs nothing off the payout.
            lines.append(
                f"<i>🎟 Билет закончит смену сразу, а заплатят как за все {planned} ч.</i>"
            )
    elif status.get("ready"):
        lines.append("✅ Закончилась. Награда уже едет в личные сообщения.")
    elif level <= 0:
        lines.append("Сначала построй ферму — первый уровень откроет смены от 1 до 8 часов.")
    elif status.get("blocked_by_quarry"):
        lines.append(_busy_elsewhere_line("в карьере"))
    else:
        lines.append("<i>чем длиннее, тем больше монет, опыта и шанс находки</i>")
        for row in status.get("hour_previews", []):
            if int(row.get("hours", 0) or 0) not in C.FARM_QUICK_HOUR_CHOICES:
                continue
            lines.append(
                f"{row['hours']} ч — 🪙 {_money(int(row['gold']))} · "
                f"✨ {int(row['xp'])} · 🎁 {_drop_pct(row.get('drop_chance'))}"
            )

    lines.append("\n<b>⛏ Карьер</b>")
    charges = "∞" if quarry.get("pickaxe_unlimited") else str(int(quarry.get("pickaxe_runs", 0) or 0))
    if quarry.get("running"):
        lines.append(f"⏳ Добыча идёт · зарядов кирки: {charges}")
        lines.append("<i>Забрать раньше — заплатят по ближайшей меньшей смене.</i>")
    elif quarry.get("blocked_by_farm"):
        lines.append(_busy_elsewhere_line("на ферме"))
    else:
        lines.append(f"<i>один заряд — одна смена · зарядов: {charges}</i>")
        for preview in quarry.get("hour_previews", []):
            lines.append(
                f"{int(preview['hours'])} ч — 💎 {int(preview['ruby_min'])}–{int(preview['ruby_max'])} · "
                f"🪙 {_money(int(preview['gold']))} · ✨ {int(preview['xp'])} · "
                f"🎁 {_drop_pct(preview.get('drop_chance'))}"
            )

    lines.append("\n<b>🌼 Поляна</b>")
    lines.append(f"🎫 Билетов на поляну: {meadow_tickets} — копай клетки, ищи алмазы.")

    next_cost = status.get("next_level_cost")
    lines.append("\n<b>🏡 Хозяйство</b>")
    if level < max_level:
        bonus = status.get("next_level_bonus")
        # Price on the headline, payout on its own italic line underneath. Printed as one
        # sentence these were two bare "N монет" halves in a row and read as a single
        # contradictory number.
        lines.append(
            f"Уровень {level} → {level + 1}"
            + (f" — {_coins(int(next_cost))}" if next_cost is not None else "")
        )
        if bonus:
            lines.append(f"<i>даст {escape(str(bonus))}</i>")
    else:
        lines.append("🏆 Прокачано полностью.")
    # A blank line between each owned thing. Four kinds of kit stacked without one is the
    # «каша» this block was: every line starts with an emoji and ends in a percentage, so
    # nothing tells the eye where one item stops and the next begins.
    lines.append("")
    lines.append(_feature_summary(status))
    lines.append("")
    lines.extend(_shovel_lines(status))
    lines.append("")
    lines.extend(_pickaxe_lines(quarry))
    lines.append("")
    lines.extend(_figurine_lines(status))

    lines.append("\n<b>💰 Кошелёк</b>")
    lines.append(f"🪙 {_money(coins)} · 🎟 билетов: {int(status.get('tickets', 0) or 0)}")

    # Every countdown on the screen, collected in one block at the very bottom.
    timers = []
    if level > 0:
        if passive_before.get("stored"):
            timers.append(
                f"Пассив +{passive_before['rate']}/ч — накоплено "
                f"🪙 {_money(passive_before['stored'])} из {_money(passive_before['cap'])}"
            )
        else:
            timers.append(
                f"Пассив +{passive_before['rate']}/ч — начисление в "
                f"{passive_before['next_hour'].strftime('%H:%M')}"
            )
    if active:
        timers.append(f"Смена — осталось {_farm_duration(_farm_seconds_left(status))}")
    if quarry.get("running"):
        timers.append(f"Карьер — осталось {_farm_duration(int(quarry.get('seconds_left', 0) or 0))}")
    if timers:
        lines.append("\n<b>⏱ Таймеры</b>")
        lines.extend(timers)

    rows = []
    if status.get("can_start"):
        # The four useful presets fit on one row and leave the menu readable.
        hour_row = []
        for hours in C.FARM_QUICK_HOUR_CHOICES:
            hour_row.append({
                "text": f"{hours} ч",
                "callback_data": callback_data(user_id, "farmstart", str(hours)),
            })
        if hour_row:
            rows.append(hour_row)
    if status.get("can_ticket"):
        # Above «Забрать сейчас», because it is the better version of the same wish and
        # the two are one tap apart -- somebody impatient should meet the free one first.
        rows.append([{
            "text": f"🎟 Билет: закончить смену ({int(status.get('tickets', 0) or 0)})",
            "callback_data": callback_data(user_id, "farmticket"),
        }])
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
    if not status.get("shovel_upgraded") and not int(status.get("shovel_runs", 0) or 0):
        rows.append([{
            "text": f"🪏 Купить лопату · {_money(int(status.get('shovel_cost', C.SHOVEL_COST)))}",
            "callback_data": callback_data(user_id, "shovelbuy"),
        }])
    # Buying a pickaxe is not going anywhere, so it stays offered even while the creature
    # is on the farm; only the START row obeys the one-place-at-a-time rule. A row of
    # buttons that answer with "существо на ферме" is exactly what this removes.
    if quarry.get("can_start"):
        rows.append([{
            "text": f"⛏ {hours}ч",
            "callback_data": callback_data(user_id, "quarrystart", str(hours)),
        } for hours in C.QUARRY_HOUR_CHOICES])
    elif quarry.get("can_cancel"):
        # The quarry's half of «Забрать сейчас». Same wording as the farm's on purpose:
        # it is the same promise, and the two sit on the same screen.
        rows.append([{
            "text": "❌ Забрать добычу сейчас",
            "callback_data": callback_data(user_id, "quarrycancel"),
        }])
    elif not quarry.get("running") and not (
        quarry.get("pickaxe_unlimited") or int(quarry.get("pickaxe_runs", 0) or 0) > 0
    ):
        rows.append([{
            "text": f"⛏ Купить кирку · {_money(int(quarry.get('cost', 150)))}",
            "callback_data": callback_data(user_id, "quarrybuy"),
        }])
    # One tap both opens the meadow screen and starts that round -- see the "meadow"
    # callback in bot_listener, which starts on any argument and merely redraws without
    # one. start_meadow explains a short wallet on its own, so the button is drawn
    # regardless of whether this player can currently afford it.
    rows.append([
        {"text": "🌼 Малая поляна", "callback_data": callback_data(user_id, "meadow", "small")},
        {"text": "🌼 Большая поляна", "callback_data": callback_data(user_id, "meadow", "big")},
    ])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


# What an opened cell shows. Matches the strings pets_meadow.build_board fills a board
# with (see pets_meadow.EMPTY/DIAMOND/JACKPOT/REFILL) -- kept as plain strings there, not
# an enum, so a stored round is plain JSON, and mirrored here as plain strings for the
# same reason this module has no import of pets_meadow at all: everything this screen
# needs already comes back through pets.meadow_status.
_MEADOW_CELL_ICON = {"empty": "▪️", "diamond": "💎", "jackpot": "🏆", "refill": "🔄"}


def meadow_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    """The diamond-lotto board: a size picker when nothing is running, the side x side
    grid itself once it is.

    Every closed cell is the SAME button no matter what is buried under it -- meadow_status
    never tells this screen what an unopened cell holds, so there is nothing here that
    could leak it either. An opened cell keeps a callback (the bare "meadow" redraw)
    rather than being left with none: Telegram does not allow a button with no
    callback_data at all, and a stray tap on a filled square should just redraw, not error.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)

    status = pets.meadow_status(entry, user_id)
    tickets = int(status.get("tickets", 0) or 0)
    active = status.get("round")

    # 🎫, never the farm's 🎟: the two tickets sit on the same farm screen and buy
    # completely different things.
    lines = ["🌼 <b>Поляна</b>", f"🎫 {_plural(tickets, 'билет', 'билета', 'билетов')} в кошельке."]

    rows = []
    if not active:
        lines.append(
            "\n<i>Копай клетки и ищи алмазы. Билеты падают со смен на ферме и из "
            "подземелья.</i>"
        )
        for option in status.get("meadows", []):
            title = escape(str(option.get("title") or ""))
            side = int(option.get("side", 0) or 0)
            lines.append(f"\n<b>{title}</b> — поле {side}x{side}")
            lines.append(
                f"Закопано {_plural(int(option.get('diamonds', 0) or 0), 'алмаз', 'алмаза', 'алмазов')} · "
                f"{_plural(int(option.get('picks', 0) or 0), 'попытка', 'попытки', 'попыток')} · вход "
                f"🎫 {_plural(int(option.get('tickets', 0) or 0), 'билет', 'билета', 'билетов')}"
            )
            extras = []
            if option.get("has_jackpot"):
                extras.append("🏆 суперприз забирает разом все закопанные алмазы")
            if option.get("has_refill"):
                extras.append("🔄 клетка обновления восстанавливает бои на арене")
            if extras:
                lines.append("<i>" + " · ".join(extras) + "</i>")
            rows.append([{
                "text": f"🌼 {option.get('title') or ''} · {option.get('tickets', 0)} 🎫",
                "callback_data": callback_data(user_id, "meadow", str(option.get("size") or "")),
            }])
    else:
        title = escape(str(active.get("title") or ""))
        side = int(active.get("side", 0) or 0)
        finished = bool(active.get("finished"))
        lines.append(f"\n<b>{title}</b> — поле {side}x{side}")
        lines.append(
            f"Правила: {_plural(int(active.get('diamonds', 0) or 0), 'алмаз', 'алмаза', 'алмазов')} "
            f"закопано, {_plural(int(active.get('picks', 0) or 0), 'попытка', 'попытки', 'попыток')} на раунд"
        )
        lines.append(
            f"Уже нашёл: {_plural(int(active.get('rubies_won', 0) or 0), 'алмаз', 'алмаза', 'алмазов')}"
        )
        if finished:
            lines.append("✅ Раунд закончен, поле перекопано.")
            if active.get("refilled"):
                lines.append("🔄 Бои на арене восстановлены до полного бака.")
        else:
            lines.append(
                f"Осталось {_plural(int(active.get('picks_left', 0) or 0), 'попытка', 'попытки', 'попыток')}."
            )

        # The full board only exists once finished; before that only opened cells are in
        # `revealed` at all, by design (see pets_meadow.public_state) -- so a KeyError here
        # would mean the anti-cheat boundary broke, not that this screen needs a fallback.
        if finished and active.get("board"):
            opened = dict(enumerate(active["board"]))
        else:
            opened = {int(index): value for index, value in (active.get("revealed") or {}).items()}
        for row_start in range(0, side * side, side):
            row = []
            for index in range(row_start, row_start + side):
                value = opened.get(index)
                if value is None:
                    row.append({
                        "text": "⬜",
                        "callback_data": callback_data(user_id, "meadowpick", str(index)),
                    })
                else:
                    row.append({
                        "text": _MEADOW_CELL_ICON.get(value, "▪️"),
                        "callback_data": callback_data(user_id, "meadow"),
                    })
            rows.append(row)

        if finished:
            rows.append([{
                "text": "🔄 Играть ещё раз",
                "callback_data": callback_data(user_id, "meadow", str(active.get("size") or "")),
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
        detail = (
            "эффект появится позже" if key == "endurance"
            else f"в бою {effective.get(key, level)}"
        )
        lines.append(f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {level} <i>({detail})</i>")
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {effective.get('armor', 0)} <i>(из снаряжения)</i>")
    # Luck is the one stat whose payoff is invisible in a fight log, so its current find
    # bonus is spelled out where the points are actually bought.
    luck_bonus = C.luck_drop_multiplier(effective.get("luck", levels.get("luck", C.STAT_MIN_LEVEL))) - 1
    lines.append(
        f"\n🍀 Удача сейчас даёт <b>+{luck_bonus * 100:.0f}%</b> к шансу найти вещь"
        " — и в бою, и на ферме."
    )
    # Магия is invisible in the stat column for the same reason Удача was: its payoff is
    # in the scroll lines of a fight log rather than in the swing. Both halves are spelled
    # out where the points are actually bought, the floor included -- otherwise a player
    # at Магия 1 reads "свитки бьют от Магии" and concludes their scrolls do nothing.
    swing = C.BASE_DAMAGE + effective.get("strength", 1) * C.DAMAGE_PER_POINT
    power = C.spell_power(effective.get("magic", C.STAT_MIN_LEVEL), swing)
    lines.append(
        f"🔮 Магия сейчас даёт <b>{power:.0f}</b> силы свитков"
        f" — обычный удар бьёт на {swing:.0f}."
    )
    if power <= swing * C.SPELL_POWER_SWING_FLOOR + 0.01:
        lines.append(
            "<i>Это пол в 45% от удара: свитки начнут расти, как только Магия его догонит.</i>"
        )
    weapon = C.find_item((pet.get("equipped") or {}).get("weapon"))
    scaling = C.weapon_scaling(weapon)
    if scaling != C.WEAPON_SCALING_STRENGTH:
        lines.append(
            f"<i>Оружие «{escape(weapon.name)}»: {C.WEAPON_SCALING_LABELS[scaling]}.</i>"
        )
    lines.append(f"\n🪙 Монеты: {_money(coins)}")
    points = pets.available_stat_points(pet)
    if points:
        lines.append(f"🎯 Свободные очки: <b>{points}</b> <i>(сначала тратятся они)</i>")
    lines.append(
        "\n<i>Если стат меньше половины стата соперника, проявится слабость: "
        "сила режет уклонение, здоровье — HP, ловкость повышает входящий урон, удача — меткость.</i>"
    )
    lines.append("\n<i>Максимального уровня нет. Чем выше, тем дороже следующий пункт.</i>")
    lines.append(
        "<i>Сброс возвращает монеты по той же цене, по какой статы покупались, "
        "а пересобрать билд стоит только алмазов.</i>"
    )

    rows = []
    for key in C.STAT_KEYS:
        level = levels.get(key, C.STAT_MIN_LEVEL)
        one = C.stat_upgrade_cost(level)
        ten = C.total_stat_cost(level + 10, level)
        row = [{
            "text": f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]} +1 — {_money(one)}",
            "callback_data": callback_data(user_id, "up", key),
        }]
        # The +10 button is not a discount, just fewer taps: it charges the sum of the ten
        # individual steps, and buys as many as the wallet reaches.
        row.append({
            "text": f"+10 — {_money(ten)}",
            "callback_data": callback_data(user_id, "up10", key),
        })
        rows.append(row)
    # The refund rides on the button. A reset is a real sum of money now, and how much
    # is the whole of the decision -- a button that only names its diamond price makes
    # the player guess at the half that matters.
    refund = pets.stat_refund_value(pet)
    rows.append([{
        "text": (f"🔄 Сбросить статы — {C.STAT_RESPEC_RUBY_COST} 💎"
                 + (f" · 🪙 +{_money(refund)}" if refund else "")),
        "callback_data": callback_data(user_id, "respec"),
    }])
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
    # Above every slot, because that is the point of pinning: the two weapons a player
    # actually rotates should not sit four taps deeper than the sixty they keep as forge
    # material. Hidden while nothing is pinned -- an empty category teaches nothing.
    pinned = pets.favourite_items(pet)
    if pinned:
        rows.append([{
            "text": f"⭐ Избранное · {len(pinned)}",
            "callback_data": callback_data(
                user_id, "bagitems", slot_argument(pets.FAVOURITE_SLOT),
            ),
        }])
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
        "text": "📜 Боевые свитки · 4 слота",
        "callback_data": callback_data(user_id, "skills"),
    }])
    personal = pets.personal_paint_status(entry, user_id)
    rune_count = len(personal.get("runes", []))
    applied_count = len(personal.get("applied", []))
    rows.append([{
        "text": f"🎨 Персональные руны · {rune_count}",
        "callback_data": callback_data(user_id, "paintrunes"),
    }])
    if rune_count or applied_count:
        lines.append(
            f"\n🎨 Персональные покрасы: {rune_count} готово к применению · "
            f"{applied_count} уже на предметах и свитках."
        )
    rows.append([{
        "text": "🛒 Магазин", "callback_data": callback_data(user_id, "store"),
    }])
    rows.append([{
        "text": "⚒️ Кузница", "callback_data": callback_data(user_id, "forge"),
    }])
    if owned:
        lines.append(
            f"\n<i>В сумке: {_plural(len(owned), 'предмет', 'предмета', 'предметов')}.</i>"
        )
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def personal_paint_runes_view(entry: str, user_id) -> tuple[str, dict]:
    """Owner-bound artwork runes earned by accepted specialist paint quests."""
    state = pets.personal_paint_status(entry, user_id)
    runes = state.get("runes", [])
    applied = state.get("applied", [])
    lines = [
        "🎨 <b>Персональные руны</b>",
        "Руна хранит фотографию твоего покраса. Применяется один раз к предмету "
        "того же типа: изображение становится его аватаркой, положительные боевые "
        "параметры усиливаются на 30%.",
        "<i>Шансы срабатывания, длительность и отрицательные параметры не растут.</i>",
    ]
    rows = []
    if runes:
        lines.append("\n<b>Готовы к применению:</b>")
    for index, rune in enumerate(runes, 1):
        target = PERSONAL_PAINT_TARGET_NAMES.get(str(rune.get("target") or ""), "предмет")
        lines.append(f"{index}. 🎨 {escape(target)} · +30%")
        rows.append([{
            "text": f"🎨 Выбрать {target}",
            "callback_data": callback_data(user_id, "paintrune", str(rune.get("id") or "")),
        }])
    if not runes:
        lines.append("\nПока нет свободных персональных рун. Они выдаются после принятия отдельного рунического квеста.")
    if applied:
        lines.append(f"\n<i>Уже применено: {len(applied)}. Снять или сложить два покраса нельзя.</i>")
    rows.append([{"text": "🎒 К снаряжению", "callback_data": callback_data(user_id, "bag")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def personal_paint_targets_view(entry: str, user_id, rune_id: str) -> tuple[str, dict]:
    state = pets.personal_paint_status(entry, user_id)
    rune = next((row for row in state.get("runes", []) if row.get("id") == rune_id), None)
    if rune is None:
        return personal_paint_runes_view(entry, user_id)
    target = PERSONAL_PAINT_TARGET_NAMES.get(str(rune.get("target") or ""), "предмет")
    candidates = pets.personal_paint_candidates(entry, user_id, rune_id)
    lines = [
        f"🎨 <b>Выбери {escape(target)}</b>",
        "Руна расходуется навсегда. У выбранной вещи появится твоя картинка, "
        f"а {_personal_paint_bonus_text(str(rune.get('target') or ''))}.",
    ]
    rows = []
    for index, row in enumerate(candidates):
        rows.append([{
            "text": f"🎨 {str(row.get('name') or row.get('code'))[:42]}",
            "callback_data": callback_data(user_id, "paintapply", f"{rune_id},{index}"),
        }])
    if not candidates:
        lines.append("\n<i>Подходящих непрокрашенных вещей этого типа пока нет.</i>")
    rows.append([{"text": "◀️ К персональным рунам", "callback_data": callback_data(user_id, "paintrunes")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def forge_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    labels = {"cursed": "проклятых", "common": "обычных", "rare": "редких", "legendary": "легендарный"}
    # The ordinary ladder and the cursed one both pass through "rare" and "legendary", so
    # the plain labels above are ambiguous exactly where it matters most -- a player who
    # can't tell "5 редких → легендарный" from the cursed rung of the same shape would
    # forge the wrong five hoping for a curse, or the other way round.
    # Two forms per rung: what goes IN is plural ("6 проклятых"), what comes OUT is one
    # item ("редкая проклятая"). One shared label read "6 проклятых → редких проклятых",
    # which promises a pile and hands back a single weapon.
    cursed_labels = {"rare": "редких проклятых", "legendary": "легендарных проклятых"}
    cursed_results = {"rare": "редкая проклятая", "legendary": "легендарная проклятая"}
    status = pets.forge_status(entry, user_id)
    lines = [
        "⚒️ <b>Кузница</b>",
        (f"Кузница берёт предметы одного типа и одной редкости и возвращает предмет того "
         f"же типа редкостью выше: {pets.FORGE_REQUIREMENTS['common']} обычных перчаток — "
         f"редкие перчатки, {pets.FORGE_REQUIREMENTS['rare']} редких — легендарные. "
         f"Для пушек есть отдельная проклятая ветка: {pets.FORGE_REQUIREMENTS['cursed']} "
         f"проклятых — редкая проклятая пушка, {pets.FORGE_REQUIREMENTS['rare']} редких "
         f"проклятых — легендарная проклятая."),
        "<i>Надетые и защищённые вещи кузница не трогает. Сначала уходят самые слабые.</i>",
    ]
    rows = []
    recipes = status.get("recipes", [])
    if not recipes:
        lines.append("\nПереплавлять пока нечего — не хватает предметов одного типа "
                     "и одной редкости.")
    for recipe in recipes:
        rarity = recipe["rarity"]
        slot = recipe["slot"]
        result_rarity = recipe["result_rarity"]
        required = recipe["required"]
        cursed = bool(recipe.get("cursed"))
        skull = "☠️ " if cursed else ""
        ingr_label = cursed_labels[rarity] if cursed and rarity in cursed_labels else labels[rarity]
        result_label = cursed_results[result_rarity] if cursed and result_rarity in cursed_results else labels[result_rarity]
        kind = f"{C.SLOT_EMOJI[slot]} {C.SLOT_NAMES[slot]}"
        ingredients = [C.find_item(code) for code in recipe.get("ingredients", [])]
        lines.append(
            f"\n<b>{skull}{kind}: {required} {ingr_label} → {result_label}</b> "
            f"(в сумке {recipe['available']})"
        )
        if ingredients:
            lines.append("Будут использованы: " + ", ".join(
                f"«{escape(item.name)}»" for item in ingredients if item is not None
            ))
        # No disabled state: forge_status only returns recipes that are ready, so every
        # button on this screen forges.
        rows.append([{
            "text": f"⚒️ {skull}{kind} · {required} {ingr_label} → {result_label}",
            # rarity:slot:cursed, which parse_callback hands back whole. The cursed flag
            # rides along because "5 редких" now names two different recipes -- the
            # ordinary rung and the cursed one -- and a button that dropped it would
            # silently forge the wrong pile. A bare "rarity:slot" (an already-rendered
            # button from before this flag existed) still parses: its missing third field
            # reads as "not cursed", which is exactly what it always meant.
            "callback_data": callback_data(user_id, "reforge", f"{rarity}:{slot}:{'1' if cursed else '0'}"),
        }])
    rune_state = pets.rune_status(entry, user_id)
    rune_names = {"fire": "Огонь", "frost": "Лёд", "water": "Вода", "earth": "Земля", "air": "Воздух", "plants": "Растения"}
    lines.append(f"\n🔮 <b>Зачарования</b> · 1 руна + {rune_state['cost']} рубинов")
    owned_runes = [f"{rune_names[element]} ×{count}" for element, count in rune_state["runes"].items() if count]
    lines.append(" · ".join(owned_runes) if owned_runes else "Рун пока нет.")
    rows.append([{
        "text": "🔮 Выбрать руну",
        "callback_data": callback_data(user_id, "runemenu") if owned_runes else callback_data(user_id, "noop"),
    }])
    rows.append([{
        "text": "🛠️ Ковка оружия — скоро",
        "callback_data": callback_data(user_id, "weaponforge"),
    }])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def enchant_weapon_view(entry: str, user_id, code: str) -> tuple[str, dict]:
    item = C.find_item(code)
    pet = pets.get_pet(entry, user_id)
    if item is None or item.slot != "weapon" or pet is None or code not in pet.get("inventory", []):
        return forge_view(entry, user_id, 0)
    state = pets.rune_status(entry, user_id)
    names = {"fire": "🔥 Огонь", "frost": "❄️ Лёд", "water": "💧 Вода", "earth": "🪨 Земля", "air": "💨 Воздух", "plants": "🌿 Растения"}
    existing = state["enchantments"].get(code)
    painted = code in (pet.get("personal_enchantments") or {})
    count = int(existing in pets.RUNE_ELEMENTS) + int(painted)
    lines = [
        f"🔮 <b>Зачаровать «{escape(item.name)}»</b>",
        f"Зачарования: {count}/2 · максимум одна стихия и один персональный покрас.",
    ]
    if existing in pets.RUNE_ELEMENTS:
        lines.append(f"Уже наложена стихия: {escape(pets.RUNE_NAMES.get(existing, existing))}.")
    else:
        lines.append(f"Цена стихии: {state['cost']} рубинов и 1 руна.")
    rows = []
    for element, label in names.items():
        owned = int(state["runes"].get(element, 0) or 0)
        action = "enchant" if owned and existing not in pets.RUNE_ELEMENTS else "noop"
        rows.append([{"text": f"{label} · {owned}", "callback_data": callback_data(user_id, action, f"{code}:{element}")}])
    rows.append([{"text": "◀️ К кузнице", "callback_data": callback_data(user_id, "forge")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def rune_enchant_view(entry: str, user_id) -> tuple[str, dict]:
    state = pets.rune_status(entry, user_id)
    names = {"fire": "🔥 Огонь", "frost": "❄️ Лёд", "water": "💧 Вода", "earth": "🪨 Земля", "air": "💨 Воздух", "plants": "🌿 Растения"}
    rows = [[{
        "text": f"{label} · {int(state['runes'].get(element, 0) or 0)}",
        "callback_data": callback_data(user_id, "enchantrune" if state["runes"].get(element) else "noop", element),
    }] for element, label in names.items()]
    rows.append([{"text": "◀️ К кузнице", "callback_data": callback_data(user_id, "forge")}])
    return "🔮 <b>Выбери руну</b>\nЦена зачарования: 1 руна и 15 рубинов.", {"inline_keyboard": rows}


def rune_weapon_view(entry: str, user_id, element: str) -> tuple[str, dict]:
    state = pets.rune_status(entry, user_id)
    if element not in state["runes"] or not state["runes"].get(element):
        return rune_enchant_view(entry, user_id)
    pet = pets.get_pet(entry, user_id) or {}
    weapons = [C.find_item(code) for code in pet.get("inventory", [])]
    weapons = [item for item in weapons if item is not None and item.slot == "weapon"]
    enchanted = pet.get("weapon_enchantments") or {}
    rows = [[{
        "text": f"🔮 {item.name[:26]}" + (" · стихия уже есть" if enchanted.get(item.code) in pets.RUNE_ELEMENTS else ""),
        "callback_data": callback_data(
            user_id, "noop" if enchanted.get(item.code) in pets.RUNE_ELEMENTS else "enchant",
            f"{item.code}:{element}",
        ),
    }] for item in weapons]
    rows.append([{"text": "◀️ К рунам", "callback_data": callback_data(user_id, "runemenu")}])
    return (
        "🔮 <b>Выбери оружие для руны</b>\n"
        "На оружии помещаются два разных зачарования: одна стихия и один персональный покрас."
    ), {"inline_keyboard": rows}


def skills_view(entry: str, user_id) -> tuple[str, dict]:
    """The live loadout: three ordinary scrolls and one once-per-fight ultimate."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    unlocked = pets.owned_scrolls(entry, user_id)
    personal_scrolls = set(pets.personal_enchanted_scrolls(entry, user_id))
    lines = [
        "📜 <b>Боевые свитки</b>",
        "В обычных боях существо само выбирает между атакой, защитой и доступными свитками.",
        "<i>У каждого доступного свитка одинаковый шанс. Четвёртый слот — ультимейт один раз за бой.</i>",
        f"<i>Открыто свитков: {len(unlocked)} из {len(SCROLLS.SCROLLS)}. Новые попадаются за покрас и сложные квесты.</i>",
        "<i>Новый #япокрасил: шанс 2,5%; если раньше не выпал, "
        "на 20-м новом покрасе свиток гарантирован. "
        "Принятый квест сложности 4/5: шанс 12%/20%; если раньше не выпал, "
        "на 6-м принятом сложном квесте свиток гарантирован.</i>",
    ]
    rows = []
    for index, code in enumerate(pets.skill_loadout(entry, user_id), start=1):
        spell = SCROLLS.scroll(code) if code else None
        if spell is None:
            # An empty slot is a normal state, not a fault: a creature fields as many
            # scrolls as it has found, and the fourth slot stays open until an ultimate
            # turns up at all.
            lines.extend([
                "",
                f"<b>{index}. Пусто</b>" + (" · ультимейт" if index == 4 else ""),
                "<i>Слот свободен.</i>",
            ])
            rows.append([{
                "text": f"Поставить в слот {index}",
                "callback_data": callback_data(user_id, "skillpick", f"{index},0"),
            }])
            continue
        title = str(spell["name"]).split(": ", 1)[-1]
        # Scrolls have no cooldown and never had one -- every entry is uses: 1. The header
        # used to print spell['cooldown'], a key the catalogue does not define, and the
        # KeyError took the whole screen down to the generic error card.
        lines.extend([
            "",
            f"<b>{index}. {escape(spell['icon'])} {escape(title)}</b>"
            + (" · УЛЬТИМЕЙТ" if spell["ultimate"] else "")
            + (" · 🎨 ПОКРАС +30%" if code in personal_scrolls else "")
            + " · один раз за бой",
            escape(spell["short"]),
        ])
        lines.extend(f"▸ {escape(line)}" for line in _scroll_effect_lines(spell, code in personal_scrolls))
        lines.append(
            SCROLLS.element_label(spell["element"])
            + (" · Нельзя увернуться." if not spell["dodgeable"] else "")
        )
        rows.append([
            {
                "text": f"Слот {index} · {spell['icon']} {title}",
                "callback_data": callback_data(user_id, "skillpick", f"{index},0"),
            },
            {
                "text": "✖️",
                "callback_data": callback_data(user_id, "skillclear", str(index)),
            },
        ])
    # Shown only once the set is actually complete, and nowhere else in the game: a rule
    # that pays for a restriction is best learned at the moment the restriction is met.
    element = SCROLLS.loadout_element(pets.skill_loadout(entry, user_id))
    if element:
        lines.append(
            f"\n✨ <b>{escape(SCROLLS.element_label(element))}</b> — четыре свитка одной "
            f"стихии: <b>+{round(C.ELEMENTAL_RESONANCE_BONUS * 100)}%</b> к магическому "
            "урону и лечению."
        )
    rows.append([{"text": "🎒 К снаряжению", "callback_data": callback_data(user_id, "bag")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def skill_picker_view(entry: str, user_id, slot: int, page: int = 0) -> tuple[str, dict]:
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    slot = max(1, min(4, int(slot)))
    unlocked = set(pets.owned_scrolls(entry, user_id, ultimate=(slot == 4)))
    catalogue = SCROLLS.ULTIMATE_SCROLLS if slot == 4 else SCROLLS.REGULAR_SCROLLS
    pool = [spell for spell in catalogue if spell["code"] in unlocked]
    page_size = 6
    total_pages = max(1, (len(pool) + page_size - 1) // page_size)
    page = min(max(0, int(page)), total_pages - 1)
    visible = pool[page * page_size:(page + 1) * page_size]
    current = pets.skill_loadout(entry, user_id)[slot - 1]
    personal_scrolls = set(pets.personal_enchanted_scrolls(entry, user_id))
    lines = [
        f"📜 <b>Слот {slot}</b> · {page + 1}/{total_pages}",
        "Ультимейт используется не больше одного раза за бой." if slot == 4
        else "Выбери открытый магический свиток или свиток умения.",
    ]
    rows = []
    if not pool:
        # The ordinary state for a new creature rather than an error, so it says what to
        # go and do instead of apologising for an empty list.
        lines.extend([
            "",
            "<i>Открытых свитков для этого слота пока нет.</i>",
            "Свитки выпадают за #япокрасил и за принятые сложные квесты.",
        ])
    if current:
        rows.append([{
            "text": "✖️ Освободить слот",
            "callback_data": callback_data(user_id, "skillclear", str(slot)),
        }])
    for spell in visible:
        chosen = spell["code"] == current
        lines.extend([
            "",
            f"<b>{escape(spell['icon'])} {escape(spell['name'])}</b>"
            + (" 🎨 +30%" if spell["code"] in personal_scrolls else "")
            + (" ✅" if chosen else ""),
            escape(spell["short"]),
        ])
        lines.extend(
            f"▸ {escape(line)}"
            for line in _scroll_effect_lines(spell, spell["code"] in personal_scrolls)
        )
        lines.append(
            SCROLLS.element_label(spell["element"])
            + (" · Нельзя увернуться" if not spell["dodgeable"] else "")
            + " · один раз за бой"
        )
        if not chosen:
            rows.append([{
                "text": f"{spell['icon']} Выбрать · {str(spell['name']).split(': ', 1)[-1]}",
                "callback_data": callback_data(user_id, "setskill", f"{slot}:{spell['code']}"),
            }])
    navigation = []
    if page:
        navigation.append({
            "text": "◀️", "callback_data": callback_data(user_id, "skillpick", f"{slot},{page - 1}"),
        })
    if page + 1 < total_pages:
        navigation.append({
            "text": "▶️", "callback_data": callback_data(user_id, "skillpick", f"{slot},{page + 1}"),
        })
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "◀️ К свиткам", "callback_data": callback_data(user_id, "skills")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def weapon_forge_view(user_id) -> tuple[str, dict]:
    return (
        "🛠️ <b>Ковка оружия</b>\n\nЗдесь позже можно будет создавать оружие по рецептам. Функция пока готовится.",
        {"inline_keyboard": [[{"text": "◀️ В кузницу", "callback_data": callback_data(user_id, "forge")}]]},
    )


def slot_argument(slot: str, page: int = 0) -> str:
    return f"{slot},{max(0, int(page))}"


def parse_slot_argument(argument: str) -> tuple[str, int]:
    slot, _, raw_page = str(argument or "").partition(",")
    return slot, int(raw_page) if raw_page.isdecimal() else 0


def _buyable_here(item, storefront_codes) -> bool:
    """Whether tapping "Купить" on this item would work right now.

    Shared by the sort key below and the button-rendering loop so the two can never
    disagree: every slot is only buyable while it sits in the player's current
    twelve-hour storefront.
    """
    return item.code in storefront_codes


def shop_slot_view(entry: str, user_id, xp: int, slot: str) -> tuple[str, dict]:
    """The shop's personal 12-hour shelf for one non-weapon slot.

    ``slot_view`` is the full catalogue for a slot -- hundreds of entries, most of them
    drop-only trophies -- and reaching it from the 🛒 shop was the whole problem: whether
    the two buyable accessories landed on page one depended on how much the player already
    owned, so an active player still opened the shop onto a wall of "только из боёв" with
    no button anywhere. Weapons never had that problem because they have their own
    storefront (``store_view``). This is the same thing for the other four slots: a short,
    unpaginated shelf, so "buy an amulet" is always exactly two taps.
    """
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    if slot not in C.SLOT_KEYS or slot == "weapon":
        return store_view(entry, user_id, xp)
    owned = set(pet.get("inventory", []))
    stock = pets.daily_storefront_items(entry, slot, user_id=user_id)

    lines = [
        f"🛒 <b>{escape(C.SLOT_NAMES[slot])}</b> {C.SLOT_EMOJI[slot]}",
        (f"Твоя витрина: {C.STOREFRONT_NORMAL_COUNT} обычных и {C.STOREFRONT_RARE_COUNT} "
         "редкий предмет, обновляется в 00:00 по Москве.\n"),
    ]
    rows = []
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
    storefront = pets.daily_storefront_items(entry, slot, user_id=user_id)
    storefront_prices = {item.code: item.price for item in storefront}
    storefront_codes = set(storefront_prices)

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
            not _buyable_here(item, storefront_codes),
            item.code,
        ),
    )
    total_pages = max(1, (len(all_items) + SLOT_PAGE_SIZE - 1) // SLOT_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = all_items[page * SLOT_PAGE_SIZE:(page + 1) * SLOT_PAGE_SIZE]
    lines = [f"{C.SLOT_EMOJI[slot]} <b>{escape(C.SLOT_NAMES[slot])}</b> · {page + 1}/{total_pages}\n"]
    if not any(item.code not in owned and _buyable_here(item, storefront_codes) for item in all_items):
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
        elif item.code in storefront_codes:
            state = _coins(storefront_prices[item.code])
        elif item.source == "drop":
            state = "только из боёв"
        else:
            state = _coins(item.price)
        lines.append(f"<b>{escape(item.name)}</b>{mark}{lock_mark} — {_bonus_text(item)} · {state}")
        rarity = C.RARITY_LABELS.get(getattr(item, "rarity", "common"), "⚪ Обычное")
        lines[-1] = f"<b>{escape(item.name)}</b>{mark}{lock_mark} · {rarity} · {_bonus_text(item)} · {state}"
        if item.slot == "weapon" and item.code in owned:
            details = pets.weapon_details(entry, user_id, item.code)
            lines.append(
                f"🏷 Первый Владелец - {escape(details.get('first_owner') or '')}\n"
                f"⚔️ Победил петов: {details.get('pet_wins', 0)} · "
                f"👹 Победил мобов: {details.get('mob_wins', 0)} · "
                f"👑 Победил боссов: {details.get('boss_wins', 0)}"
            )
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
        elif _buyable_here(item, storefront_codes):
            rows.append([{
                "text": f"Купить {item.name} — {_money(storefront_prices[item.code])}",
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
    pinned_view = slot == pets.FAVOURITE_SLOT
    if slot not in C.SLOT_KEYS and not pinned_view:
        return bag_view(entry, user_id, xp)

    inventory = pet.get("inventory", [])
    favourites = set(pets.favourite_items(pet))
    counts = Counter(
        item.code for code in inventory
        if (item := C.find_item(code)) is not None
        and (item.code in favourites if pinned_view else item.slot == slot)
    )
    owned = [C.find_item(code) for code in counts]
    locked = set(pet.get("locked_items", []))
    equipped = pet.get("equipped") or {}
    # The pinned page spans slots, so what is worn has to be asked per item: one code for
    # the whole page cannot answer it once a sword and a shield share a screen.
    worn_codes = {code for code in equipped.values() if code}
    worn = equipped.get(slot)
    personal_enchantments = pet.get("personal_enchantments") or {}
    elemental_enchantments = pet.get("weapon_enchantments") or {}
    owned.sort(key=lambda item: (item.code not in worn_codes, item.slot, item.code)
               if pinned_view else (item.code != worn, item.code))
    total_pages = max(1, (len(owned) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = owned[page * INVENTORY_PAGE_SIZE:(page + 1) * INVENTORY_PAGE_SIZE]

    copy_count = sum(counts.values())
    noun = _plural(copy_count, "предмет", "предмета", "предметов").split(" ", 1)[1]
    title = ("⭐ Избранное" if pinned_view
             else f"🎒 Моя сумка · {C.SLOT_NAMES[slot]}")
    lines = [
        f"<b>{escape(title)}</b>",
        f"{copy_count} {noun} · {len(owned)} видов · {page + 1}/{total_pages}",
    ]
    rows = []
    if not visible:
        lines.append(
            "\nЗдесь пока пусто. Отметь звездой то, что носишь чаще всего."
            if pinned_view else
            "\nЗдесь пока пусто. Новое оружие появляется в магазине дня или после победы."
        )
    for number, item in enumerate(visible, start=page * INVENTORY_PAGE_SIZE + 1):
        is_worn = item.code in worn_codes if pinned_view else item.code == worn
        copies = counts[item.code]
        spare_copies = copies - int(is_worn)
        mark = " ✅ надето" if is_worn else ""
        lock_mark = " 🔒" if item.code in locked else ""
        star_mark = " ⭐" if item.code in favourites else ""
        # On the pinned page the slot is the one thing the heading no longer says.
        slot_mark = f" · {C.SLOT_NAMES[item.slot]}" if pinned_view else ""
        paint_mark = " 🎨 +30%" if item.code in personal_enchantments else ""
        element = elemental_enchantments.get(item.code) if item.slot == "weapon" else None
        element_mark = (
            f" 🔮 {escape(pets.RUNE_NAMES.get(element, element))}"
            if element in pets.RUNE_ELEMENTS else ""
        )
        label = C.RARITY_LABELS.get(item.rarity, item.rarity)
        count_mark = f" ×{copies}" if copies > 1 else ""
        lines.append(
            f"\n{number}. <b>{escape(item.name)}</b>{count_mark}{mark}{lock_mark}{star_mark}"
            f"{element_mark}{paint_mark}"
        )
        lines.append(f"{label}{slot_mark} · {_bonus_text(item)}")
        if item.slot == "weapon":
            details = pets.weapon_details(entry, user_id, item.code)
            lines.append(
                f"🏷 Первый Владелец - {escape(details.get('first_owner') or '')}\n"
                f"⚔️ Петы: {details.get('pet_wins', 0)} · 👹 Мобы: {details.get('mob_wins', 0)} · "
                f"👑 Боссы: {details.get('boss_wins', 0)}"
            )
        if item.description:
            lines.append(f"<i>{escape(item.description)}</i>")

        if is_worn:
            rows.append([{
                "text": f"Снять · {item.name}",
                "callback_data": callback_data(user_id, "unequip", item.slot),
            }])
        else:
            rows.append([{
                "text": f"Надеть · {item.name}",
                "callback_data": callback_data(user_id, "equip", item.code),
            }])
        rows.append([{
            "text": "🔓 Открепить" if item.code in locked else "🔒 Закрепить",
            "callback_data": callback_data(user_id, "lock", item.code),
        }, {
            "text": ("☆ Из избранного"
                     if item.code in favourites
                     else "⭐ В избранное"),
            "callback_data": callback_data(user_id, "fav", item.code),
        }])
        # A lock is a safety control, not merely a warning: keep destructive actions
        # out of the convenient page as well as enforcing the same rule in pets.py.
        if spare_copies > 0 and item.code not in locked:
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
    short = {"all": "Все", "cursed": "☠️", "common": "⚪", "rare": "🔵", "legendary": "🟡"}
    buttons = []
    for rarity in RARITY_FILTERS:
        if rarity == "cursed" and not include_cursed:
            continue
        text = short[rarity] + (" ✓" if rarity == selected else "")
        argument = collection_argument(rarity, 0) if paged else rarity
        buttons.append({"text": text, "callback_data": callback_data(user_id, action, argument)})
    return [buttons[:3], buttons[3:]]


def store_view(entry: str, user_id, xp: int, rarity: str = "all") -> tuple[str, dict]:
    """The 12-hour weapon shelf plus direct routes to other rotating equipment shelves."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, _ = _rarity_argument(rarity)
    if rarity == "cursed":
        rarity = "all"
    owned = set(pet.get("inventory", []))
    stock = pets.daily_storefront_weapons(entry, user_id=user_id)
    visible = [item for item in stock if rarity == "all" or item.rarity == rarity]
    lines = [
        "🛒 <b>Витрина</b>",
        (f"В 00:00 по Москве появляются {C.STOREFRONT_NORMAL_COUNT} обычных и "
         f"{C.STOREFRONT_RARE_COUNT} редкий предмет для каждого слота."),
    ]
    lines.append(f"Фильтр: <b>{RARITY_FILTER_NAMES[rarity]}</b>\n")
    if not visible:
        lines.append("Сейчас оружия этой редкости нет.")
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
        # The full seven-item window occupies at most three compact rows.
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
        }, {
            "text": f"{C.SLOT_EMOJI['shield']} {C.SLOT_NAMES['shield']}",
            "callback_data": callback_data(user_id, "shopslot", "shield"),
        }],
        [{"text": "🎒 Моё снаряжение", "callback_data": callback_data(user_id, "bag")}],
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
    # Which stat the swing reads is the single most consequential fact about a magic
    # weapon -- equipping one with Магия at 1 costs far more than any stat line on it
    # gives back -- so it is printed with the stats rather than left to the description.
    scaling = C.weapon_scaling(item)
    if scaling != C.WEAPON_SCALING_STRENGTH:
        parts.append(C.WEAPON_SCALING_LABELS[scaling])
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
    scaling = C.weapon_scaling(item)
    if scaling != C.WEAPON_SCALING_STRENGTH:
        parts.append(C.WEAPON_SCALING_LABELS[scaling])
    if isinstance(getattr(item, "effect", None), dict) and item.effect.get("code"):
        effect_text = str(item.effect.get("text") or "").strip()
        parts.append(f"🧿 {escape(effect_text)}" if effect_text else "🧿")
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
        f"Базовая награда за победу: {C.ARENA_WIN_GOLD_MIN}–{C.ARENA_WIN_GOLD_MAX} монет"
        f" и {C.WIN_XP} опыта;"
        f" за соперника ниже уровнем — меньше, выше — больше (до ±25%)."
        f" Поражение: {round(C.ARENA_LOSS_TRANSFER_SHARE * 100)}% текущих монет и алмазов "
        "переходят победителю."
    )
    if farming:
        lines.append(
            "\n🌾 Питомец на ферме — сам он подождёт с боями, но напасть на него "
            "по-прежнему можно."
        )
    elif left <= 0:
        lines.append(f"\n<b>{ARENA_NO_FIGHTS_NOTICE}</b>")
        lines.append("Следующий бой появится после указанного выше отсчёта.")

    pve = pets.pve_allowance(entry, user_id)
    lines.append(
        f"\n👾 Атаки на мобов: {pve['available']} из {pve['capacity']}"
        + (f" · сброс в {datetime.fromisoformat(pve['resets_at']).strftime('%H:%M')}"
           if pve["seconds_until_reset"] else "")
    )
    rubies = pets.ruby_balance(entry, user_id)
    if rubies:
        lines.append(f"💎 Руби: {_money(rubies)}")

    # The other of the two arenas that has to show this. Below the counters and above the
    # birthday card: it is a standing condition on the creature about to fight, not news.
    mark = pets.debuff_for(pet)
    if mark:
        lines.append(f"\n{mark['emoji']} <b>{escape(mark['title'])}</b> — {escape(mark['line'])}")
        lines.append(f"<i>{escape(mark['description'])}</i>")
        lines.append(f"<i>{escape(mark['hint'])}</i>")

    # Above the search, and above the fight bank: it costs no fight, it lasts one day,
    # and somebody who opens the arena with an empty bank should still see it.
    party = pets.birthday(entry, viewer=user_id)
    if party and not party["is_me"]:
        lines.append(f"\n🎂 <b>Сегодня день рождения!</b> {escape(party['owner_name'])}")
        if party["greeted"]:
            lines.append("<i>Ты уже поздравил.</i>")
        else:
            lines.append("<i>Поздравь — награду получите оба, и это не потратит бой.</i>")
    elif party:
        lines.append(
            f"\n🎂 <b>С днём рождения!</b> Тебя поздравили: {party['greeted_count']}"
        )

    rows = []
    if party and not party["is_me"] and not party["greeted"]:
        rows.append([{
            "text": f"🎉 Поздравить · {str(party['owner_name'])[:24]}",
            "callback_data": callback_data(user_id, "bday"),
        }])
    if left > 0 and not farming:
        rows.append([{
            "text": "🔍 Найти соперника",
            "callback_data": callback_data(user_id, "search"),
        }])
    # PVE next to PVP because the choice between them belongs in one place -- but on its
    # OWN counter, so an empty arena bank does not hide the mobs and vice versa.
    if pve["available"] > 0 and not farming:
        rows.append([{
            "text": f"👾 Найти моба ({pve['available']})",
            "callback_data": callback_data(user_id, "mob"),
        }])
    rows.append([{"text": "📜 История боёв", "callback_data": callback_data(user_id, "history")}])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def support_view(entry: str, user_id) -> tuple[str, dict]:
    """The pitch, the roll of honour, and one way in."""
    lines = [f"💜 <b>{escape(donations.PITCH_TITLE)}</b>\n"]
    lines.extend(escape(paragraph) for paragraph in donations.PITCH_PARAGRAPHS)
    lines.append("\n<b>Что получают поддержавшие</b>")
    lines.extend(f"• {escape(perk)}" for perk in donations.PITCH_PERKS)
    lines.append(f"\n<i>{escape(donations.PITCH_FOOTER)}</i>")

    top = donations.donors(entry)
    if top:
        lines.append("\n🏆 <b>Топ поддержавших</b>")
        for place, donor in enumerate(top, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")
            note = f" — {escape(donor['note'])}" if donor["note"] else ""
            lines.append(f"{medal} <b>{escape(donor['name'])}</b> · ${donor['amount']}{note}")
    else:
        # An empty list is worth showing rather than hiding: "nobody yet" is an invitation,
        # while a missing section reads as a feature that does not work.
        lines.append("\n🏆 <b>Топ поддержавших</b>\nПока пусто — можно стать первым.")

    rows = [
        [{"text": "💜 Задонатить", "callback_data": callback_data(user_id, "supportgive")}],
        _back_row(user_id),
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


def support_confirm_view(user_id) -> tuple[str, dict]:
    """The speed bump between an impulse and a message about money."""
    return escape(donations.CONFIRM_QUESTION), {
        "inline_keyboard": [
            [{"text": "✅ Да", "callback_data": callback_data(user_id, "supportyes")}],
            [{"text": "Нет, просто смотрел", "callback_data": callback_data(user_id, "support")}],
        ],
    }


def mob_view(entry: str, user_id, block: dict | None) -> tuple[str, dict]:
    """One rolled mob, ready to be fought or re-rolled.

    The block is passed IN rather than rolled here: this module renders, it does not
    decide, and a view that rolled its own mob would deal a different one every time the
    screen was redrawn.
    """
    if not block:
        return notice_view(user_id, "Мобов сейчас нет. Попробуй ещё раз.")
    allowance = pets.pve_allowance(entry, user_id)
    stats_line = " · ".join(
        f"{C.STAT_EMOJI.get(key, '')}{block['stats'].get(key, 0)}" for key in C.STAT_KEYS
    )
    lines = [
        f"👾 <b>{escape(block['name'])}</b>",
        f"<i>{escape(block['flavour'])}</i>\n",
        f"Сложность: <b>{escape(block['tier_name'])}</b> · сила ⚡ {_money(block['power'])}",
        stats_line + (f" · 🛡{block['armor']}" if block.get("armor") else ""),
        # The taunt brings its own quotation marks where it wants them -- wrapping it in
        # another pair rendered «« ... »» on every mob that already quotes itself.
        f"\n<i>{escape(block['taunt'])}</i>",
        f"\n👾 Атаки на мобов: {allowance['available']} из {allowance['capacity']}"
        + (f" · сброс в {datetime.fromisoformat(allowance['resets_at']).strftime('%H:%M')}"
           " — сразу у всех" if allowance["seconds_until_reset"] else ""),
        "\nЗа моба платят меньше, чем за игрока, но у мобов свой счётчик — арену они "
        "не тратят. И только с них падают 💎 руби.",
    ]
    rows = [
        [{"text": "⚔️ В бой", "callback_data": callback_data(user_id, "mobfight",
                                                              f"{block['code']}:{block['tier']}")}],
        [{"text": "🔍 Другой моб", "callback_data": callback_data(user_id, "mob")}],
        [{"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")}],
        _back_row(user_id),
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


def mob_result_text(reward: dict, report: str) -> str:
    """What the chat is told after a PVE fight, on top of the ordinary battle report."""
    mob = reward.get("mob") or {}
    lines = [
        f"👾 <b>{escape(mob.get('name') or 'Моб')}</b> · {escape(mob.get('tier_name') or '')}\n",
        report,
    ]
    bits = []
    if reward.get("gold"):
        bits.append(f"🪙 +{_money(int(reward['gold']))}")
    if reward.get("xp"):
        bits.append(f"✨ +{_money(int(reward['xp']))}")
    if reward.get("rubies"):
        bits.append(f"💎 +{int(reward['rubies'])}")
    rune = reward.get("rune") or {}
    if rune.get("granted"):
        bits.append(f"🔮 {escape(str(rune.get('element') or 'руна'))} +{int(rune['granted'])}")
    if reward.get("farm_ticket"):
        bits.append("🎟️ ферма +1")
    if reward.get("dungeon_ticket"):
        bits.append("🎫 подземелье +1")
    if bits:
        lines.append("\n" + " · ".join(bits))
    if reward.get("dropped_name"):
        worn = " (надето)" if reward.get("auto_equipped") else ""
        lines.append(f"🎁 Находка: «{escape(str(reward['dropped_name']))}»{worn}")
    if reward.get("levels_gained"):
        lines.append(f"⬆️ Новый уровень: {reward.get('level')}")
    return "\n".join(lines)


def mob_result_keyboard(user_id) -> dict:
    """The next fight after PVE must stay in PVE, not fall into the player search."""
    return {"inline_keyboard": [
        [{"text": "👾 Найти моба", "callback_data": callback_data(user_id, "mob")}],
        _back_row(user_id),
    ]}


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
    their_element = theirs.get("element")
    if their_element in C.CHARACTER_ELEMENTS:
        lines.append(
            f"Элемент: {C.CHARACTER_ELEMENT_ICONS[their_element]} "
            f"{escape(C.CHARACTER_ELEMENT_NAMES[their_element])}"
        )
    lines.append(f"Хозяин: {escape(theirs.get('owner_name') or 'кто-то')}")
    lines.append(
        f"Боёв: {theirs.get('fights', 0)} / побед: {theirs.get('wins', 0)}"
    )
    lines.append("")
    for key in C.STAT_KEYS:
        lines.append(f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {their_stats.get(key, 1)}")
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {their_stats.get('armor', 0)}")
    # Their stats above are already the reduced ones, so this is not gossip -- it is why
    # the opponent you are sizing up reads weaker than their level suggests.
    their_mark = pets.debuff_for(theirs)
    if their_mark:
        lines.append(
            f"\n{their_mark['emoji']} <b>{escape(their_mark['title'])}</b>"
            f" — {escape(their_mark['line'])}"
        )
        lines.append(f"<i>{escape(their_mark['description'])}</i>")

    # How many times today, and nothing more: repeating a matchup costs nothing now,
    # so this is information rather than a warning.
    repeats = pets.repeat_fights(entry, user_id, opponent_id)
    if repeats:
        lines.append("")
        lines.append(f"{repeats['tag']} <i>{escape(repeats['hint'])}</i>")

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
        lines.append("<i>Решение по урону после лимита ходов.</i>")

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
            if reward.get("transfer_gold"):
                lines.append(f"🪙 +{_coins(reward['transfer_gold'])} от проигравшего")
            if reward.get("loss_gold"):
                lines.append(f"🪙 −{_coins(reward['loss_gold'])}")
            if reward.get("transfer_rubies"):
                lines.append(f"💎 +{int(reward['transfer_rubies'])} от проигравшего")
            if reward.get("loss_rubies"):
                lines.append(f"💎 −{int(reward['loss_rubies'])}")
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
        transferred = record.get("transfer_gold") or 0
        lost = record.get("loss_gold") or 0
        transferred_rubies = record.get("transfer_rubies") or 0
        lost_rubies = record.get("loss_rubies") or 0
        # Bare numbers here, with no noun to agree with -- the line is already dense and
        # "(Победа, +45)" reads fine next to a column of them.
        if draw:
            outcome += f", +{C.DRAW_XP} опыта"
        elif won and (gold or transferred):
            outcome += f", +{_money(gold + transferred)}"
        elif lost:
            outcome += f", −{_money(lost)}"
        if transferred_rubies:
            outcome += f", +{int(transferred_rubies)} 💎"
        elif lost_rubies:
            outcome += f", −{int(lost_rubies)} 💎"
        if attacked:
            lines.append(f"⚔️ Вы напали: {who} ({outcome})")
        else:
            lines.append(f"🛡 На вас напали: {who} ({outcome})")

    rows = [[{"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")}], _back_row(user_id)]
    return "\n".join(lines), {"inline_keyboard": rows}


# ----------------------------------------------------------------------------- mail

# One Telegram message holds 4096 characters. Thirty events do not come close, but an
# event's text is partly other people's names, so the whole thing is trimmed rather than
# risking the API rejecting the send outright and showing nothing at all.
MAIL_MAX_CHARS = 3800

MAIL_ICONS = {
    "attack": "⚔️", "defense": "🛡", "farm": "🌾", "gift_in": "🎁", "gift_out": "🎁",
    "quest_ok": "🎯", "quest_no": "🎯", "scroll": "📜",
}
MAIL_OUTCOMES = {"win": "победа", "loss": "поражение", "draw": "ничья"}


def mail_day_label(day: str) -> str:
    """"Сегодня" / "Вчера" / "09.08" for one ISO date, against the chat's calendar.

    Public because the Mini App groups the same feed under the same headings, and the
    page cannot work this out for itself: its idea of "today" is the phone's, which is
    not necessarily the chat's (see pets_web.handle_mail).
    """
    today = pets.today()
    if day == today.isoformat():
        return "Сегодня"
    if day == (today - timedelta(days=1)).isoformat():
        return "Вчера"
    try:
        return date.fromisoformat(day).strftime("%d.%m")
    except ValueError:
        return day


def _mail_who(event: dict) -> str:
    """The other side, named the way the arena names things: creature first, owner after."""
    pet = escape(event.get("pet_name") or "")
    owner = escape(event.get("owner_name") or "")
    if pet and owner:
        return f"<b>{pet}</b> ({owner})"
    return f"<b>{pet or owner or '?'}</b>"


def _mail_find(event: dict) -> str:
    if not event.get("item_name"):
        return ""
    return f", находка: «{escape(event['item_name'])}»"


def _mail_line(event: dict) -> str:
    kind = event.get("kind")
    icon = MAIL_ICONS.get(kind, "•")
    coins = int(event.get("coins", 0) or 0)
    money = ""
    if coins > 0:
        money = f", +{_money(coins)} 🪙"
    elif coins < 0:
        money = f", −{_money(-coins)} 🪙"
    # Diamonds ride in the same tail as coins and are signed the same way. They used to be
    # dropped here entirely: `pets.mail` has always handed them over, the web feed has
    # always drawn them, and this line read `coins` alone -- so a defender who lost four
    # diamonds and no coins got a mailbox line with no number in it at all. A currency
    # that leaves a wallet has to be legible in the place players go to ask what they
    # missed.
    rubies = int(event.get("rubies", 0) or 0)
    if rubies > 0:
        money += f", +{_money(rubies)} 💎"
    elif rubies < 0:
        money += f", −{_money(-rubies)} 💎"
    if kind == "farm":
        hours = int(event.get("hours", 0) or 0)
        xp = int(event.get("xp", 0) or 0)
        body = f"Ферма, {_plural(hours, 'час', 'часа', 'часов')}{money}"
        if xp:
            body += f", +{_money(xp)} опыта"
        return f"{icon} {body}{_mail_find(event)}"
    if kind in ("quest_ok", "quest_no"):
        title = escape(event.get("pet_name") or "квест")
        if kind == "quest_no":
            note = escape(event.get("note") or "")
            return f"{icon} Квест «{title}» отклонён" + (f": {note}" if note else "")
        xp = int(event.get("xp", 0) or 0)
        tickets = int(event.get("tickets", 0) or 0)
        tail = money
        if xp:
            tail += f", +{_money(xp)} опыта"
        if tickets:
            tail += f", +{tickets} билет"
        if event.get("scroll_name"):
            tail += " · 📜 «" + escape(event["scroll_name"]) + "»"
        return f"{icon} Квест «{title}» принят{tail}"
    if kind == "scroll":
        name = escape(event.get("scroll_name") or "свиток")
        rarity = "ультимейт" if event.get("scroll_ultimate") else "свиток"
        return f"{icon} Открыт {rarity}: «{name}»"
    if kind in ("gift_in", "gift_out"):
        item = escape(event.get("item_name") or "предмет")
        verb = "Подарок для" if kind == "gift_out" else "Подарок от"
        return f"{icon} {verb} {_mail_who(event)}: «{item}»"
    # Both fight kinds. The verb carries who started it, which is the first thing anybody
    # wants to know -- see history_view for the same reasoning at greater length.
    lead = "Ты напал на" if kind == "attack" else "На тебя напал"
    outcome = MAIL_OUTCOMES.get(event.get("outcome"), "")
    tail = f" — {outcome}{money}" if outcome else money
    return f"{icon} {lead} {_mail_who(event)}{tail}{_mail_find(event)}"


def mail_view(entry: str, user_id) -> tuple[str, dict]:
    """Everything that happened to this player, oldest at top and newest at bottom.

    Grouped by day with the time in front of every line, because the question the mailbox
    answers is "what did I miss" -- and that is a question about when, not about which
    subsystem produced the row. Fights, farm shifts and gifts therefore share one column
    instead of living in three separate menus.
    """
    # Quest verdicts live in quests.py, which imports pets -- so they are collected here
    # and handed to pets.mail, which still does the merging and the cap. See its docstring.
    events = pets.mail(entry, user_id, extra=quests.mail_events(entry, user_id))

    def render(visible: list[dict], trimmed: bool = False) -> str:
        lines = ["📬 <b>Почта</b>\n"]
        if trimmed:
            lines.append("<i>…старые события скрыты.</i>\n")
        if not visible:
            lines.append("Пока пусто. Здесь будут бои, смены на ферме и подарки.")
        current_day = None
        for event in visible:
            day = event.get("day") or ""
            if day != current_day:
                gap = "" if current_day is None else "\n"
                current_day = day
                lines.append(f"{gap}<b>{mail_day_label(day)}</b>")
            lines.append(f"<code>{escape(event.get('at') or '--.--')}</code> {_mail_line(event)}")
        return "\n".join(lines)

    # Keep the bottom of the feed when Telegram's message limit is reached: that is where
    # the newest events live now. Removing complete rows also avoids cutting an HTML tag.
    visible = list(events)
    text = render(visible)
    while len(text) > MAIL_MAX_CHARS and len(visible) > 1:
        visible.pop(0)
        text = render(visible, trimmed=True)
    rows = [
        [{"text": "⚔️ Арена", "callback_data": callback_data(user_id, "fight")},
         {"text": "🌾 Ферма", "callback_data": callback_data(user_id, "farm")}],
        _back_row(user_id),
    ]
    return text, {"inline_keyboard": rows}


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
        # Signed, because the gap is no longer always a bonus: a granted debuff scales the
        # effective number down, and the old hardcoded "+" printed "(+-1)".
        extra = f" <i>({total - purchased:+d})</i>" if total != purchased else ""
        lines.append(f"{C.STAT_EMOJI[key]} {C.STAT_NAMES[key]}: {total}{extra}")
    lines.append(f"{C.ARMOR_EMOJI} {C.ARMOR_NAME}: {effective.get('armor', 0)}")
    # Directly under the stats it is subtracting from, with its joke and its way out.
    # The numbers above are already the reduced ones, so leaving the mark unexplained
    # here would make the card look like it was doing arithmetic wrong.
    mark = pets.debuff_for(pet)
    if mark:
        lines.append("")
        lines.append(f"{mark['emoji']} <b>{escape(mark['title'])}</b> — {escape(mark['line'])}")
        lines.append(f"<i>{escape(mark['description'])}</i>")
        lines.append(f"<i>{escape(mark['hint'])}</i>")

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


def pet_view(entry: str, user_id, xp: int = 0) -> tuple[str, dict]:
    """The owner's own view of their creature: the same card /pet prints, plus the two
    buttons only the owner has any business pressing."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        coins = pets.balance_for(entry, user_id, xp)
        text = (
            "🖼 <b>Твоё существо</b>\n\nПришли фотографию своей покрашенной работы и дай ей имя. "
            "Эта работа станет твоим существом и будет участвовать в боях против других игроков."
            f"\n\n🪙 У тебя: {_money(coins)}"
        )
        action = {
            "text": "🐣 Создать существо",
            "callback_data": callback_data(user_id, "tame"),
        }
        return text, {"inline_keyboard": [[action], _back_row(user_id)]}
    rows = [
        [
            {"text": "✏️ Переименовать", "callback_data": callback_data(user_id, "rename")},
            {"text": "🖼 Картинка существа", "callback_data": callback_data(user_id, "photo")},
        ],
    ]
    cage = pets.cage_level(entry, user_id)
    if cage < C.CAGE_MAX_LEVEL:
        rows.append([{
            "text": f"⬆️ Улучшить клетку — {_money(C.CAGE_UPGRADE_COSTS[cage])}",
            "callback_data": callback_data(user_id, "upcage", "pet"),
        }])
    rows.append(_back_row(user_id))
    return pet_card(entry, user_id, pet), {"inline_keyboard": rows}


def no_pet_view(user_id) -> tuple[str, dict]:
    text = (
        "У тебя ещё нет существа.\n\n"
        "Создай его бесплатно.\n"
        "Это должна быть твоя покрашенная работа: она станет существом и будет сражаться с другими игроками."
    )
    return text, {"inline_keyboard": [[
        {"text": "🐣 Создать существо", "callback_data": callback_data(user_id, "tame")},
    ], _back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    rows = []
    if "Сначала приручи существо" in text:
        rows.append([{"text": "🐣 Создать существо", "callback_data": callback_data(user_id, "tame")}])
    rows.append(_back_row(user_id))
    return text, {"inline_keyboard": rows}
