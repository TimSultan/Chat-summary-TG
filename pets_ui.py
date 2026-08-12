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

import economy
import casino
import pets
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

    rows = []
    if webapp_url:
        # First, and alone on its row: it is the whole game rather than one more screen.
        rows.append([{"text": "🎮 Открыть игру", "web_app": {"url": webapp_url}}])
    quest_button = ("❗ " if quests.has_available_quests(entry, user_id) else "") + "📜 Квесты"
    updates_button = (
        "❗ 📰 Обновления" if pets_updates.has_unread(entry, user_id) else "📰 Обновления"
    )

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
    """The few ideas a newcomer needs; individual screens teach their own details."""
    lines = [
        "ℹ️ <b>Как играть</b>\n",
        "Открой /arena в личке бота — здесь живёт вся игра.",
        "\n<b>1. Создай своё существо</b>",
        "Купи клетку и приручи фигурку. "
        "<b>На картинке существа должен быть именно твой собственный покрас</b> — "
        "загрузи фотографию своей раскрашенной миниатюры.",
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
            f"гарантирован не позже {int(reward.get('scroll_pity', 0))}-го сложного квеста."
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
            "\nСначала покрась что-то новое: старые работы не подходят."
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


def quests_view(entry: str, user_id, kind: str = "paint") -> tuple[str, dict]:
    """Compact quest shelf: three readable cards and three matching buttons."""
    kind = "real" if kind == "real" else "paint"
    board = quests.real_quest(entry, user_id) if kind == "real" else quests.daily_quest(entry, user_id)
    cards = board.get("quests") or []
    paint = kind == "paint"
    title = "🎯 <b>Квесты на покрас · 3 карточки</b>" if paint else "🌍 <b>Квест в реале</b>"
    lines = [title, f"\n⏳ Новая подборка через <b>{_quest_timer(board.get('seconds_until_refresh', 0))}</b>."]
    lines.append(
        "Успей отправить фото до обновления. Выполни всё раньше — новая подборка придёт через 8 часов."
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
        buttons.append([{
            "text": f"{index}. {marker} {str(card.get('title') or 'Квест')[:45]}",
            "callback_data": callback_data(user_id, "questdetail", f"{kind}:{card.get('code')}"),
        }])
    if not cards:
        lines.append("\nПока доступных заданий нет. Проверим снова через 8 часов.")
    other = "real" if paint else "paint"
    buttons.append([{
        "text": "🌍 Квест в реале" if paint else "🎯 Три квеста на покрас",
        "callback_data": callback_data(user_id, "quests", other),
    }])
    buttons.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": buttons}


def quest_detail_view(entry: str, user_id, kind: str, code: str) -> tuple[str, dict]:
    """Full brief and a practical step-by-step tutorial for one selected card."""
    kind = "real" if kind == "real" else "paint"
    board = quests.real_quest(entry, user_id) if kind == "real" else quests.daily_quest(entry, user_id)
    card = next((row for row in board.get("quests", []) if row.get("code") == code), None)
    if card is None:
        return quests_view(entry, user_id, kind)
    paint = kind == "paint"
    reward = card.get("reward") or {}
    difficulty = int(card.get("difficulty", 1) or 1)
    status = card.get("status", "open")
    scroll_reward = (
        f"\n📜 Новый свиток: {round(float(reward['scroll_chance']) * 100)}%, "
        f"гарантирован не позже {int(reward.get('scroll_pity', 0))}-го сложного квеста."
        if reward.get("scroll_chance") else ""
    )
    lines = [
        f"{'🎯' if paint else '🌍'} <b>{escape(card.get('title') or 'Квест')}</b>",
        f"{quest_pips(difficulty)} {QUEST_DIFFICULTY_NAMES.get(difficulty, '')}",
        f"\n<b>{'Что красим' if paint else 'Что делаем'}:</b> {escape(card.get('subject') or '')}",
        f"\n<b>Техника:</b> {escape(card.get('technique') or '')}",
        f"\n💡 <b>Подсказка:</b> {escape(card.get('hint') or '')}",
        "\n<b>Как выполнить:</b>",
        "1. Возьми новую, ещё не показанную работу и подготовь нужную деталь.",
        f"2. {'Нанеси технику из описания небольшими контролируемыми этапами.' if paint else 'Выполни действие полностью, не только для фотографии.'}",
        "3. Сверь результат с подсказкой и поправь самые заметные места.",
        f"4. Сделай чёткое фото: {escape(card.get('proof') or 'готового результата')}.",
        f"5. Выложи фото в чат с хештегом <code>{escape(card.get('hashtag') or '')}</code>.",
        f"\n<b>Награда:</b> 🪙 {_money(int(reward.get('gold', 0)))} · ✨ {int(reward.get('xp', 0))} опыта · "
        f"🎟 {int(reward.get('tickets', 0))} · 🎁 {round(float(reward.get('drop_chance', 0)) * 100)}%",
        scroll_reward,
        f"\n⏳ До обновления: <b>{_quest_timer(board.get('seconds_until_refresh', 0))}</b>",
    ]
    if status == "review":
        lines.append("\n⏳ Работа уже на проверке у модератора.")
    elif status == "done":
        lines.append("\n✅ Квест принят и завершён.")
    else:
        lines.append("\n⚠️ Старые работы не подходят — нужно покрасить что-то новое.")
    rows = []
    rerolls = int(card.get("rerolls_left", 0) or 0)
    if status == "open" and rerolls:
        lines.append("Реролл даст квест на ступень сложнее, и награда тоже вырастет.")
        rows.append([{
            "text": f"🎲 Реролл · осталось {rerolls}",
            "callback_data": callback_data(user_id, "questreroll", f"{kind}:{code}"),
        }])
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
        if status.get("can_ticket"):
            # The distinction that matters, and the one a player will not assume: unlike
            # «Забрать сейчас», a ticket costs nothing off the payout.
            lines.append(
                f"🎟 Есть билет ({int(status.get('tickets', 0) or 0)} шт.) — смена закончится "
                f"через минуту, а заплатят как за все {planned} ч."
            )
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
    lines.append(f"🎟 Билетов: {int(status.get('tickets', 0) or 0)}")
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
    lines.append(f"\n🪙 Монеты: {_money(coins)}")
    lines.append("\n<i>Максимального уровня нет. Чем выше, тем дороже следующий пункт.</i>")

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
        "text": "📜 Боевые свитки · 4 слота",
        "callback_data": callback_data(user_id, "skills"),
    }])
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


def forge_view(entry: str, user_id, xp: int) -> tuple[str, dict]:
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    labels = {"common": "обычных", "rare": "редких", "legendary": "легендарный"}
    status = pets.forge_status(entry, user_id)
    lines = [
        "⚒️ <b>Кузница</b>",
        "5 обычных предметов превращаются в редкий, а 7 редких — в легендарный.",
        "<i>Надетые и защищённые вещи кузница не трогает. Сначала уходят самые слабые.</i>",
    ]
    rows = []
    for recipe in status.get("recipes", []):
        rarity = recipe["rarity"]
        result_rarity = recipe["result_rarity"]
        required = recipe["required"]
        ingredients = [C.find_item(code) for code in recipe.get("ingredients", [])]
        lines.append(
            f"\n<b>{required} {labels[rarity]} → {labels[result_rarity]}</b> "
            f"({recipe['available']} доступно)"
        )
        if ingredients:
            lines.append("Будут использованы: " + ", ".join(
                f"«{escape(item.name)}»" for item in ingredients if item is not None
            ))
        else:
            lines.append("Подходящих вещей пока нет.")
        rows.append([{
            "text": f"⚒️ {required} {labels[rarity]} → {labels[result_rarity]}",
            "callback_data": callback_data(
                user_id, "reforge" if recipe.get("can_forge") else "noop", rarity,
            ),
        }])
    rows.append([{
        "text": "🛠️ Ковка оружия — скоро",
        "callback_data": callback_data(user_id, "weaponforge"),
    }])
    rows.append(_back_row(user_id))
    return "\n".join(lines), {"inline_keyboard": rows}


def skills_view(entry: str, user_id) -> tuple[str, dict]:
    """The live loadout: three ordinary scrolls and one once-per-fight ultimate."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    unlocked = pets.owned_scrolls(entry, user_id)
    lines = [
        "📜 <b>Боевые свитки</b>",
        "В обычных боях существо само выбирает между атакой, защитой и доступными свитками.",
        "<i>У каждого доступного свитка одинаковый шанс. Четвёртый слот — ультимейт один раз за бой.</i>",
        f"<i>Открыто свитков: {len(unlocked)} из {len(SCROLLS.SCROLLS)}. Новые попадаются за покрас и сложные квесты.</i>",
        "<i>Новый #япокрасил: шанс 2,5%, свиток не позже 20-го покраса. "
        "Принятый квест сложности 4/5: шанс 12%/20%, свиток не позже 6-го сложного квеста.</i>",
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
            + (" · УЛЬТИМЕЙТ" if spell["ultimate"] else "") + " · один раз за бой",
            escape(spell["short"]),
        ])
        lines.extend(f"▸ {escape(line)}" for line in SCROLLS.effect_lines(spell))
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
            f"<b>{escape(spell['icon'])} {escape(spell['name'])}</b>" + (" ✅" if chosen else ""),
            escape(spell["short"]),
        ])
        lines.extend(f"▸ {escape(line)}" for line in SCROLLS.effect_lines(spell))
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
    storefront (``store_view``). This is the same thing for the other four slots: a short,
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
    daily_weapon_codes = {item.code for item in pets.daily_storefront_weapons(entry)}

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
    """The 12-hour weapon window plus direct routes to accessory shelves."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        return no_pet_view(user_id)
    rarity, _ = _rarity_argument(rarity)
    if rarity == "cursed":
        rarity = "all"
    owned = set(pet.get("inventory", []))
    stock = pets.daily_storefront_weapons(entry)
    visible = [item for item in stock if rarity == "all" or item.rarity == rarity]
    lines = [
        "🛒 <b>Витрина</b>",
        "Каждые 12 часов появляются 3 обычных и 3 редких оружия. Фиксированные вещи остаются.",
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

    pve = pets.pve_allowance(entry, user_id)
    lines.append(
        f"\n👾 Атаки на мобов: {pve['available']} из {pve['capacity']}"
        + (f" · сброс в {datetime.fromisoformat(pve['resets_at']).strftime('%H:%M')}"
           if pve["seconds_until_reset"] else "")
    )
    rubies = pets.ruby_balance(entry, user_id)
    if rubies:
        lines.append(f"💎 Руби: {_money(rubies)}")

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
            if reward.get("loss_gold"):
                lines.append(f"🪙 −{_coins(reward['loss_gold'])}")
            # A defender never chose this fight, so losing it pays a small consolation
            # instead of taking coins. Only one of the two lines can ever appear.
            if reward.get("consolation_gold"):
                lines.append(f"🪙 +{_coins(reward['consolation_gold'])} за стойкость")
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
        consolation = record.get("consolation_gold") or 0
        # Bare numbers here, with no noun to agree with -- the line is already dense and
        # "(Победа, +45)" reads fine next to a column of them.
        if draw:
            outcome += f", +{C.DRAW_XP} опыта"
        elif won and gold:
            outcome += f", +{_money(gold)}"
        elif lost:
            outcome += f", −{_money(lost)}"
        elif consolation:
            # A defeated defender is up, not down: showing nothing here used to make the
            # line look like the fight simply cost them their time.
            outcome += f", +{_money(consolation)}"
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


def pet_view(entry: str, user_id, xp: int = 0) -> tuple[str, dict]:
    """The owner's own view of their creature: the same card /pet prints, plus the two
    buttons only the owner has any business pressing."""
    pet = pets.get_pet(entry, user_id)
    if not pet:
        cage = pets.cage_level(entry, user_id)
        coins = pets.balance_for(entry, user_id, xp)
        if not cage:
            text = (
                "🖼 <b>Твоё существо</b>\n\nСначала купи клетку. После этого бот попросит "
                "фото твоей собственной раскрашенной миниатюры — она станет картинкой существа."
                f"\n\n🪙 У тебя: {_money(coins)}"
            )
            action = {
                "text": f"🏠 Купить клетку — {_money(C.CAGE_PRICE)}",
                "callback_data": callback_data(user_id, "buycage", "pet"),
            }
        else:
            text = (
                "🖼 <b>Твоё существо</b>\n\nКлетка готова. Теперь пришли фотографию "
                "именно своей раскрашенной миниатюры и дай ей имя."
                f"\n\n🪙 У тебя: {_money(coins)}"
            )
            action = {
                "text": f"🐣 Приручить свой покрас — {_money(C.TAME_PRICE)}",
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
        f"Сначала клетка ({_coins(C.CAGE_PRICE)}), потом приручение"
        f" ({_coins(C.TAME_PRICE)}).\n"
        "Существо должно быть твоей собственной раскрашенной фигуркой."
    )
    return text, {"inline_keyboard": [_back_row(user_id)]}


def notice_view(user_id, text: str) -> tuple[str, dict]:
    """A one-line result (bought, refused, renamed) with the way back. Used instead of a
    toast whenever the answer is longer than a callback answer's 200 characters."""
    return text, {"inline_keyboard": [_back_row(user_id)]}
