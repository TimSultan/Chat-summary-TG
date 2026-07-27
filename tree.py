"""The chat-wide progression: one ЕПХ tree that everybody's XP grows together.

Unlike every other ladder in this project, this one is not per-member. All XP earned in
the chat pools into a single height, so a quiet member's few points still visibly move
the same tree as the loudest member's -- which is the point: it is the one score nobody
competes on.

Calibrated against the chat's own measured output rather than guessed: over a 34-day
window the whole chat produced ~3,600 XP/day, so at TREE_XP_PER_MM the final stage lands
almost exactly three years out, and an ordinary day moves the tree ~18 mm -- small enough
that a quiet day reads differently from a busy one.
"""

from datetime import date
from html import escape

# 200 XP per millimetre, 20 m at the top. At the chat's measured ~3,600 XP/day that is
# ~18 mm a day and ~3.0 years to the final stage.
TREE_XP_PER_MM = 200
TREE_MAX_HEIGHT_MM = 20_000
# Temporarily hide and ignore the on-demand /tree command without disabling planting,
# previews or the automatic 10:00 posts. Both bot listeners read this same switch.
TREE_COMMAND_ENABLED = False

# (minimum height in mm, emoji, name). Ordered lowest-first; stage 1 is the seed everyone
# starts from. Thresholds are set in HEIGHT rather than XP so the names line up with a
# tree somebody can picture -- a 2 m "деревце", a 20 m giant.
TREE_STAGES = (
    (0, "🌰", "Семечко"),
    (50, "🌱", "Росток"),
    (200, "🌿", "Проросток"),
    (500, "🪴", "Саженец"),
    (1_000, "🌾", "Молодая поросль"),
    (2_000, "🌲", "Деревце"),
    (3_500, "🌳", "Молодое дерево"),
    (5_500, "🍃", "Крепкое дерево"),
    (8_000, "🌳", "Раскидистое дерево"),
    (11_000, "🦉", "Дерево с дуплом"),
    (14_000, "🌸", "Цветущий великан"),
    (17_000, "🏛️", "Древо-исполин"),
    # 19.5 m rather than the full 20: at the measured rate this lands the final stage a
    # touch under three years, so the tree reaches its name on time and then keeps
    # inching towards the cap instead of the name arriving three months late.
    (19_500, "👑", "Легендарное Древо ЕПХ"),
)

MORNING_GREETING = "Доброе утро, ЕПХ-чане!"
# How many of yesterday's top contributors the morning post names.
TOP_CONTRIBUTORS_SHOWN = 3

# Members take part in the planting by tapping a button under the ceremony post, not by
# reacting to it. A reaction was the original plan and had to go: Telegram only accepts
# its own fixed quick-reaction set (core.telegram.org/api/reactions), which contains no
# 🌳, 🌱 or 🌿 at all -- 🎄, a new year tree, is the only tree in it. A button carries any
# emoji, reports exactly who pressed it, and needs no Telethon session to read.
#
# 🪏 (shovel) is Unicode 16, new enough that a very old client may draw it as a blank box;
# ⛏ is the safe substitute if that ever shows up in the chat.
SEED_BUTTON_TEXT = "🌰🪏🌳 Посадить семечко"
# Answered as a toast on the presser's own screen -- nothing is posted to the chat, so
# 190 members tapping a button cannot turn into 190 messages.
SEED_BUTTON_ACK = "Готово, ты участвуешь в посадке. В 10:00 назову всех."
SEED_BUTTON_ALREADY = "Ты уже участвуешь в посадке."
SEED_BUTTON_TEST_ACK = "Это только предпросмотр — нажатие ни на что не влияет."


def tree_height_mm(total_xp: int) -> int:
    """Height for the chat's pooled all-time XP, capped at the final stage. The cap
    matters: XP keeps accruing forever, and without it the tree would silently grow past
    its own last name into a number nobody has a word for."""
    return min(TREE_MAX_HEIGHT_MM, max(0, int(total_xp)) // TREE_XP_PER_MM)


def tree_stage(total_xp: int) -> tuple[int, str, str]:
    """(stage number starting at 1, emoji, name) for the pooled XP."""
    height = tree_height_mm(total_xp)
    index = 0
    for position, (minimum, _, _) in enumerate(TREE_STAGES):
        if height >= minimum:
            index = position
    _, emoji, name = TREE_STAGES[index]
    return index + 1, emoji, name


def next_stage(total_xp: int) -> tuple[str, int] | None:
    """(name, millimetres still to go) for the next stage, or None at the top."""
    height = tree_height_mm(total_xp)
    for minimum, _, name in TREE_STAGES:
        if height < minimum:
            return name, minimum - height
    return None


def format_length(mm: int) -> str:
    """Millimetres up to a centimetre, centimetres up to a metre, then metres.

    The unit follows the number rather than being fixed, because the same function
    renders both a day's growth (usually tens of mm) and the tree's total height
    (eventually tens of metres), and "18000 мм" helps nobody picture a tree."""
    mm = max(0, int(mm))
    if mm < 10:
        return f"{mm} мм"
    if mm < 1_000:
        return f"{mm / 10:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " см"
    return f"{mm / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " м"


def format_growth(mm: int) -> str:
    """A day's growth. Kept in millimetres far longer than format_length would, because
    "17 мм" reads as progress where "1,7 см" reads as nothing much."""
    mm = max(0, int(mm))
    if mm < 100:
        return f"{mm} мм"
    return format_length(mm)


# --- Идея на день -------------------------------------------------------------------
#
# Picked by date rather than at random so that everybody in the chat sees the SAME line
# on the same morning -- it is a shared greeting, not a personal fortune -- and so a
# re-send after a restart cannot produce a different one. 120 entries means no repeat for
# four months. Deliberately emoji-free: the surrounding post already carries them, and
# these read as advice rather than decoration.
DAILY_ADVICE = (
    # --- покраска ---
    "Выбери участок, который сильнее всего изменит миниатюру, и начни с него.",
    "Разбавляй краску сильнее, чем кажется нужным. Два тонких слоя всегда лучше одного плотного.",
    "Когда цвет ложится неровно, проверь грунт — хорошая основа заметно облегчает покраску.",
    "Ровный базовый слой раскрывает смывку. Дай ему немного времени и внимания.",
    "Любой участок можно уточнить или обновить — краска позволяет спокойно пробовать ещё раз.",
    "Глаза и лицо решают. Если они читаются с расстояния вытянутой руки — миниатюра удалась.",
    "Контраст помогает форме читаться. Смелые свет и тень часто оживляют работу сильнее идеальной аккуратности.",
    "Металлики любят чёрную подложку. Попробуй — разница заметна сразу.",
    "Оставь миниатюру на день и посмотри свежим взглядом. Новые решения станут заметнее.",
    "Подставка — часть работы. Пять минут на неё меняют впечатление сильнее часа на плаще.",
    "Кисть без тонкого кончика отлично подойдёт для сухой кисти, а для деталей выбери острую.",
    "Сфотографируй работу. Камера показывает то, чего глаз уже не замечает.",
    # --- 3D-печать ---
    "Каждая тестовая печать уточняет настройки и приближает стабильный результат.",
    "Хорошая калибровка стола — короткий путь к ровной и предсказуемой печати.",
    "Поддержки проще расставить руками, чем потом отчищать следы автоматических.",
    "Тщательная отмывка смолы сохраняет мелкие детали. Пара дополнительных минут здесь окупается.",
    "Печатай тест-модель перед большой. Час проверки экономит десять часов переделки.",
    "Ориентация детали на столе важнее любых настроек качества.",
    "Храни профили печати с заметками. Они помогут быстро повторить удачный результат.",
    "Сначала зритель видит силуэт и покрас. Небольшие линии слоёв обычно остаются фоном.",
    "Выбирай высоту слоя под задачу: часто 0,1 мм сохраняет детали и заметно экономит время.",
    "Проветривай рабочее место. Свежий воздух помогает дольше сохранять внимание.",
    "Штифтование помогает надёжно собирать тонкие и крупные детали. Этот приём пригодится ещё много раз.",
    "Стабильный профиль принтера стоит сохранить прежде, чем экспериментировать дальше.",
    # --- творчество и процесс ---
    "Готовая работа приносит больше радости, чем бесконечная погоня за идеалом.",
    "Начни с пятнадцати минут. Чаще всего они превращаются в два часа.",
    "У каждой работы есть переходный этап, когда замысел только начинает проявляться. Продолжай.",
    "Закончи одну вещь, прежде чем начинать три новых. Законченное придаёт сил.",
    "Замеченные детали показывают, как вырос твой взгляд. Это уже прогресс.",
    "Каждая следующая работа добавляет опыт. Сотая вырастает из всех предыдущих.",
    "Осваивай новую технику на простой модели: так легче дать себе свободу эксперимента.",
    "Сравнивай себя с собой полгода назад, а не с чужой витриной.",
    "Даже короткая спокойная сессия за столом поддерживает навык и творческий ритм.",
    "Иногда лучшее решение — отложить кисть и разобрать рабочее место.",
    "Вдохновение часто приходит уже в процессе. Начни с одного простого действия.",
    "Ограничение — подарок. Три краски заставят придумать больше, чем тридцать.",
    # --- любопытство ---
    "Спроси, как это сделано. Люди любят рассказывать про свою работу.",
    "Посмотри, как красят в другом масштабе. Оттуда приходят самые неожиданные приёмы.",
    "Разбери чужую работу на составляющие: что именно тебе в ней нравится?",
    "Загляни в область, которая с миниатюрами не связана вообще. Половина идей приходит оттуда.",
    "Новый материал быстро показывает свои возможности и может подсказать неожиданный приём.",
    "Прочитай про то, как устроен цвет. Один вечер теории экономит годы наугад.",
    "Найди мастера, чей стиль отличается от твоего, и попробуй увидеть, что делает его выразительным.",
    "Задай вопрос, который кажется слишком простым. Ответ наверняка пригодится не только тебе.",
    "Изучи референс дольше, чем кажется нужным. Пять минут смотрения экономят час переделки.",
    "Попробуй объяснить кому-то свой процесс. Сам поймёшь его лучше.",
    # --- вдохновение ---
    "Вдохновение — это насмотренность. Сохраняй то, что цепляет, даже без повода.",
    "Заведи папку с чужими работами, к которым хочется вернуться.",
    "Хорошая музыка и время за столом помогают переключиться и вернуть удовольствие от процесса.",
    "Пересмотри свои старые работы. Ты удивишься, сколько уже пройдено.",
    "Возьми цветовую схему из фотографии заката, а не из гайда.",
    "Сходи на выставку, даже если она не про миниатюры.",
    "Кино, книга, прогулка — это тоже работа над миниатюрой, просто не за столом.",
    "Выбери что-нибудь совершенно несерьёзное и покрась просто ради удовольствия.",
    "Иногда достаточно сменить лампу, чтобы захотелось вернуться к столу.",
    "Держи под рукой модель «для удовольствия», без сроков и без ожиданий.",
    # --- вдохновлять других ---
    "Покажи не только результат, но и процесс. Именно он помогает другим.",
    "Похвали конкретно: «хорошо» забывается, «отличный переход на плаще» запоминается.",
    "Покажи момент, в котором искал решение. Такой процесс часто помогает другим сильнее идеального кадра.",
    "Ответь новичку. Когда-то кто-то ответил тебе.",
    "Поделись приёмом, который считаешь очевидным. Для кого-то он станет открытием.",
    "Отметь чужой прогресс. Человек часто сам его не видит.",
    "Рассказывай о своей работе с уважением к вложенному времени. Зритель сам увидит детали.",
    "Твой сегодняшний уровень может вдохновить того, кто только начинает. Помни об этом, когда пишешь о себе.",
    # --- не перегружаться ---
    "Перед новой моделью выбери одну из уже начатых и подари ей немного внимания.",
    "Коллекция некрашеных коробок — это запас возможностей. Выбирай следующую работу по настроению.",
    "Разбей большую работу на этапы и празднуй каждый.",
    "Замечай усталость и вовремя делай паузу. Отдых сохраняет удовольствие и аккуратность.",
    "Один час в неделю стабильно лучше, чем восемь часов раз в месяц.",
    "Если проект потерял ритм, зафиксируй текущий этап и спокойно переключись. Так к нему легче вернуться.",
    "Запиши новые идеи, чтобы освободить внимание для текущей работы.",
    "Сроки, которые ты сам себе придумал, можно и передвинуть.",
    "Смени стремление к идеалу на любопытство: один эксперимент часто двигает работу вперёд.",
    "Делай короткие перерывы заранее — спина и внимание скажут спасибо.",
    # --- общение онлайн ---
    "Иногда достаточно одной простой мысли в чате, чтобы начался хороший разговор.",
    "Обсуждай приёмы и решения — так спор приносит новые идеи.",
    "Если работа зацепила — скажи об этом автору, а не только себе.",
    "Вопрос — самый короткий путь к новому навыку.",
    "Прежде чем поправить кого-то, спроси, нужен ли совет.",
    "По ту сторону экрана — человек с таким же столом, кистями и любовью к хобби.",
    "Иногда лучший ответ в чате — просто эмодзи-реакция.",
    # --- общение вживую ---
    "Договорись покрасить вместе. Вдвоём за столом идёт совсем иначе.",
    "Сходи на локальную игру или встречу, даже если ты не играешь.",
    "Покажи работу вживую: объём, цвет и фактура раскрываются совсем иначе.",
    "Позови кого-то вместе разобраться с принтером. В компании решение часто находится быстрее.",
    "Поговори с тем, кто в хобби дольше тебя, и с тем, кто только начинает.",
    "Живая встреча раз в месяц держит интерес лучше, чем лента каждый день.",
    "Подари кому-нибудь свою миниатюру. Ощущение стоит того.",
    # --- природа и отдых ---
    "Двадцать минут на улице дают глазам дальний план и возвращают внимание.",
    "Посмотри, как свет ложится на кору дерева. Это лучший урок по текстурам.",
    "Посмотри вживую на кору, камень и старый металл — природа щедра на готовые референсы.",
    "Прогулка без телефона даёт голове время переключиться и собрать новые идеи.",
    "Природные цвета состоят из множества оттенков. Попробуй добавить их в смесь.",
    "Проведи вечер без экранов. Руки сами потянутся к чему-нибудь интересному.",
    "Хороший сон помогает вниманию и точности сильнее ещё одного позднего часа за столом.",
    "Посиди у воды. Спокойный ритм помогает вниманию перезагрузиться.",
    # --- быт и рабочее место ---
    "Хороший свет важнее хорошей кисти.",
    "Разложи краски так, чтобы не искать. Пять минут порядка — час работы.",
    "Замени воду в стакане. Да, прямо сейчас.",
    "Держи мусорку рядом со столом. Мелочь, а меняет всё.",
    "Подписывай смеси, которые собираешься повторять.",
    "Стул важнее стола. Спина скажет спасибо через год.",
    "Оставь на столе только то, что нужно для текущего этапа.",
    "Заведи коробку для интересных деталей, которые пригодятся в будущих проектах.",
    # --- общее ---
    "Сделай сегодня одну маленькую вещь. Этого достаточно.",
    "Прогресс лучше всего виден в архиве фотографий. Иногда заглядывай назад.",
    "Ты знаешь каждую деталь своей работы, поэтому замечаешь больше остальных. Не забывай видеть и целое.",
    "Умение остановиться — такой же навык, как умение начать.",
    "Если подход не даёт результата, попробуй другой инструмент, порядок или референс.",
    "Спроси себя, зачем ты это делаешь. Если ответ «нравится» — этого хватит.",
    "Хобби не обязано становиться профессией, чтобы быть ценным.",
    "Разреши себе сначала сделать черновой вариант — качество приходит вместе с практикой.",
    "Терпение — это не ждать, а продолжать, пока ждёшь.",
    "Даже спокойный технический этап двигает работу вперёд. Хороший грунт уже часть результата.",
    "Записывай удачные решения — так их легко повторить и развить.",
    "Оставь несколько минут в конце сессии для свободного эксперимента.",
    "Сравни своё начало с тем, что умеешь сейчас: так лучше всего виден рост.",
    "Сегодня — подходящий день, чтобы начать с одного небольшого шага.",
    "Спокойная голова помогает рукам работать точнее.",
    "Ты уже дальше, чем был. Этого достаточно на сегодня.",
)


def advice_for(day: date) -> str:
    """The same line for everybody on a given day, rotating without repeats for as many
    days as there are entries."""
    return DAILY_ADVICE[day.toordinal() % len(DAILY_ADVICE)]


def _planting_story_lines() -> list[str]:
    """The shared story behind the old planting post and the 10:00 roll call."""
    return [
        "🌱 <b>Сегодня мы все вместе посадили семечко.</b>",
        "",
        "Из него вырастет могучее дерево ЕПХ — одно на весь чат, общее.",
        "Его питает всё, что вы здесь делаете: каждое сообщение, каждый ответ,",
        "каждая показанная работа. Чем живее чат — тем выше оно тянется.",
        "",
        "Это общий рост: дерево одно, и вклад каждого помогает всем.",
        "Каким оно станет и как высоко вырастет, зависит от нас.",
        "",
        "Каждое утро в 10:00 я буду рассказывать, насколько оно подросло за сутки",
        "и кто особенно помог ему вырасти.",
        "",
        "🌳 <b>Давайте растить его вместе и радоваться каждому новому шагу.</b>",
        "Всё начинается сегодня.",
    ]


def format_planting_message() -> str:
    """The one-off post that opens the whole thing, sent instead of the first morning
    digest.

    Deliberately carries no numbers at all: on the day it goes out the tree is a seed,
    and a height of "0 мм" would undercut the moment. It also never names how many
    stages there are or how long the whole thing takes -- same rule /stat already
    follows by not printing the next level's threshold. Knowing it is "thirteen stages
    and three years" turns an open-ended thing the chat is growing into a progress bar
    with a visible end, and the mystery is doing more work here than the number would.
    """
    return "\n".join(_planting_story_lines())


# --- Посадка семечка ------------------------------------------------------------------
#
# The opening ceremony, in three posts: an admin invites the chat, members respond to that
# invitation, and the next 10:00 post names everyone who did and puts the tree in the
# ground. Nothing here pins or unpins anything -- the admin pins the invitation by hand,
# which is why the bot never needs can_pin_messages in the chat at all.

# The roll call carries this line instead of drawing from DAILY_ADVICE. On the one day the
# chat plants a tree, a rotation line about cleaning your brushes would be a non sequitur;
# this one addresses each participant's own growth, so it only ever appears once.
PLANTING_ADVICE = (
    "Всё, что вы сегодня создаёте и чему учитесь, становится частью вашего мастерства. "
    "Каждый новый приём, смелая идея, заданный вопрос и завершённая работа помогают вам расти. "
    "День за днём будет прибавляться опыт, появится больше уверенности, "
    "а со временем вы увидите результаты, которыми сможете гордиться."
)


def _planter_names(planters: list) -> list:
    """[(display_name, username)] -> renderable names, @handle where there is one."""
    return [
        f"@{username.lstrip('@')}" if username else escape(display_name)
        for display_name, username in planters
    ]


def format_seed_ceremony_message(same_day: bool = False) -> str:
    """The invitation an admin posts to open the planting.

    Carries no height, no stage and no end goal -- the same rule format_planting_message
    follows, for the same reason: on this day there is nothing to report yet, and a
    "0 мм" would undercut the moment.

    `same_day` is True when the roll call lands later today (the ceremony was opened
    before 10:00) rather than tomorrow morning.
    """
    when = "Сегодня" if same_day else "Завтра"
    return "\n".join([
        "🌰 <b>Сегодня мы начинаем общую посадку семечка.</b>",
        "",
        "Из него вырастет дерево ЕПХ — одно на весь чат, общее.",
        "Его питает всё, что вы здесь делаете: каждое сообщение, каждый ответ,",
        "каждая показанная работа. Чем живее чат — тем выше оно тянется.",
        "",
        "Это общий рост: вклад каждого помогает дереву и становится частью общего результата.",
        "Каким оно станет и как высоко вырастет, зависит от всех нас.",
        "",
        "🪏 <b>Нажмите кнопку под этим сообщением</b>, чтобы присоединиться к посадке.",
        f"{when} в 10:00 я назову всех участников, и мы вместе посадим семечко.",
        "За участие в посадке вы получите уникальный значок.",
    ])


def format_seed_reminder_message(planter_count: int) -> str:
    """A count-only follow-up for an open planting ceremony."""
    return "\n".join([
        "🌰 <b>Напоминание о посадке дерева ЕПХ</b>",
        "",
        "Помогите нам посадить дерево ЕПХ.",
        f"Уже участвуют: <b>{max(0, int(planter_count))}</b>.",
        "",
        "Нажмите кнопку под этим сообщением, чтобы посадить семечко.",
        "За участие вы получите уникальный значок.",
    ])


def seed_keyboard(callback_data: str) -> dict:
    """The one button under the ceremony post. The caller owns the callback payload, so
    the same layout serves both the real planting and the /preview test post."""
    return {"inline_keyboard": [[{"text": SEED_BUTTON_TEXT, "callback_data": callback_data}]]}


def format_planting_roll_call(planters: list) -> str:
    """The 10:00 post that closes the ceremony and plants the tree.

    `planters` is [(display_name, username)] for everyone who pressed the invitation button,
    in whatever order the caller collected them. Like the invitation it prints no numbers
    about the tree itself: it goes into the ground as this is posted.
    """
    names = _planter_names(planters)
    return "\n".join([
        *_planting_story_lines(),
        "",
        "<b>Семечко посадили:</b>",
        ", ".join(names),
        "",
        "<b>Напутствие</b>",
        escape(PLANTING_ADVICE),
    ])


def format_nobody_planted_message() -> str:
    """10:00 with no reactions on the invitation. The tree is deliberately NOT planted:
    opening the whole thing on an empty roll call would be worse than waiting a day."""
    return "\n".join([
        "🌰 <b>Семечко ждёт своих участников.</b>",
        "",
        "Посадка остаётся открытой. Нажмите кнопку под приглашением,",
        "и завтра в 10:00 я назову всех, кто присоединился.",
    ])


def format_awaiting_planting_status() -> str:
    """Preview status between the invitation and roll call, before the tree exists."""
    return "\n".join([
        "🌰 <b>Посадка открыта.</b>",
        "",
        "Нажмите кнопку под приглашением, чтобы присоединиться.",
        "В 10:00 я назову всех участников, и семечко начнёт расти.",
    ])


def _contributor_lines(contributors: list) -> list:
    """Shared contributor block for the status preview and morning post."""
    shown = [item for item in contributors if item[2] > 0][:TOP_CONTRIBUTORS_SHOWN]
    lines = []
    for display_name, username, xp in shown:
        who = f"@{username.lstrip('@')}" if username else escape(display_name)
        lines.append(f"{who} — {xp} XP")
    return lines


def format_tree_status(total_xp: int, yesterday_xp: int = 0, contributors: list | None = None) -> str:
    """Status preview: total height, yesterday's growth, and who drove it."""
    _, emoji, name = tree_stage(total_xp)
    grown_mm = tree_height_mm(total_xp) - tree_height_mm(max(0, total_xp - yesterday_xp))
    lines = [
        f"🌳 Высота нашего дерева ЕПХ — {format_length(tree_height_mm(total_xp))}.",
        f"Сейчас оно на стадии {emoji} <b>{escape(name)}</b>.",
    ]
    upcoming = next_stage(total_xp)
    if upcoming:
        following, remaining = upcoming
        lines.append(f"До следующей стадии «{escape(following)}» — {format_length(remaining)}.")

    named = _contributor_lines(contributors or [])
    if named:
        lines.append("")
        lines.append(f"Вчера дерево подросло на {format_growth(grown_mm)}.")
        lines.append("Особенно помогли дереву вырасти:")
        lines.extend(named)

    lines.append("")
    lines.append("Каждое сообщение, ответ и показанная работа помогают ему расти.")
    return "\n".join(lines)


def format_morning_digest(
    total_xp: int,
    yesterday_xp: int,
    contributors: list,
    day: date,
) -> str:
    """The 10:00 post.

    `contributors` is [(display_name, username, xp)] for yesterday, already sorted
    highest-first; the caller trims nothing, this shows TOP_CONTRIBUTORS_SHOWN of them.
    Rendered for Telegram's HTML mode, so every name is escaped.
    """
    grown_mm = tree_height_mm(total_xp) - tree_height_mm(max(0, total_xp - yesterday_xp))
    _, emoji, name = tree_stage(total_xp)

    lines = [
        f"🌳 <b>{MORNING_GREETING}</b>",
        "",
    ]
    if grown_mm:
        lines.append(f"Вчера наше дерево подросло на {format_growth(grown_mm)}.")
    else:
        lines.append("Вчера высота дерева не изменилась — оно набирается сил.")
    lines.append(
        f"Сейчас оно на стадии {emoji} <b>{escape(name)}</b>. "
        f"Высота — {format_length(tree_height_mm(total_xp))}."
    )
    upcoming = next_stage(total_xp)
    if upcoming:
        following, remaining = upcoming
        lines.append(f"До следующей стадии «{escape(following)}» — {format_length(remaining)}.")

    shown = [item for item in contributors if item[2] > 0][:TOP_CONTRIBUTORS_SHOWN]
    if shown:
        lines.append("")
        lines.append("Особенно помогли дереву вырасти:")
        for display_name, username, xp in shown:
            who = f"@{username.lstrip('@')}" if username else escape(display_name)
            lines.append(f"{who} — {xp} XP")

    lines.append("")
    lines.append("<b>Идея на день</b>")
    lines.append(escape(advice_for(day)))
    return "\n".join(lines)
