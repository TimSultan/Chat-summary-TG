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


# --- Напутствие на день -------------------------------------------------------------
#
# Picked by date rather than at random so that everybody in the chat sees the SAME line
# on the same morning -- it is a shared greeting, not a personal fortune -- and so a
# re-send after a restart cannot produce a different one. 120 entries means no repeat for
# four months. Deliberately emoji-free: the surrounding post already carries them, and
# these read as advice rather than decoration.
DAILY_ADVICE = (
    # --- покраска ---
    "Начни с самого страшного участка. Дальше будет только легче.",
    "Разбавляй краску сильнее, чем кажется нужным. Два тонких слоя всегда лучше одного плотного.",
    "Если цвет не ложится — дело почти всегда в грунте, а не в краске.",
    "Смывка прощает многое, но не спасает кривой базовый слой. Не торопись под неё.",
    "Не бойся переделать участок. Краска — единственный материал, который не заканчивается.",
    "Глаза и лицо решают. Если они читаются с расстояния вытянутой руки — миниатюра удалась.",
    "Контраст важнее точности. Аккуратная, но плоская работа выглядит хуже смелой и грязноватой.",
    "Металлики любят чёрную подложку. Попробуй — разница заметна сразу.",
    "Оставь миниатюру на день и посмотри свежим взглядом. Половина «проблем» исчезнет сама.",
    "Подставка — часть работы. Пять минут на неё меняют впечатление сильнее часа на плаще.",
    "Кисть с потерянным кончиком не лечится терпением. Отложи её на сухую кисть и возьми новую.",
    "Сфотографируй работу. Камера показывает то, чего глаз уже не замечает.",
    # --- 3D-печать ---
    "Неудачная печать — это не потерянный день, а найденный параметр.",
    "Калибровка стола скучнее печати, но именно она решает, будет ли печать вообще.",
    "Поддержки проще расставить руками, чем потом отчищать следы автоматических.",
    "Смола не прощает спешки при отмывке. Лишние две минуты сейчас — целая деталь потом.",
    "Печатай тест-модель перед большой. Час проверки экономит десять часов переделки.",
    "Ориентация детали на столе важнее любых настроек качества.",
    "Храни профили печати с заметками. Через месяц ты не вспомнишь, почему тогда получилось.",
    "Слои видны только тебе. Остальные смотрят на силуэт и покрас.",
    "Не гонись за 0.05 мм. Разница в детализации часто меньше разницы во времени.",
    "Проветривай. Никакая модель не стоит головной боли к вечеру.",
    "Сломанная деталь — повод научиться штифтовать. Это умение пригодится ещё сто раз.",
    "Если принтер работает — не трогай его настройки. Серьёзно.",
    # --- творчество и процесс ---
    "Сделанное лучше идеального. Идеальное обычно остаётся в коробке.",
    "Начни с пятнадцати минут. Чаще всего они превращаются в два часа.",
    "У любой работы есть уродливая середина. Это не признак провала, а этап.",
    "Закончи одну вещь, прежде чем начинать три новых. Законченное придаёт сил.",
    "Ошибка, которую ты заметил, — это выросший вкус, а не упавшие руки.",
    "Твоя сотая работа будет лучше первой, только если ты сделаешь остальные девяносто восемь.",
    "Пробуй технику, которая тебя пугает, на модели, которую не жалко.",
    "Сравнивай себя с собой полгода назад, а не с чужой витриной.",
    "Плохой день за столом всё равно лучше, чем день без стола.",
    "Иногда лучшее решение — отложить кисть и разобрать рабочее место.",
    "Не жди вдохновения. Оно приходит к тем, кто уже сидит и работает.",
    "Ограничение — подарок. Три краски заставят придумать больше, чем тридцать.",
    # --- любопытство ---
    "Спроси, как это сделано. Люди любят рассказывать про свою работу.",
    "Посмотри, как красят в другом масштабе. Оттуда приходят самые неожиданные приёмы.",
    "Разбери чужую работу на составляющие: что именно тебе в ней нравится?",
    "Загляни в область, которая с миниатюрами не связана вообще. Половина идей приходит оттуда.",
    "Попробуй материал, которым никогда не работал. Даже если не понравится — узнаешь границу.",
    "Прочитай про то, как устроен цвет. Один вечер теории экономит годы наугад.",
    "Найди мастера, чей стиль тебе не близок, и пойми, почему он им нравится другим.",
    "Задай глупый вопрос. Обычно оказывается, что его хотели задать ещё пятеро.",
    "Изучи референс дольше, чем кажется нужным. Пять минут смотрения экономят час переделки.",
    "Попробуй объяснить кому-то свой процесс. Сам поймёшь его лучше.",
    # --- вдохновение ---
    "Вдохновение — это насмотренность. Сохраняй то, что цепляет, даже без повода.",
    "Заведи папку с чужими работами, к которым хочется вернуться.",
    "Хорошая музыка и два часа за столом лечат почти всё.",
    "Пересмотри свои старые работы. Ты удивишься, сколько уже пройдено.",
    "Возьми цветовую схему из фотографии заката, а не из гайда.",
    "Сходи на выставку, даже если она не про миниатюры.",
    "Кино, книга, прогулка — это тоже работа над миниатюрой, просто не за столом.",
    "Если всё надоело — покрась что-нибудь совершенно несерьёзное.",
    "Иногда достаточно сменить лампу, чтобы захотелось вернуться к столу.",
    "Держи под рукой модель «для удовольствия», без сроков и без ожиданий.",
    # --- вдохновлять других ---
    "Покажи не только результат, но и процесс. Именно он помогает другим.",
    "Похвали конкретно: «хорошо» забывается, «отличный переход на плаще» запоминается.",
    "Расскажи о своей неудаче. Это помогает сильнее, чем очередной идеальный кадр.",
    "Ответь новичку. Когда-то кто-то ответил тебе.",
    "Поделись приёмом, который считаешь очевидным. Для кого-то он станет открытием.",
    "Отметь чужой прогресс. Человек часто сам его не видит.",
    "Не обесценивай свою работу в подписи. Пусть люди решают сами.",
    "Твой уровень кому-то сейчас кажется недостижимым. Помни об этом, когда пишешь о себе.",
    # --- не перегружаться ---
    "Не покупай новую модель, пока не закончил предыдущую. Ну, хотя бы попробуй.",
    "Гора некрашеных коробок — это не долг. Ты никому ничего не должен.",
    "Разбей большую работу на этапы и празднуй каждый.",
    "Устал — остановись. Работа, сделанная через силу, потом переделывается.",
    "Один час в неделю стабильно лучше, чем восемь часов раз в месяц.",
    "Если проект встал — отложи его открыто, а не «на потом». Так легче вернуться.",
    "Не держи в голове десять идей. Запиши их и освободи место.",
    "Сроки, которые ты сам себе придумал, можно и передвинуть.",
    "Перфекционизм — это страх в костюме требовательности.",
    "Сделай перерыв раньше, чем заболит спина, а не после.",
    # --- общение онлайн ---
    "Напиши в чат, даже если кажется, что сказать нечего. Часто именно так и начинается разговор.",
    "Спорь о технике, а не о людях.",
    "Если работа зацепила — скажи об этом автору, а не только себе.",
    "Задать вопрос — не признак слабости, а самый быстрый путь.",
    "Прежде чем поправить кого-то, спроси, нужен ли совет.",
    "Онлайн легко забыть, что по ту сторону человек с таким же столом и такими же кистями.",
    "Иногда лучший ответ в чате — просто эмодзи-реакция.",
    # --- общение вживую ---
    "Договорись покрасить вместе. Вдвоём за столом идёт совсем иначе.",
    "Сходи на локальную игру или встречу, даже если ты не играешь.",
    "Покажи свою работу вживую. Экран съедает половину.",
    "Позови кого-то к себе разобрать принтер. Заодно и разберёшь.",
    "Поговори с человеком, который в хобби дольше тебя. И с тем, кто меньше.",
    "Живая встреча раз в месяц держит интерес лучше, чем лента каждый день.",
    "Подари кому-нибудь свою миниатюру. Ощущение стоит того.",
    # --- природа и отдых ---
    "Выйди на улицу на двадцать минут. Глазам нужен горизонт, а не подставка.",
    "Посмотри, как свет ложится на кору дерева. Это лучший урок по текстурам.",
    "Настоящая ржавчина и настоящая грязь выглядят не так, как в гайдах. Сходи посмотри.",
    "Прогулка без телефона — это не потерянное время, а перезагрузка.",
    "Цвета в природе почти никогда не чистые. Подмешивай.",
    "Проведи вечер без экранов. Руки сами потянутся к чему-нибудь интересному.",
    "Сон чинит больше проблем с покрасом, чем ещё один час за столом.",
    "Посиди у воды. Это работает, и никто не знает почему.",
    # --- быт и рабочее место ---
    "Хороший свет важнее хорошей кисти.",
    "Разложи краски так, чтобы не искать. Пять минут порядка — час работы.",
    "Замени воду в стакане. Да, прямо сейчас.",
    "Держи мусорку рядом со столом. Мелочь, а меняет всё.",
    "Подписывай смеси, которые собираешься повторять.",
    "Стул важнее стола. Спина скажет спасибо через год.",
    "Убери со стола всё, что не нужно для текущего этапа.",
    "Заведи коробку «на потом» для деталей, которые жалко выкинуть.",
    # --- общее ---
    "Сделай сегодня одну маленькую вещь. Этого достаточно.",
    "Прогресс редко виден в моменте. Он виден в архиве фотографий.",
    "Никто не смотрит на твою работу так придирчиво, как ты.",
    "Умение остановиться — такой же навык, как умение начать.",
    "Если долго не получается — меняй подход, а не старайся сильнее.",
    "Спроси себя, зачем ты это делаешь. Если ответ «нравится» — этого хватит.",
    "Хобби не обязано становиться профессией, чтобы быть ценным.",
    "Дай себе право делать плохо. Иначе не начнёшь вообще.",
    "Терпение — это не ждать, а продолжать, пока ждёшь.",
    "Скучный этап тоже часть работы. Не пропускай грунт.",
    "Записывай, что получилось. Память сохраняет только неудачи.",
    "Пробуй новое в конце сессии, когда уже нечего терять.",
    "Не сравнивай своё начало с чужой серединой.",
    "Лучшее время начать было вчера. Второе лучшее — сегодня.",
    "Спокойные руки приходят после спокойной головы, а не наоборот.",
    "Ты уже дальше, чем был. Этого достаточно на сегодня.",
)


def advice_for(day: date) -> str:
    """The same line for everybody on a given day, rotating without repeats for as many
    days as there are entries."""
    return DAILY_ADVICE[day.toordinal() % len(DAILY_ADVICE)]


def format_planting_message() -> str:
    """The one-off post that opens the whole thing, sent instead of the first morning
    digest. Deliberately carries no numbers: on the day it goes out the tree is a seed,
    and a height of "0 мм" would undercut the moment."""
    return "\n".join([
        "🌱 <b>Сегодня мы все вместе посадили семечко.</b>",
        "",
        "Из него вырастет могучее дерево ЕПХ — одно на весь чат, общее.",
        "Его питает всё, что вы здесь делаете: каждое сообщение, каждый ответ,",
        "каждая выложенная работа. Чем живее чат — тем выше оно тянется.",
        "",
        "Здесь не с кем соревноваться. Дерево одно, и чужой вклад — это и ваш рост тоже.",
        "Впереди тринадцать стадий и три года пути до Легендарного Древа.",
        "",
        "Каждое утро в 10:00 я буду рассказывать, на сколько оно подросло за сутки",
        "и кто вложил больше всех.",
        "",
        "🌳 <b>Давайте вырастим его вместе — покажите ему, на что мы способны.</b>",
        "Всё начинается сегодня.",
    ])


def _contributor_lines(contributors: list) -> list:
    """The shared "who moved it" block, so /tree and the morning post cannot drift apart."""
    shown = [item for item in contributors if item[2] > 0][:TOP_CONTRIBUTORS_SHOWN]
    lines = []
    for display_name, username, xp in shown:
        who = f"@{username.lstrip('@')}" if username else escape(display_name)
        lines.append(f"{who} — {xp} XP")
    return lines


def format_tree_status(total_xp: int, yesterday_xp: int = 0, contributors: list | None = None) -> str:
    """The /tree reply: total height, yesterday's growth, and who drove it."""
    _, emoji, name = tree_stage(total_xp)
    grown_mm = tree_height_mm(total_xp) - tree_height_mm(max(0, total_xp - yesterday_xp))
    lines = [
        f"🌳 Наше дерево ЕПХ выросло на {format_length(tree_height_mm(total_xp))}.",
        f"Сейчас это {emoji} <b>{escape(name)}</b>.",
    ]
    upcoming = next_stage(total_xp)
    if upcoming:
        following, remaining = upcoming
        lines.append(f"До стадии «{escape(following)}» — {format_length(remaining)}.")

    named = _contributor_lines(contributors or [])
    if named:
        lines.append("")
        lines.append(f"За вчера подросло на {format_growth(grown_mm)}. Больше всех вложили:")
        lines.extend(named)

    lines.append("")
    lines.append("Ваша активность и покрасы помогают ему расти.")
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
    number, emoji, name = tree_stage(total_xp)

    lines = [
        f"🌳 <b>{MORNING_GREETING}</b>",
        "",
        f"Сегодня наше дерево выросло на {format_growth(grown_mm)}.",
        f"Сейчас это {emoji} <b>{escape(name)}</b> — {format_length(tree_height_mm(total_xp))}.",
    ]
    upcoming = next_stage(total_xp)
    if upcoming:
        following, remaining = upcoming
        lines.append(f"До стадии «{escape(following)}» — {format_length(remaining)}.")

    shown = [item for item in contributors if item[2] > 0][:TOP_CONTRIBUTORS_SHOWN]
    if shown:
        lines.append("")
        lines.append("Самый большой вклад вчера внесли:")
        for display_name, username, xp in shown:
            who = f"@{username.lstrip('@')}" if username else escape(display_name)
            lines.append(f"{who} — {xp} XP")

    lines.append("")
    lines.append("<b>Напутствие на день</b>")
    lines.append(escape(advice_for(day)))
    return "\n".join(lines)
