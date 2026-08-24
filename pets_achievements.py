"""Ачивки: what a player did that was worth doing, and what it pays.

A catalogue of data rows plus one predicate each. Nothing here reads the store, rolls a
reward or knows what a pet record looks like: an achievement is handed a PROFILE -- a flat
dict of finished counters assembled by `pets.achievement_profile` -- and answers yes or no.
That split is what makes the catalogue reviewable and the whole thing testable without a
database behind it, exactly like the scroll and weapon catalogues beside it.

What makes a row worth adding
-----------------------------
Only things a player would tell somebody about. "Выиграй 10 боёв" is a progress bar with a
name; "выиграй боссу оружием, которое стоит 60 монет" is a story. Every row should be one
of:

* a deliberate act nobody does by accident (a full cursed set, one weapon carried to a
  hundred wins),
* a thing that reveals mastery (the Phoenix taken without a single mistake),
* a thing that cannot happen twice (see LEGACY below),
* or a joke that turns out to be reachable.

Retroactivity, and why almost nothing is retroactive
----------------------------------------------------
An achievement that can still be earned starts at ZERO for everybody, including players
who did the thing years ago. Backfilling them would empty the whole feature in the second
it shipped: a veteran would open the screen to a wall of already-claimed rows and nothing
left to chase, which is the opposite of the point.

The exception is `legacy=True`: rows that are no longer POSSIBLE. Beating the Phoenix as
it fought before it became a hand-played boss is the archetype -- nobody can go and do
that now, so the only fair thing is to credit whoever did. Those are decided once, by
`pets.backfill_legacy_achievements`, from evidence that survives (see `deepest_floor`).
"""

from __future__ import annotations

from typing import Callable, Final


# --------------------------------------------------------------------------- the shape
class Achievement:
    """One row of the catalogue. Immutable data plus the question it asks."""

    __slots__ = ("code", "icon", "name", "description", "rubies", "farm_tickets",
                 "dungeon_tickets", "legacy", "hidden", "check")

    def __init__(
        self, code: str, icon: str, name: str, description: str,
        check: Callable[[dict], bool], *, rubies: int = 0, farm_tickets: int = 0,
        dungeon_tickets: int = 0, legacy: bool = False, hidden: bool = False,
    ):
        self.code = code
        self.icon = icon
        self.name = name
        # One line, and it says what to DO rather than what was done -- the screen is read
        # by people deciding what to chase, not only by people collecting.
        self.description = description
        self.check = check
        self.rubies = rubies
        self.farm_tickets = farm_tickets
        self.dungeon_tickets = dungeon_tickets
        # No longer earnable. Decided once at migration from surviving evidence.
        self.legacy = legacy
        # Not listed until it is earned. For the jokes that would stop being jokes if the
        # screen told you how to trigger them.
        self.hidden = hidden

    def payload(self) -> dict:
        """The row as a screen draws it, without the predicate."""
        return {
            "code": self.code, "icon": self.icon, "name": self.name,
            "description": self.description, "rubies": self.rubies,
            "farm_tickets": self.farm_tickets, "dungeon_tickets": self.dungeon_tickets,
            "legacy": self.legacy, "hidden": self.hidden,
        }


# ---------------------------------------------------------------- the profile contract
# Every predicate reads exactly these keys and nothing else. Assembled once per evaluation
# by `pets.achievement_profile`, so a row can never be slow: it is a dict lookup, not a
# walk over a save file, however many rows the catalogue grows to.
PROFILE_FIELDS: Final = (
    # -- the creature ---------------------------------------------------------------
    "level", "cage_level", "farm_level",
    "stats",                # {"strength": int, ... "magic": int} purchased levels
    "effective_stats",      # the same six plus "armor", after gear and pet level
    "power",                # the leaderboard's own number
    # -- fighting -------------------------------------------------------------------
    "wins", "fights",
    # Totals across every weapon carried -- but only wins taken WITH ONE EQUIPPED, and
    # only since the per-weapon ledger began. Never use these to ask "has this player ever
    # fought": `wins` and `fights` are the pet's own counters and answer that honestly.
    "boss_wins", "mob_wins", "pet_wins",
    "best_weapon_wins",     # most wins carried on ONE weapon
    "deepest_floor",        # the deepest dungeon floor ever stood on
    "phoenix_wins", "phoenix_perfect",       # the hand-played boss, and a flawless run
    # -- collecting -----------------------------------------------------------------
    "weapons_found", "weapons_total",
    "scrolls_owned", "scrolls_total",
    "equipped_rarities",    # {"legendary": 3, "cursed": 1, ...} across worn slots
    "equipped_slots",       # how many of the five slots are filled
    "equipped_cursed",      # worn items on the cursed ladder
    "equipped_magic",       # worn weapons that scale from Магия
    "personal_paints", "scroll_paints", "runes",
    # -- money ----------------------------------------------------------------------
    "gold_earned", "gold_spent", "rubies", "farm_tickets", "dungeon_tickets",
    # -- the chat the pet lives in --------------------------------------------------
    "quests_done", "quest_best_difficulty",
    "figurines_painted", "messages", "active_days", "best_work_posts",
)


def catalogue() -> tuple[Achievement, ...]:
    """Every achievement, in the order a screen should list them."""
    return ACHIEVEMENTS


def by_code(code: str) -> Achievement | None:
    return _BY_CODE.get(str(code))


def earned(profile: dict) -> tuple[str, ...]:
    """Codes this profile satisfies right now. Legacy rows are never decided here."""
    out = []
    for row in ACHIEVEMENTS:
        if row.legacy:
            continue
        try:
            if row.check(profile):
                out.append(row.code)
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            # A catalogue row must never be able to take the screen down. A predicate
            # that trips on an unexpected profile simply has not been earned yet.
            continue
    return tuple(out)


# --------------------------------------------------------------------------- catalogue
#
# Ordered the way the screen reads: what a new player can reach first, then the long
# collections, then the things that take a build rather than a session. Thresholds are
# picked so that no row is a formality and none is a second job -- a row nobody ever
# earns is the same as no row at all, and a row everybody has on day one is furniture.
def _worn_all(profile: dict, rarity: str) -> bool:
    """Every one of the five slots filled, and every one of them the same rarity.

    Both halves matter. Four legendary items and an empty glove slot is a rich player,
    not a completed set, and counting it would make the hardest row in the game an
    accident of what happened to drop.
    """
    return (int(profile.get("equipped_slots", 0) or 0) == 5
            and int((profile.get("equipped_rarities") or {}).get(rarity, 0) or 0) == 5)


def _stat(profile: dict, key: str) -> int:
    return int((profile.get("effective_stats") or {}).get(key, 0) or 0)


ACHIEVEMENTS: Final[tuple[Achievement, ...]] = (
    # ---------------------------------------------------------------- первые шаги
    # Reads the pet's own win counter rather than the per-weapon ledger. That ledger only
    # records a win taken WITH A WEAPON EQUIPPED and only since it started being kept, so
    # a player with four hundred wins behind them can have nothing in it -- and being told
    # to go and win a first fight is the one thing that would make this screen look broken
    # to exactly the people who have played longest.
    Achievement(
        "first_blood", "🩸", "Первая кровь",
        "Выиграй свой первый бой на арене ⚔️",
        lambda p: p.get("wins", 0) >= 1,
        farm_tickets=1,
    ),
    Achievement(
        "first_weapon", "🗡", "Что-то подобрал",
        "Найди своё первое оружие 🎁",
        lambda p: p.get("weapons_found", 0) >= 1,
        farm_tickets=1,
    ),
    Achievement(
        "first_scroll", "📜", "Читать умею",
        "Открой свой первый боевой свиток ✨",
        lambda p: p.get("scrolls_owned", 0) >= 1,
        rubies=1,
    ),
    Achievement(
        "descent", "🚪", "Вниз",
        "Спустись на второй этаж подземелья 🕯",
        lambda p: p.get("deepest_floor", 1) >= 2,
        dungeon_tickets=1,
    ),
    Achievement(
        "full_kit", "🎒", "Одет по погоде",
        "Займи все пять слотов снаряжения 🛡",
        lambda p: p.get("equipped_slots", 0) >= 5,
        rubies=1,
    ),
    # ---------------------------------------------------------------- коллекции
    Achievement(
        "hundred_weapons", "📦", "Сто железок",
        "Найди 100 разных оружий 🔍",
        lambda p: p.get("weapons_found", 0) >= 100,
        rubies=1, farm_tickets=2,
    ),
    Achievement(
        "quarter_catalogue", "📚", "Четверть склада",
        "Собери четверть всего каталога оружия 🗃",
        lambda p: p.get("weapons_found", 0) >= max(1, p.get("weapons_total", 0) // 4),
        rubies=2,
    ),
    Achievement(
        "half_catalogue", "🏛", "Половина мира",
        "Собери половину каталога оружия 🗝",
        lambda p: p.get("weapons_found", 0) >= max(1, p.get("weapons_total", 0) // 2),
        rubies=3, dungeon_tickets=2,
    ),
    # The only row in the file that asks for literally everything. It is meant to be
    # unfinished for a long time -- a collection with a reachable end stops being one.
    Achievement(
        "full_catalogue", "👑", "Всё железо мира",
        "Найди каждое оружие в игре 🏆",
        lambda p: p.get("weapons_total", 0) > 0
        and p.get("weapons_found", 0) >= p.get("weapons_total", 0),
        rubies=3, farm_tickets=5, dungeon_tickets=5,
    ),
    Achievement(
        "scroll_shelf", "📖", "Полка чтеца",
        "Открой 20 боевых свитков 📜",
        lambda p: p.get("scrolls_owned", 0) >= 20,
        rubies=2, farm_tickets=1,
    ),
    Achievement(
        "all_scrolls", "🌟", "Прочитано всё",
        "Открой все свитки до единого ✨📚",
        lambda p: p.get("scrolls_total", 0) > 0
        and p.get("scrolls_owned", 0) >= p.get("scrolls_total", 0),
        rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "rune_smith", "🪄", "Рунная кузница",
        "Скопи 10 рун для зачарования 🔥❄️",
        lambda p: p.get("runes", 0) >= 10,
        rubies=1,
    ),
    # ---------------------------------------------------------------- комплекты
    Achievement(
        "all_legendary", "🌈", "Полный комплект легенд",
        "Надень легендарное во все пять слотов 🟣🟣🟣",
        lambda p: _worn_all(p, "legendary"),
        rubies=3, dungeon_tickets=3,
    ),
    # Not merely hard but deliberately BAD for you: the cursed shelf trades real stats
    # away, so wearing all five is a choice nobody makes by accident.
    Achievement(
        "all_cursed", "☠️", "Проклят полностью",
        "Надень проклятое во все пять слотов 💀💀",
        lambda p: _worn_all(p, "cursed") or p.get("equipped_cursed", 0) >= 5,
        rubies=3, dungeon_tickets=2,
    ),
    Achievement(
        "wizard_kit", "🔮", "Настоящий волшебник",
        "Возьми волшебное оружие и подними Магию до 60 ✨",
        lambda p: p.get("equipped_magic", 0) >= 1 and _stat(p, "magic") >= 60,
        rubies=2, farm_tickets=1,
    ),
    Achievement(
        "own_paint", "🎨", "Свой покрас",
        "Нанеси персональный покрас на предмет 🖌",
        lambda p: p.get("personal_paints", 0) >= 1,
        rubies=1,
    ),
    Achievement(
        "paint_gallery", "🖼", "Личная галерея",
        "Покрась пять своих вещей 🎨🎨",
        lambda p: p.get("personal_paints", 0) >= 5,
        rubies=2, farm_tickets=2,
    ),
    # ---------------------------------------------------------------- арена
    Achievement(
        "hundred_wins", "🥊", "Сотня побед",
        "Выиграй 100 боёв 🏅",
        lambda p: p.get("wins", 0) >= 100,
        rubies=2, farm_tickets=2,
    ),
    # A weapon carried to a hundred wins is a weapon somebody refused to replace, which
    # is a much better story than a hundred wins spread over a hundred drops.
    Achievement(
        "old_faithful", "🗿", "Старый друг",
        "Возьми 100 побед ОДНИМ оружием ⚔️",
        lambda p: p.get("best_weapon_wins", 0) >= 100,
        rubies=3, dungeon_tickets=2,
    ),
    Achievement(
        "boss_hunter", "🐲", "Охотник на боссов",
        "Убей 10 боссов подземелья 👑",
        lambda p: p.get("boss_wins", 0) >= 10,
        rubies=2, dungeon_tickets=2,
    ),
    Achievement(
        "mob_grinder", "💀", "Коридорный",
        "Убей 200 обычных врагов в подземелье 🕯",
        lambda p: p.get("mob_wins", 0) >= 200,
        rubies=2, farm_tickets=2,
    ),
    Achievement(
        "power_thousand", "⚡", "Тысяча силы",
        "Разгони силу существа до 1000 💪",
        lambda p: p.get("power", 0) >= 1000,
        rubies=1, farm_tickets=1,
    ),
    # ---------------------------------------------------------------- подземелье
    Achievement(
        "phoenix_slain", "🔥", "Феникс повержен",
        "Убей Феникса пепельных залов 🪶",
        lambda p: p.get("phoenix_wins", 0) >= 1,
        rubies=2, dungeon_tickets=2,
    ),
    # The one row in the file that cannot be bought with stats at all: a maxed pet that
    # misreads one telegraph loses it, and a weak pet that reads every one keeps it.
    Achievement(
        "phoenix_flawless", "💯", "Ни одной ошибки",
        "Пройди Феникса, не ошибившись ни разу 🪶🔥",
        lambda p: bool(p.get("phoenix_perfect")),
        rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "floor_twenty", "🕳", "Двадцатый этаж",
        "Дойди до 20-го этажа подземелья 🔦",
        lambda p: p.get("deepest_floor", 1) >= 20,
        rubies=2, dungeon_tickets=2,
    ),
    Achievement(
        "floor_last", "⛏", "До самого дна",
        "Дойди до 45-го этажа подземелья 💎",
        lambda p: p.get("deepest_floor", 1) >= 45,
        rubies=3, dungeon_tickets=5,
    ),
    # ---------------------------------------------------------------- то, чего больше нет
    # The Phoenix used to be resolved in one press like any other boss. It is played by
    # hand now, so this is not a thing anybody can go and do -- only a thing somebody
    # did. Decided once, from the only evidence that survives: standing past floor five
    # required clearing floor five, and floor five was the bird.
    Achievement(
        "old_phoenix", "🕯", "Помнит старого Феникса",
        "Ты убил Феникса ещё до того, как он научился читать движения 🪶",
        lambda p: p.get("deepest_floor", 1) > 5,
        rubies=3, dungeon_tickets=3, legacy=True,
    ),
    # ---------------------------------------------------------------- чат и кисти
    Achievement(
        "first_figurine", "🖌", "Первая миниатюра",
        "Выложи свой первый #япокрасил 📸",
        lambda p: p.get("figurines_painted", 0) >= 1,
        rubies=1, farm_tickets=1,
    ),
    Achievement(
        "twenty_figurines", "🎨", "Полка растёт",
        "Выложи 20 покрашенных миниатюр 🖼",
        lambda p: p.get("figurines_painted", 0) >= 20,
        rubies=2, farm_tickets=3,
    ),
    Achievement(
        "best_work", "🏆", "Работа недели",
        "Забери первое место в конкурсе работ 🥇",
        lambda p: p.get("best_work_posts", 0) >= 1,
        rubies=3, farm_tickets=2,
    ),
    Achievement(
        "first_quest", "🎯", "Взял заказ",
        "Выполни свой первый квест на покрас ✅",
        lambda p: p.get("quests_done", 0) >= 1,
        farm_tickets=2,
    ),
    Achievement(
        "quest_master", "🏅", "Постоянный подрядчик",
        "Выполни 25 квестов 📋",
        lambda p: p.get("quests_done", 0) >= 25,
        rubies=3, farm_tickets=3,
    ),
    Achievement(
        "hardest_quest", "🔥", "Взял пятёрку",
        "Сдай квест максимальной сложности 💥",
        lambda p: p.get("quest_best_difficulty", 0) >= 5,
        rubies=3, dungeon_tickets=2,
    ),
    Achievement(
        "hundred_days", "📅", "Сто дней в чате",
        "Отметься в чате в 100 разных дней 💬",
        lambda p: p.get("active_days", 0) >= 100,
        rubies=2, farm_tickets=2,
    ),
    # ---------------------------------------------------------------- хозяйство
    Achievement(
        "cage_lux", "🏠", "Хоромы",
        "Прокачай клетку до 5-го уровня 🛠",
        lambda p: p.get("cage_level", 1) >= 5,
        rubies=1, farm_tickets=1,
    ),
    Achievement(
        "farm_boss", "🌾", "Крепкое хозяйство",
        "Подними ферму до 10-го уровня 🚜",
        lambda p: p.get("farm_level", 0) >= 10,
        rubies=2, farm_tickets=3,
    ),
    Achievement(
        "level_thirty", "🦅", "Тридцатый уровень",
        "Доведи существо до 30-го уровня ⭐",
        lambda p: p.get("level", 1) >= 30,
        rubies=2, dungeon_tickets=2,
    ),
    Achievement(
        "rich", "💰", "Через мои руки",
        "Заработай 100 000 монет за всё время 🪙",
        lambda p: p.get("gold_earned", 0) >= 100_000,
        rubies=3, farm_tickets=3,
    ),
    Achievement(
        "spender", "🔥", "Деньги не пахнут",
        "Потрать 50 000 монет 💸",
        lambda p: p.get("gold_spent", 0) >= 50_000,
        rubies=2, farm_tickets=2,
    ),
    # ---------------------------------------------------------- расширенная лестница
    # Short, medium and genuinely long goals between the original milestones. They use
    # counters already owned by the game, so progress is retroactive and auditable.
    Achievement(
        "arena_ten", "🥉", "Размялся",
        "Выиграй 10 боёв на арене ⚔️",
        lambda p: p.get("wins", 0) >= 10, farm_tickets=1,
    ),
    Achievement(
        "arena_fifty", "🥈", "Знакомое лицо",
        "Выиграй 50 боёв на арене 🛡",
        lambda p: p.get("wins", 0) >= 50, rubies=1, farm_tickets=1,
    ),
    Achievement(
        "arena_two_fifty", "🥇", "Арена помнит имя",
        "Выиграй 250 боёв на арене 🏟",
        lambda p: p.get("wins", 0) >= 250, rubies=2, farm_tickets=2,
    ),
    Achievement(
        "arena_five_hundred", "🏆", "Полтысячи побед",
        "Выиграй 500 боёв на арене ⚔️🔥",
        lambda p: p.get("wins", 0) >= 500, rubies=3, dungeon_tickets=2,
    ),
    Achievement(
        "arena_veteran", "🛡", "Тысяча выходов",
        "Проведи 1000 боёв на арене — победы и поражения считаются 📜",
        lambda p: p.get("fights", 0) >= 1000, rubies=2, farm_tickets=3,
    ),
    Achievement(
        "weapon_twenty_five", "⚔️", "Привыкаю к рукояти",
        "Возьми 25 побед одним оружием 🗡",
        lambda p: p.get("best_weapon_wins", 0) >= 25, rubies=1,
    ),
    Achievement(
        "weapon_two_fifty", "🗿", "Легенда одного клинка",
        "Возьми 250 побед одним оружием ⚔️",
        lambda p: p.get("best_weapon_wins", 0) >= 250, rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "five_figurines", "🖌", "Первая витрина",
        "Выложи 5 покрашенных миниатюр 📸",
        lambda p: p.get("figurines_painted", 0) >= 5, rubies=1, farm_tickets=1,
    ),
    Achievement(
        "fifty_figurines", "🖼", "Пятьдесят историй",
        "Выложи 50 покрашенных миниатюр 🎨",
        lambda p: p.get("figurines_painted", 0) >= 50, rubies=3, farm_tickets=3,
    ),
    Achievement(
        "hundred_figurines", "🏛", "Личная выставка",
        "Выложи 100 покрашенных миниатюр 🖼",
        lambda p: p.get("figurines_painted", 0) >= 100,
        rubies=3, farm_tickets=5, dungeon_tickets=2,
    ),
    Achievement(
        "three_best_works", "🌟", "Стабильное качество",
        "Трижды займи первое место в конкурсе работ 🥇",
        lambda p: p.get("best_work_posts", 0) >= 3, rubies=3, farm_tickets=3,
    ),
    Achievement(
        "five_quests", "📋", "Вошёл во вкус",
        "Выполни 5 квестов на покрас ✅",
        lambda p: p.get("quests_done", 0) >= 5, rubies=1, farm_tickets=2,
    ),
    Achievement(
        "fifty_quests", "🎖", "Гильдия доверяет",
        "Выполни 50 квестов любой сложности 📚",
        lambda p: p.get("quests_done", 0) >= 50, rubies=3, farm_tickets=4,
    ),
    Achievement(
        "three_personal_paints", "🎨", "Авторская серия",
        "Нанеси персональный покрас на три предмета 🖌",
        lambda p: p.get("personal_paints", 0) >= 3, rubies=2,
    ),
    Achievement(
        "painted_loadout", "🌈", "Снаряжение с подписью",
        "Собери пять предметов с персональными покрасами 🎒",
        lambda p: p.get("personal_paints", 0) >= 5, rubies=3, dungeon_tickets=2,
    ),
    Achievement(
        "floor_ten", "🕯", "Под землёй темнее",
        "Дойди до 10-го этажа подземелья 🔦",
        lambda p: p.get("deepest_floor", 1) >= 10, dungeon_tickets=2,
    ),
    Achievement(
        "floor_thirty", "🧭", "Глубже карты",
        "Дойди до 30-го этажа подземелья 🕳",
        lambda p: p.get("deepest_floor", 1) >= 30, rubies=2, dungeon_tickets=3,
    ),
    Achievement(
        "floor_sixty", "♾", "После самого дна",
        "Спустись до 60-го этажа, где боссы уже идут по кругу ⛏",
        lambda p: p.get("deepest_floor", 1) >= 60, rubies=3, dungeon_tickets=5,
    ),
    Achievement(
        "bosses_twenty_five", "🐉", "Коллекционер корон",
        "Победи 25 боссов подземелья 👑",
        lambda p: p.get("boss_wins", 0) >= 25, rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "mobs_five_hundred", "💀", "Коридоры опустели",
        "Победи 500 обычных врагов подземелья 🕯",
        lambda p: p.get("mob_wins", 0) >= 500, rubies=3, farm_tickets=3,
    ),
    Achievement(
        "phoenix_ten", "🔥", "Пепел знает тебя",
        "Победи Феникса 10 раз и открой автобой 🐦",
        lambda p: p.get("phoenix_wins", 0) >= 10, rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "level_ten", "⭐", "Характер проявился",
        "Доведи существо до 10-го уровня 🐾",
        lambda p: p.get("level", 1) >= 10, rubies=1,
    ),
    Achievement(
        "level_fifty", "🦅", "Пятидесятый уровень",
        "Доведи существо до 50-го уровня 🌠",
        lambda p: p.get("level", 1) >= 50, rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "power_two_five", "⚡", "Тяжёлая категория",
        "Подними силу существа до 2500 💪",
        lambda p: p.get("power", 0) >= 2500, rubies=2, farm_tickets=2,
    ),
    Achievement(
        "ten_weapons", "🗡", "Оружейная стойка",
        "Найди 10 разных видов оружия 🎁",
        lambda p: p.get("weapons_found", 0) >= 10, rubies=1,
    ),
    Achievement(
        "three_quarter_catalogue", "🗄", "Почти весь арсенал",
        "Собери три четверти каталога оружия 🔍",
        lambda p: p.get("weapons_total", 0) > 0
        and p.get("weapons_found", 0) >= (p.get("weapons_total", 0) * 3 + 3) // 4,
        rubies=3, dungeon_tickets=3,
    ),
    Achievement(
        "ten_scrolls", "📜", "Малая библиотека",
        "Открой 10 боевых свитков ✨",
        lambda p: p.get("scrolls_owned", 0) >= 10, rubies=1,
    ),
    Achievement(
        "thirty_scrolls", "📚", "Архив заклинаний",
        "Открой 30 боевых свитков 🔮",
        lambda p: p.get("scrolls_owned", 0) >= 30, rubies=2, dungeon_tickets=2,
    ),
    Achievement(
        "runes_twenty_five", "🪄", "Запас на чёрный день",
        "Скопи 25 рун для зачарования 💠",
        lambda p: p.get("runes", 0) >= 25, rubies=2, dungeon_tickets=2,
    ),
    Achievement(
        "millionaire", "💰", "Монетный водопад",
        "Заработай 1 000 000 монет за всё время 🪙",
        lambda p: p.get("gold_earned", 0) >= 1_000_000, rubies=3, farm_tickets=4,
    ),
    Achievement(
        "big_spender", "💸", "Четверть миллиона",
        "Потрать 250 000 монет на развитие и снаряжение 🔥",
        lambda p: p.get("gold_spent", 0) >= 250_000, rubies=3, farm_tickets=3,
    ),
    Achievement(
        "thousand_messages", "💬", "Всегда на связи",
        "Напиши 1000 сообщений в чате 🗨",
        lambda p: p.get("messages", 0) >= 1000, rubies=1, farm_tickets=2,
    ),
    Achievement(
        "ten_thousand_messages", "📣", "Голос сообщества",
        "Напиши 10 000 сообщений в чате 🎙",
        lambda p: p.get("messages", 0) >= 10_000, rubies=3, farm_tickets=4,
    ),
    Achievement(
        "thirty_days", "📅", "Месяц рядом",
        "Отметься в чате в 30 разных дней ☀️",
        lambda p: p.get("active_days", 0) >= 30, rubies=1, farm_tickets=2,
    ),
    Achievement(
        "year_in_chat", "🎂", "Год вместе",
        "Отметься в чате в 365 разных дней 🎉",
        lambda p: p.get("active_days", 0) >= 365, rubies=3, farm_tickets=5,
    ),
    # ---------------------------------------------------------------- скрытые
    # Hidden because naming them turns the joke into a checklist. Each one is a shape a
    # player falls into on purpose and then tells somebody about.
    Achievement(
        "all_in_luck", "🍀", "Ва-банк",
        "Удача выше 60 — и почти ничего больше 🎲",
        lambda p: _stat(p, "luck") >= 60 and _stat(p, "strength") <= 15
        and _stat(p, "magic") <= 15,
        rubies=3, hidden=True,
    ),
    Achievement(
        "naked_hero", "🩲", "Как есть",
        "Убить Феникса, надев не больше одной вещи 🪶",
        lambda p: p.get("phoenix_wins", 0) >= 1 and p.get("equipped_slots", 0) <= 1,
        rubies=3, dungeon_tickets=3, hidden=True,
    ),
    # Same counter, and here reading the wrong one was worse than a missing row: the
    # per-weapon ledger is empty for most veterans, so the joke about never having fought
    # was being handed to the people with the most fights in the chat.
    Achievement(
        "pacifist", "🕊", "Не боец",
        "Дойти до 10-го этажа, ни разу не победив на арене 🌿",
        lambda p: p.get("deepest_floor", 1) >= 10 and p.get("wins", 0) == 0
        and p.get("fights", 0) == 0,
        rubies=3, hidden=True,
    ),
    Achievement(
        "one_stat", "🗿", "Одно на всё",
        "Прокачать один стат выше 80, остальные оставить ниже 20 ⚖️",
        lambda p: any(
            _stat(p, key) >= 80 and all(
                _stat(p, other) <= 20
                for other in ("strength", "health", "agility", "luck", "magic")
                if other != key
            )
            for key in ("strength", "health", "agility", "luck", "magic")
        ),
        rubies=3, hidden=True,
    ),
)

_BY_CODE: Final = {row.code: row for row in ACHIEVEMENTS}


def _validate_catalogue() -> None:
    """Fail at import if a future edit breaks a promise the save file depends on."""
    codes = [row.code for row in ACHIEVEMENTS]
    assert len(set(codes)) == len(codes), "коды ачивок должны быть уникальны"
    # Codes are written into save files, so they can never be renamed later.
    assert all(code.isascii() and code.replace("_", "").isalnum() for code in codes)
    assert all(row.name and row.description for row in ACHIEVEMENTS)
    # The owner's ceiling: no single row is worth more than three diamonds.
    assert all(0 <= row.rubies <= 3 for row in ACHIEVEMENTS)
    assert all(
        row.rubies or row.farm_tickets or row.dungeon_tickets for row in ACHIEVEMENTS
    ), "ачивка без награды -- это строчка в списке, а не достижение"
    # Exactly one row is closed for ever. If a second is ever added, the backfill has to
    # be taught what evidence decides it -- see pets._legacy_evidence.
    assert sum(1 for row in ACHIEVEMENTS if row.legacy) == 1


_validate_catalogue()
