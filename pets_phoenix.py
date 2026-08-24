"""Феникс пепельных залов — the first boss a player LEARNS rather than out-levels.

Every other fight in this game is one call to `pets_combat.simulate`: both fighters are
decided in advance and the transcript is read afterwards. That cannot express a boss whose
outcome depends on what the player pressed, so the Phoenix gets its own turn engine.

What this module is
-------------------
A pure state machine. It never touches the store, never rolls loot, never reads a pet
record. It is handed a HERO PROFILE -- the finished numbers from the ordinary engine
(`pets.phoenix_hero_profile`, which reads `pets_combat.derive`) -- and returns a JSON-safe
state dict after every action. Persisting that dict, drawing it and paying out the reward
belong to `pets.py` and the two clients.

Being pure is what makes the fight testable at all: a balance run is a loop over
`take()`, not a fixture with a database behind it.

The shape of the fight
----------------------
The boss telegraphs, the player answers, the answer is graded, and the grade decides both
what the player takes and whether the boss opens up:

    INTRO -> TELEGRAPH -> (player action) -> RESOLVE -> VULNERABLE? -> TELEGRAPH -> ...
                                                     -> PHASE_1_DEATH -> REBIRTH -> PHASE 2
                                                     -> VICTORY / DEFEAT

`TELEGRAPH` is the whole design. It describes what the Phoenix is DOING -- wings, ash,
the colour of the flame -- and never what to press. A player who has not seen the move
takes the hit; a player who has seen it twice knows the answer. That is the difference
between "I learned to fight the Phoenix" and "I finally have the stats".

Two damage channels, deliberately separate
------------------------------------------
* **Ordinary damage** runs through the real numbers: the boss's swing against the hero's
  armour and guard. Better gear really does mean less of it.
* **Mistake damage** is a share of the hero's MAXIMUM health and mostly ignores armour.
  This is what stops a fully-geared pet from pressing ⚔️ every turn and winning on stats
  alone. One mistake is survivable by design; three in a row are not.

Nothing here scales to the hero. The Phoenix has the stats of its floor, so a strong pet
genuinely does finish it faster -- but a per-window damage cap (`PHASE_DAMAGE_CAP_SHARE`)
means it always takes several well-played windows rather than one enormous hit.
"""

from __future__ import annotations

import copy
import random
from typing import Final

# --------------------------------------------------------------------------- states
INTRO: Final = "intro"
TELEGRAPH: Final = "telegraph"
VULNERABLE: Final = "vulnerable"
REBIRTH: Final = "rebirth"
VICTORY: Final = "victory"
DEFEAT: Final = "defeat"

STATES: Final = (INTRO, TELEGRAPH, VULNERABLE, REBIRTH, VICTORY, DEFEAT)

# --------------------------------------------------------------------------- actions
ATTACK: Final = "attack"
SHIELD: Final = "shield"
DODGE: Final = "dodge"
MAGIC: Final = "magic"
LEFT: Final = "left"
RIGHT: Final = "right"
# The twin illusion is the one move that needs a target rather than a manner.
ATTACK_LEFT: Final = "attack_left"
ATTACK_RIGHT: Final = "attack_right"

ACTIONS: Final = (ATTACK, SHIELD, DODGE, MAGIC, LEFT, RIGHT, ATTACK_LEFT, ATTACK_RIGHT)

ACTION_LABELS: Final = {
    ATTACK: "⚔️ Атаковать",
    SHIELD: "🛡 Щит",
    DODGE: "↩ Уклониться",
    MAGIC: "✨ Магия",
    LEFT: "← Влево",
    RIGHT: "Вправо →",
    ATTACK_LEFT: "⚔️ Бить левого",
    ATTACK_RIGHT: "⚔️ Бить правого",
}

# --------------------------------------------------------------------------- grades
# Three, not two. A fight where every telegraph has exactly one right button is a quiz;
# these let a move have a best answer AND a defensible one, so a player who half-read the
# telegraph is punished less than one who ignored it.
PERFECT: Final = "perfect"
FINE: Final = "fine"
BAD: Final = "bad"

# --------------------------------------------------------------------------- balance
# Share of a phase's health bar one attack or one vulnerability window may remove. High
# enough that a strong pet feels its gear (a phase falls in a handful of good windows),
# low enough that nothing ends the fight in one press.
PHASE_DAMAGE_CAP_SHARE: Final = 0.22
# Burning: capped stacks, a tick before the hero's own action, and removable by playing
# well. It is the memory of past mistakes, which is why it is worth clearing.
BURN_MAX_STACKS: Final = 4
BURN_TICK_SHARE: Final = 0.035        # of the hero's maximum health, per stack, per turn
# How much of the hero's armour a mistake ignores. Mistake damage exists precisely to be
# unblockable by gear; a little armour still helps so the stat is never worthless.
MISTAKE_ARMOR_PIERCE: Final = 0.75

# The boss's single health pool from `pets.phoenix_boss_profile` is split between the two
# lives rather than doubled. The reborn Phoenix is the SHORTER, nastier half: it hits
# harder every turn, so giving it the bigger bar would only mean more turns in the half
# where a mistake costs the most.
PHASE_1_HP_SHARE: Final = 0.58
PHASE_2_HP_SHARE: Final = 0.42
PHASE_2_DAMAGE_MULTIPLIER: Final = 1.35
# Mistakes hurt more after the rebirth too, but by much less than the swing does. Mistake
# damage is already the largest single number in the fight; scaling it as hard as ordinary
# damage would put a phase-2 slip within one press of a full-health kill.
PHASE_2_MISTAKE_MULTIPLIER: Final = 1.15

# The three sizes of being wrong, as a share of the hero's maximum health.
MISTAKE_SMALL: Final = 0.075          # answered defensibly, just not well
MISTAKE_SERIOUS: Final = 0.14         # ignored the telegraph
MISTAKE_GRAVE: Final = 0.22           # walked into the one move that punishes exactly that

# No single exchange may take more than this share of the hero's maximum health, mistake
# damage and the boss's swing together. Without it a low-level pet meeting a deep-floor
# Phoenix dies to its first wrong button, and "one mistake never kills you, three do" is
# the rule the whole damage model is built on. Burning ticks land OUTSIDE this cap -- it
# caps the exchange, not the turn, and burning is the price of earlier exchanges.
SINGLE_HIT_HERO_CAP_SHARE: Final = 0.40
# Each further mistake without a clean answer in between costs more. Three in a row must
# be lethal for a hero that is otherwise doing fine, and a flat share only gets there for
# pets with no armour at all; the escalation is also what a boss "pressing an opening"
# should feel like.
MISTAKE_STREAK_STEP: Final = 0.30

# What an open Phoenix is worth. The full window is the reward for reading a telegraph
# that has exactly one right answer; the small one follows answers that were merely safe.
VULNERABLE_FULL_MULTIPLIER: Final = 2.15
VULNERABLE_SMALL_MULTIPLIER: Final = 1.15

# Пожирание пламени, unanswered. Big enough that ignoring it undoes a whole good window,
# which is what makes it worth interrupting rather than waiting out.
DEVOUR_HEAL_SHARE: Final = 0.12
# «Последнее Солнце» waits until the reborn Phoenix is nearly finished. Above a fifth of
# the bar it would land while the player still has a phase to fight; this way it is the
# last thing that happens.
FINAL_SUN_TRIGGER_SHARE: Final = 0.30
# However well the core is beaten, the second life keeps at least this much of its bar.
# The rebirth is a bonus for playing the interlude well, never a way to skip phase 2.
REBIRTH_FLOOR_SHARE: Final = 0.45
REBIRTH_STEPS: Final = 3

LOG_LINES: Final = 6
HISTORY_LINES: Final = 6

PHASE_2_NAME: Final = "🔥 Возрождённый Феникс"

# --------------------------------------------------------------------------- narration
SCENE_INTRO: Final = (
    "Зал засыпан горячим пеплом. Феникс поднимает голову, и перья на его груди "
    "медленно наливаются светом."
)
SCENE_PHASE_1_DEATH: Final = (
    "Феникс падает на каменный пол. Его тело рассыпается в раскалённый пепел. "
    "Но среди пепла продолжает пульсировать ослепительно белое ядро."
)
SCENE_PHASE_2: Final = (
    "Перья обуглились. Сквозь трещины в теле пробивается белое пламя."
)
SCENE_VICTORY: Final = (
    "Белое пламя гаснет. Пепел оседает на камни и больше не поднимается."
)
SCENE_DEFEAT: Final = (
    "Пепел смыкается над героем. Феникс расправляет крылья над остывающим залом."
)


def _hit(kind: str, share: float) -> dict:
    """A hero's own blow inside an answer: which stat swings and how much of it lands."""
    return {"kind": kind, "share": share}


def _answer(
    grade: str,
    *,
    ordinary: float = 0.0,
    mistake: float = 0.0,
    burn: int = 0,
    cleanse: int = 0,
    window: str = "",
    hit: dict | None = None,
    heal: float = 0.0,
    note: str = "",
) -> dict:
    """One row of an attack's answer table.

    `ordinary` is a multiple of the boss's real swing and meets armour and guard;
    `mistake` is a share of the hero's maximum health and mostly does not. Keeping them
    as separate fields rather than one number is what lets gear matter without letting
    gear excuse a wrong button.
    """
    return {
        "grade": grade, "ordinary": ordinary, "mistake": mistake, "burn": burn,
        "cleanse": cleanse, "window": window, "hit": hit, "heal": heal, "note": note,
    }


# --------------------------------------------------------------------------- the moves
#
# A telegraph is CONSTANT. The same move always reads the same way, down to the wording,
# because the entire fight is the player building a lookup table in their head over
# several attempts. A move whose prose varies is a move nobody can learn.
#
# `kind` says how the answer is graded:
#   plain       -- one row per button
#   directional -- the telegraph names the dangerous SIDE; the answer is the other one
#   double      -- two directional telegraphs resolved together, the second always mirrored
#   twins       -- pick a target rather than a manner
_ATTACKS: Final = {
    "wave": {
        "name": "Ударная волна крыльями",
        "kind": "plain",
        "phases": (1, 2),
        "weight": {1: 22, 2: 10},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Феникс широко расправляет крылья. Пепел у его лап начинает "
                     "расходиться кругами.",
        "answers": {
            SHIELD: _answer(PERFECT, ordinary=0.25, window="small", cleanse=1,
                            note="Щит принимает волну. Пепел расходится по сторонам, "
                                 "и Феникс на миг открывается."),
            DODGE: _answer(FINE, ordinary=0.55,
                           note="Уклонение выходит поздним: край волны всё же достаёт."),
            MAGIC: _answer(FINE, ordinary=0.85, mistake=MISTAKE_SMALL,
                           hit=_hit("magic", 1.0),
                           note="Заклинание уходит в Феникса, но волна проходит "
                                "прямо сквозь героя."),
            ATTACK: _answer(BAD, ordinary=1.0, mistake=MISTAKE_SERIOUS, burn=1,
                            note="Удар встречает волну в упор. Перья вспыхивают "
                                 "на герое."),
        },
    },
    "gather": {
        "name": "Накопление огня",
        "kind": "plain",
        "phases": (1,),
        "weight": {1: 18},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Пламя на перьях Феникса начинает тускнеть. Весь огонь словно "
                     "стягивается к его клюву.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.0), window="full",
                            note="Удар в клюв сбивает накопленный огонь. Феникс "
                                 "захлёбывается собственным пламенем."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.1), window="full",
                           note="Заклинание рвёт огонь у самого клюва. Феникс "
                                "сбивается."),
            SHIELD: _answer(FINE, ordinary=1.1, mistake=MISTAKE_SMALL,
                            note="Щит держит, но огонь заливает зал и прожигает "
                                 "всё вокруг."),
            DODGE: _answer(BAD, ordinary=1.2, mistake=MISTAKE_SERIOUS, burn=1,
                           note="Огонь расходится по всему залу — от него некуда уйти."),
        },
    },
    "dive": {
        "name": "Пикирование",
        "kind": "plain",
        "phases": (1, 2),
        "weight": {1: 20, 2: 10},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Феникс резко взмывает под потолок. Крылья прижимаются к телу, "
                     "а взгляд застывает на герое.",
        "answers": {
            DODGE: _answer(PERFECT, window="full", cleanse=1,
                           note="Феникс проносится мимо и врезается в камни. "
                                "Он не успевает подняться."),
            SHIELD: _answer(FINE, ordinary=0.7,
                            note="Щит выдерживает таран, но героя отбрасывает "
                                 "к стене."),
            MAGIC: _answer(FINE, ordinary=0.9, hit=_hit("magic", 0.7),
                           note="Заклинание встречает Феникса в воздухе, но не "
                                "сбивает его с линии."),
            ATTACK: _answer(BAD, ordinary=1.35, mistake=MISTAKE_SERIOUS,
                            note="Феникс бьёт сверху — встречный удар не достаёт "
                                 "и наполовину."),
        },
    },
    "wing": {
        "name": "Огненное крыло",
        "kind": "directional",
        "phases": (1, 2),
        "weight": {1: 22, 2: 12},
        "buttons": (LEFT, RIGHT, SHIELD, ATTACK, MAGIC),
        "sides": {
            "right": "Феникс резко наклоняется. Справа по каменному полу начинают "
                     "пробегать красные искры.",
            "left": "Феникс резко наклоняется. Слева по каменному полу начинают "
                    "пробегать красные искры.",
        },
        "answers": {
            "safe": _answer(PERFECT, ordinary=0.10, hit=_hit("physical", 0.7),
                            note="Крыло проходит по пустому камню, и герой достаёт "
                                 "Феникса в развороте."),
            "wrong": _answer(BAD, ordinary=1.15, mistake=MISTAKE_SERIOUS, burn=1,
                             note="Герой шагает прямо под крыло."),
            SHIELD: _answer(FINE, ordinary=0.9,
                            note="Щит принимает крыло вскользь."),
            ATTACK: _answer(BAD, ordinary=1.0, mistake=MISTAKE_SMALL,
                            hit=_hit("physical", 0.6),
                            note="Удар достаёт крыло, но огонь всё равно проходит "
                                 "по герою."),
            MAGIC: _answer(BAD, ordinary=1.0, mistake=MISTAKE_SMALL,
                           hit=_hit("magic", 0.7),
                           note="Заклинание сжигает часть перьев, но крыло всё "
                                "равно доходит."),
        },
    },
    "stance": {
        "name": "Пылающая защита",
        "kind": "plain",
        "phases": (1, 2),
        "weight": {1: 18, 2: 10},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Феникс складывает крылья перед собой. Между перьями вспыхивают "
                     "тонкие красные линии.",
        "answers": {
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.6), window="small",
                           note="Заклинание проходит сквозь сомкнутые крылья, "
                                "будто их нет."),
            SHIELD: _answer(FINE, cleanse=1,
                            note="Герой пережидает за щитом. Пламя на нём успевает "
                                 "погаснуть."),
            DODGE: _answer(FINE,
                           note="Герой отходит. Феникс так и стоит, сомкнув крылья."),
            ATTACK: _answer(BAD, ordinary=1.4, mistake=MISTAKE_GRAVE, burn=2,
                            note="Красные линии вспыхивают, и удар возвращается "
                                 "герою целиком."),
        },
    },
    # ------------------------------------------------------------------ вторая жизнь
    "white_flame": {
        "name": "Белое пламя",
        "kind": "plain",
        "phases": (2,),
        "weight": {2: 14},
        # The one move that goes past a raised shield. Phase 1 teaches "🛡 is never wrong";
        # this is the move that charges for that habit.
        "ignores_defence": True,
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Огонь вокруг Феникса становится почти белым. Пламя перестаёт "
                     "колыхаться и словно застывает.",
        "answers": {
            DODGE: _answer(PERFECT, hit=_hit("physical", 0.7), cleanse=1,
                           note="Белое пламя проходит там, где герой только что стоял."),
            MAGIC: _answer(FINE, ordinary=0.8, mistake=MISTAKE_SMALL,
                           hit=_hit("magic", 0.6),
                           note="Заклинание встречает белое пламя и гаснет вместе "
                                "с частью его."),
            ATTACK: _answer(BAD, ordinary=1.1, mistake=MISTAKE_SERIOUS,
                            note="Оружие входит прямо в белое пламя."),
            SHIELD: _answer(BAD, ordinary=1.2, mistake=MISTAKE_SERIOUS, burn=1,
                            note="Щит раскаляется добела и перестаёт держать "
                                 "что-либо вообще."),
        },
    },
    "double_wing": {
        "name": "Двойное огненное крыло",
        "kind": "double",
        "phases": (2,),
        "weight": {2: 12},
        "buttons": (LEFT, RIGHT, SHIELD, ATTACK, MAGIC),
        "sides": {
            "right": "Феникс раскрывает оба крыла. Справа по полу бегут красные "
                     "искры, второе крыло ещё поднято.",
            "left": "Феникс раскрывает оба крыла. Слева по полу бегут красные "
                    "искры, второе крыло ещё поднято.",
        },
        # The mirror is the rule: the second wing always comes from the other side. That
        # is the only thing a player has to remember, and it holds in every fight.
        "sides_2": {
            "right": "Второе крыло идёт следом. Искры бегут справа.",
            "left": "Второе крыло идёт следом. Искры бегут слева.",
        },
        "answers": {
            "both": _answer(PERFECT, hit=_hit("physical", 0.8), window="full",
                            note="Оба крыла проходят мимо. Феникс остаётся "
                                 "раскрытым."),
            "half": _answer(FINE, ordinary=0.8, mistake=MISTAKE_SMALL,
                            note="Одно крыло прошло мимо, второе достало."),
            "none": _answer(BAD, ordinary=1.3, mistake=MISTAKE_SERIOUS, burn=2,
                            note="Оба крыла сходятся на герое."),
        },
    },
    "twins": {
        "name": "Пепельные двойники",
        "kind": "twins",
        "phases": (2,),
        "weight": {2: 12},
        "buttons": (ATTACK_LEFT, ATTACK_RIGHT, MAGIC),
        # The tell never moves: ash RISES beside the living bird and FALLS beside a copy.
        # Everything else about the telegraph is symmetric on purpose.
        "sides": {
            "left": "Феникс взмахивает крыльями. Из облака пепла появляются два "
                    "похожих силуэта: слева пепел поднимается вверх, справа — "
                    "оседает вниз.",
            "right": "Феникс взмахивает крыльями. Из облака пепла появляются два "
                     "похожих силуэта: справа пепел поднимается вверх, слева — "
                     "оседает вниз.",
        },
        "answers": {
            "safe": _answer(PERFECT, hit=_hit("physical", 1.25), window="full",
                            note="Под ударом оказывается настоящий Феникс, и он "
                                 "не успевает уйти."),
            "wrong": _answer(BAD, ordinary=1.1, mistake=MISTAKE_SERIOUS,
                             note="Силуэт рассыпается пеплом, а настоящий бьёт "
                                  "с другой стороны."),
            MAGIC: _answer(FINE, ordinary=0.2, hit=_hit("magic", 0.5),
                           note="Волна магии сжигает оба силуэта разом."),
        },
    },
    "devour": {
        "name": "Пожирание пламени",
        "kind": "plain",
        "phases": (2,),
        "weight": {2: 10},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Феникс замирает. Угли вокруг его ран начинают разгораться "
                     "всё ярче.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.15), window="small",
                            note="Удар разбивает угли раньше, чем они успевают "
                                 "затянуть раны."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.25), window="small",
                           note="Заклинание гасит угли на ранах."),
            SHIELD: _answer(BAD, ordinary=0.3, mistake=MISTAKE_SMALL,
                            heal=DEVOUR_HEAL_SHARE,
                            note="Пока герой стоит за щитом, раны Феникса "
                                 "затягиваются."),
            DODGE: _answer(BAD, ordinary=0.3, mistake=MISTAKE_SMALL,
                           heal=DEVOUR_HEAL_SHARE,
                           note="Герой отходит, и Феникс спокойно допивает "
                                "собственное пламя."),
        },
    },
    "vanish": {
        "name": "Пепельное исчезновение",
        "kind": "plain",
        "phases": (2,),
        "weight": {2: 12},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Тело Феникса внезапно рассыпается в облако чёрного пепла. "
                     "За спиной героя начинают вспыхивать угли.",
        "answers": {
            SHIELD: _answer(PERFECT, ordinary=0.15, window="small", cleanse=1,
                            note="Герой разворачивается и встречает удар щитом."),
            DODGE: _answer(FINE, ordinary=0.7,
                           note="Герой уходит вслепую — удар в спину достаёт "
                                "только вскользь."),
            MAGIC: _answer(FINE, ordinary=0.8, mistake=MISTAKE_SMALL,
                           hit=_hit("magic", 0.4),
                           note="Заклинание выжигает пепел кругом, но Феникс уже "
                                "бьёт из-за спины."),
            # A swing forward at a boss that is no longer in front of you cannot land,
            # so this row carries no `hit` at all -- the miss IS the punishment.
            ATTACK: _answer(BAD, ordinary=1.1, mistake=MISTAKE_SERIOUS,
                            note="Удар рассекает пустоту: впереди уже никого нет."),
        },
    },
    "final_sun": {
        "name": "Последнее Солнце",
        "kind": "plain",
        "phases": (),          # never rolled; scheduled by _pick_attack near the end
        "weight": {},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Всё пламя в зале внезапно гаснет. Феникс опускает голову. "
                     "Даже пепел перестаёт двигаться.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.25), window="full",
                            note="Удар приходит в опущенную голову. Зал снова "
                                 "наполняется обычным огнём."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.35), window="full",
                           note="Заклинание бьёт в темноту и сбивает то, что "
                                "Феникс собирал."),
            SHIELD: _answer(FINE, ordinary=0.9, mistake=MISTAKE_SERIOUS, burn=1,
                            note="Свет возвращается разом. Щит выдерживает, "
                                 "рука за ним — нет."),
            DODGE: _answer(BAD, ordinary=1.5, mistake=MISTAKE_GRAVE, burn=2,
                           note="Свет возвращается разом и заливает весь зал. "
                                "Уходить было некуда."),
        },
    },
    # ------------------------------------------------------------------- между жизнями
    "core_vulnerable": {
        "name": "Ядро уязвимо",
        "kind": "plain",
        "phases": (),
        "weight": {},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Пепел со всего зала начинает стягиваться к ядру.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.0),
                            note="Удар приходит по самому ядру. Оно тускнеет."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.1),
                           note="Заклинание входит в ядро, и свет в нём проседает."),
            SHIELD: _answer(FINE,
                            note="Герой ждёт за щитом. Ядро спокойно собирает пепел."),
            DODGE: _answer(FINE,
                           note="Герой отходит. Ядро спокойно собирает пепел."),
        },
    },
    "core_burst": {
        "name": "Ядро взрывается",
        "kind": "plain",
        "phases": (),
        "weight": {},
        "buttons": (ATTACK, SHIELD, DODGE, MAGIC),
        "telegraph": "Ядро резко увеличивается. По его поверхности начинают "
                     "разбегаться белые трещины.",
        "answers": {
            SHIELD: _answer(PERFECT, cleanse=BURN_MAX_STACKS,
                            note="Взрыв уходит в щит. Пламя с героя срывает "
                                 "той же волной."),
            DODGE: _answer(PERFECT, cleanse=BURN_MAX_STACKS,
                           note="Герой успевает лечь за камень. Волна проходит "
                                "поверху и сбивает с него огонь."),
            ATTACK: _answer(BAD, ordinary=0.6, mistake=MISTAKE_SERIOUS, burn=1,
                            note="Оружие входит в трещины ровно в тот момент, "
                                 "когда ядро раскрывается."),
            MAGIC: _answer(BAD, ordinary=0.6, mistake=MISTAKE_SERIOUS, burn=1,
                           note="Заклинание разгоняет трещины, и ядро раскрывается "
                                "герою в лицо."),
        },
    },
}

_REBIRTH_SEQUENCE: Final = ("core_vulnerable", "core_vulnerable", "core_burst")


# ------------------------------------------------------------------------- профили
def _hero_profile(hero: dict) -> dict:
    """The hero's numbers, clamped into ranges the engine can reason about.

    Defaults exist so a half-built profile cannot crash a live fight; they are never a
    substitute for `pets.phoenix_hero_profile`, which is where the real numbers come from.
    """
    hero = dict(hero or {})
    return {
        "name": str(hero.get("name") or "Существо"),
        "max_hp": max(1, int(hero.get("max_hp", 100) or 100)),
        "damage": max(1, int(hero.get("damage", 10) or 10)),
        "spell_power": max(1, int(hero.get("spell_power", 10) or 10)),
        "crit": min(1.0, max(0.0, float(hero.get("crit", 0.0) or 0.0))),
        "crit_power": max(1.0, float(hero.get("crit_power", 2.0) or 2.0)),
        "reduction": min(0.90, max(0.0, float(hero.get("reduction", 0.0) or 0.0))),
        "guard": min(0.80, max(0.10, float(hero.get("guard", 0.40) or 0.40))),
        "has_magic": bool(hero.get("has_magic", False)),
        "level": max(1, int(hero.get("level", 1) or 1)),
    }


def _boss_profile(boss: dict) -> dict:
    boss = dict(boss or {})
    return {
        "name": str(boss.get("name") or "Феникс пепельных залов"),
        "max_hp": max(20, int(boss.get("max_hp", 1000) or 1000)),
        "damage": max(1, int(boss.get("damage", 50) or 50)),
        "level": max(1, int(boss.get("level", 1) or 1)),
        "floor": max(1, int(boss.get("floor", 1) or 1)),
    }


# ------------------------------------------------------------------------- публичное
def start(hero: dict, boss: dict, *, seed: int | None = None) -> dict:
    """Open a Phoenix fight and return its first state.

    `hero` is a profile from `pets.phoenix_hero_profile`; `boss` carries the encounter's
    own numbers. The returned dict is JSON-safe and is the ENTIRE fight -- persist it and
    hand it back to `take()`.
    """
    rng = random.Random(seed)
    hero_numbers = _hero_profile(hero)
    boss_numbers = _boss_profile(boss)
    phase_1 = max(10, round(boss_numbers["max_hp"] * PHASE_1_HP_SHARE))
    phase_2 = max(10, round(boss_numbers["max_hp"] * PHASE_2_HP_SHARE))
    state = {
        "version": 1,
        "hero": hero_numbers,
        "boss": boss_numbers,
        # Duplicated at the top level because the dungeon run reads the hero's health
        # straight off the saved state when the fight ends.
        "hero_hp": hero_numbers["max_hp"],
        "hero_max_hp": hero_numbers["max_hp"],
        "phase": 1,
        "phase_state": INTRO,
        "boss_hp": phase_1,
        "boss_max_hp": phase_1,
        "phase_2_max": phase_2,
        "burn": 0,
        "mistake_streak": 0,
        "attack": "",
        "side": "",
        "step": 1,
        "pending": "",
        "history": [],
        "used": [],
        "rebirth": {"step": 0, "core_damage": 0, "burst": ""},
        "telegraph": "",
        "scene": SCENE_INTRO,
        "log": [],
        "vulnerable": "",
        "actions_taken": 0,
    }
    _set_attack(state, _pick_attack(state, rng), rng)
    return state


def actions(state: dict) -> tuple[dict, ...]:
    """What the player may press right now, as {"code", "label"} rows.

    Direction buttons appear only for the moves that are about direction. Offering them
    every turn would turn a readable telegraph into a coin flip.
    """
    state = state or {}
    phase_state = str(state.get("phase_state") or "")
    if phase_state in (VICTORY, DEFEAT):
        return ()
    if phase_state == VULNERABLE:
        codes = [ATTACK, MAGIC]
    else:
        attack = _ATTACKS.get(str(state.get("attack") or ""))
        codes = list(attack["buttons"]) if attack else [ATTACK, SHIELD, DODGE, MAGIC]
    if not ((state.get("hero") or {}).get("has_magic")):
        # A pet with no scrolls has no spell to cast, and every move that wants ✨ has a
        # second answer that is at least safe -- so removing the button never removes the
        # only way through a telegraph.
        codes = [code for code in codes if code != MAGIC]
    return tuple({"code": code, "label": ACTION_LABELS[code]} for code in codes)


def take(state: dict, action: str, *, seed: int | None = None) -> dict:
    """Apply one player action and return the next state.

    Never mutates `state`. Raises ValueError for an action that is not on offer, so a
    replayed or hand-typed button cannot advance the fight.
    """
    nxt = copy.deepcopy(dict(state or {}))
    code = str(action or "")
    if code not in {row["code"] for row in actions(nxt)}:
        raise ValueError("Это действие сейчас недоступно.")
    rng = random.Random(seed)
    nxt["log"] = []
    nxt["scene"] = ""
    nxt["actions_taken"] = int(nxt.get("actions_taken", 0) or 0) + 1
    phase_state = str(nxt.get("phase_state") or "")
    if phase_state == VULNERABLE:
        _resolve_window(nxt, code, rng)
    elif phase_state == REBIRTH:
        _resolve_rebirth(nxt, code, rng)
    else:
        _resolve_telegraph(nxt, code, rng)
    nxt["log"] = nxt["log"][-LOG_LINES:]
    return nxt


def public(state: dict) -> dict:
    """The state as a client renders it. This shape is the contract both clients draw.

        {
          "boss_name":   str,    # "Феникс пепельных залов" / "🔥 Возрождённый Феникс"
          "phase":       1 | 2,
          "phase_state": one of STATES,
          "boss_hp":     int, "boss_max_hp": int,
          "hero_hp":     int, "hero_max_hp": int,
          "burn":        int,    # 0..BURN_MAX_STACKS
          "telegraph":   str,    # what the Phoenix is DOING; "" outside TELEGRAPH
          "scene":       str,    # narration for intro / rebirth / victory / defeat
          "log":         [str],  # what just happened, newest last, short lines
          "actions":     [{"code": str, "label": str}],
          "vulnerable":  bool,
          "over":        bool,
          "won":         bool,
        }

    Everything a screen needs is answered here, including the buttons: a client that
    decides for itself which actions exist would drift from the engine the first time a
    move changed, and the direction buttons are exactly the case where that matters.
    """
    state = state or {}
    phase_state = str(state.get("phase_state") or "")
    phase = int(state.get("phase", 1) or 1)
    name = PHASE_2_NAME if phase == 2 else str((state.get("boss") or {}).get("name") or "")
    # The core telegraphs during the rebirth exactly as the bird does, so the interlude
    # keeps the same read-and-answer loop instead of turning into a cutscene.
    shows_telegraph = phase_state in (INTRO, TELEGRAPH, REBIRTH)
    return {
        "boss_name": name,
        "phase": phase,
        "phase_state": phase_state,
        "boss_hp": max(0, int(state.get("boss_hp", 0) or 0)),
        "boss_max_hp": max(1, int(state.get("boss_max_hp", 1) or 1)),
        "hero_hp": max(0, int(state.get("hero_hp", 0) or 0)),
        "hero_max_hp": max(1, int(state.get("hero_max_hp", 1) or 1)),
        "burn": max(0, int(state.get("burn", 0) or 0)),
        "telegraph": str(state.get("telegraph") or "") if shows_telegraph else "",
        "scene": str(state.get("scene") or ""),
        "log": list(state.get("log") or []),
        "actions": [dict(row) for row in actions(state)],
        "vulnerable": phase_state == VULNERABLE,
        "over": phase_state in (VICTORY, DEFEAT),
        "won": phase_state == VICTORY,
    }


def is_over(state: dict) -> bool:
    return str((state or {}).get("phase_state")) in (VICTORY, DEFEAT)


# ------------------------------------------------------------------------- выбор атаки
def _roster(phase: int) -> list[str]:
    return [code for code, row in _ATTACKS.items() if phase in row["phases"]]


def _pick_attack(state: dict, rng: random.Random) -> str:
    """The next telegraph: weighted, and never the one that just happened.

    Back-to-back repeats are the one pattern that makes a learnable fight feel like a
    slot machine -- the player answers the same prose twice and cannot tell whether they
    read it or guessed it.
    """
    phase = int(state.get("phase", 1) or 1)
    if (
        phase == 2
        and "final_sun" not in list(state.get("used") or [])
        and 0 < int(state.get("boss_hp", 0) or 0)
        <= FINAL_SUN_TRIGGER_SHARE * max(1, int(state.get("boss_max_hp", 1) or 1))
    ):
        return "final_sun"
    history = list(state.get("history") or [])
    last = history[-1] if history else ""
    pool = [code for code in _roster(phase) if code != last]
    if not pool:                                   # a one-move phase cannot exist, but
        pool = _roster(phase)                      # never let the pick return nothing
    weights = [_ATTACKS[code]["weight"].get(phase, 1) for code in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def _set_attack(state: dict, code: str, rng: random.Random) -> None:
    attack = _ATTACKS[code]
    state["attack"] = code
    state["step"] = 1
    state["pending"] = ""
    state["side"] = rng.choice(("left", "right")) if attack.get("sides") else ""
    state["telegraph"] = _telegraph_text(state)
    history = list(state.get("history") or [])
    history.append(code)
    state["history"] = history[-HISTORY_LINES:]
    if code == "final_sun":
        used = list(state.get("used") or [])
        used.append("final_sun")
        state["used"] = used


def _telegraph_text(state: dict) -> str:
    attack = _ATTACKS[str(state.get("attack") or "")]
    side = str(state.get("side") or "")
    if int(state.get("step", 1) or 1) == 2 and attack.get("sides_2"):
        return attack["sides_2"][_other_side(side)]
    if attack.get("sides"):
        return attack["sides"][side]
    return str(attack.get("telegraph") or "")


def _other_side(side: str) -> str:
    return "left" if side == "right" else "right"


# --------------------------------------------------------------------------- оценка
def _grade(state: dict, action: str) -> dict:
    """The answer row for what the player just pressed."""
    attack = _ATTACKS[str(state.get("attack") or "")]
    kind = attack["kind"]
    answers = attack["answers"]
    side = str(state.get("side") or "")
    if kind == "directional":
        if action in (LEFT, RIGHT):
            return answers["safe" if _dodged(action, side) else "wrong"]
        return answers[action]
    if kind == "twins":
        if action in (ATTACK_LEFT, ATTACK_RIGHT):
            real = ATTACK_LEFT if side == "left" else ATTACK_RIGHT
            return answers["safe" if action == real else "wrong"]
        return answers[action]
    if kind == "double":
        first = str(state.get("pending") or "")
        second = _dodged(action, _other_side(side)) if action in (LEFT, RIGHT) else False
        clean = int(first == "clear") + int(second)
        return answers[("none", "half", "both")[clean]]
    return answers[action]


def _dodged(action: str, danger: str) -> bool:
    """Whether stepping this way leaves the burning side of the floor."""
    return (action == LEFT and danger == "right") or (action == RIGHT and danger == "left")


# --------------------------------------------------------------------------- урон
def _hero_hit(state: dict, rng: random.Random, hit: dict, *, multiplier: float = 1.0,
              cap_hp: int | None = None) -> int:
    """One blow from the hero, capped at a share of the CURRENT phase's bar.

    The cap is what keeps "a strong pet finishes faster" from becoming "a strong pet
    deletes a phase in one press": gear buys fewer windows, never zero.
    """
    hero = state["hero"]
    base = hero["spell_power"] if hit.get("kind") == "magic" else hero["damage"]
    raw = base * float(hit.get("share", 1.0)) * multiplier
    if rng.random() < hero["crit"]:
        raw *= hero["crit_power"]
        state["log"].append("Критический удар.")
    ceiling = max(1, round((cap_hp or state["boss_max_hp"]) * PHASE_DAMAGE_CAP_SHARE))
    return max(1, min(round(raw), ceiling))


def _ordinary_damage(state: dict, share: float, *, shielded: bool, pierces: bool) -> float:
    """The boss's real swing, met by the hero's real armour and guard."""
    if share <= 0:
        return 0.0
    hero, boss = state["hero"], state["boss"]
    raw = boss["damage"] * share
    if int(state.get("phase", 1) or 1) == 2:
        raw *= PHASE_2_DAMAGE_MULTIPLIER
    if not pierces:
        raw *= 1.0 - hero["reduction"]
        if shielded:
            raw *= 1.0 - hero["guard"]
    return raw


def _mistake_damage(state: dict, share: float) -> float:
    """The part of being wrong that gear cannot answer."""
    if share <= 0:
        return 0.0
    hero = state["hero"]
    if int(state.get("phase", 1) or 1) == 2:
        share *= PHASE_2_MISTAKE_MULTIPLIER
    share *= 1.0 + MISTAKE_STREAK_STEP * max(0, int(state.get("mistake_streak", 0) or 0))
    armour = hero["reduction"] * (1.0 - MISTAKE_ARMOR_PIERCE)
    return hero["max_hp"] * share * (1.0 - armour)


def _hurt_hero(state: dict, amount: float) -> int:
    amount = max(0, round(amount))
    state["hero_hp"] = max(0, int(state["hero_hp"]) - amount)
    return amount


def _burn_tick(state: dict) -> None:
    """Burning bites at the top of the boss's turn, before the answer is graded.

    It deliberately lands outside the single-exchange cap: the cap is a promise about one
    wrong button, and burning is the bill for the wrong buttons already pressed.
    """
    stacks = int(state.get("burn", 0) or 0)
    if stacks <= 0:
        return
    dealt = _hurt_hero(state, state["hero"]["max_hp"] * BURN_TICK_SHARE * stacks)
    state["log"].append(f"🔥 Горение (x{stacks}): -{dealt}")


def _apply(state: dict, row: dict, action: str, rng: random.Random) -> None:
    """Everything one graded answer does, in the order the player sees it happen."""
    attack = _ATTACKS.get(str(state.get("attack") or "")) or {}
    if row.get("note"):
        state["log"].append(str(row["note"]))

    hit = row.get("hit")
    if hit and int(state["boss_hp"]) > 0:
        dealt = _hero_hit(state, rng, hit)
        state["boss_hp"] = max(0, int(state["boss_hp"]) - dealt)
        state["log"].append(f"Феникс теряет {dealt} здоровья.")

    heal = float(row.get("heal", 0.0) or 0.0)
    if heal > 0 and int(state["boss_hp"]) > 0:
        healed = round(state["boss_max_hp"] * heal)
        before = int(state["boss_hp"])
        state["boss_hp"] = min(int(state["boss_max_hp"]), before + healed)
        state["log"].append(f"Феникс восстанавливает {state['boss_hp'] - before} здоровья.")

    ordinary = _ordinary_damage(
        state, float(row.get("ordinary", 0.0) or 0.0),
        shielded=(action == SHIELD), pierces=bool(attack.get("ignores_defence")),
    )
    mistake = _mistake_damage(state, float(row.get("mistake", 0.0) or 0.0))
    cap = state["hero"]["max_hp"] * SINGLE_HIT_HERO_CAP_SHARE
    dealt = _hurt_hero(state, min(ordinary + mistake, cap))
    if dealt:
        state["log"].append(f"Герой теряет {dealt} здоровья.")

    if row.get("cleanse"):
        cleared = min(int(state.get("burn", 0) or 0), int(row["cleanse"]))
        if cleared:
            state["burn"] = int(state["burn"]) - cleared
            state["log"].append(f"🔥 Горение спадает (-{cleared}).")
    if row.get("burn"):
        state["burn"] = min(BURN_MAX_STACKS, int(state.get("burn", 0) or 0) + int(row["burn"]))
        state["log"].append(f"🔥 Горение: x{state['burn']}")

    if row.get("grade") == BAD:
        state["mistake_streak"] = int(state.get("mistake_streak", 0) or 0) + 1
    else:
        state["mistake_streak"] = 0


# --------------------------------------------------------------------------- переходы
def _resolve_telegraph(state: dict, action: str, rng: random.Random) -> None:
    _burn_tick(state)
    if int(state["hero_hp"]) <= 0:
        _defeat(state)
        return

    attack = _ATTACKS[str(state.get("attack") or "")]
    if attack["kind"] == "double" and int(state.get("step", 1) or 1) == 1:
        # The first wing only sets the trap; nothing lands until the mirrored second one,
        # so a player who reads the pair correctly pays nothing for the setup.
        state["pending"] = "clear" if (action in (LEFT, RIGHT)
                                       and _dodged(action, str(state["side"]))) else "hit"
        state["step"] = 2
        state["telegraph"] = _telegraph_text(state)
        state["phase_state"] = TELEGRAPH
        state["log"].append("Второе крыло уже идёт следом.")
        return

    row = _grade(state, action)
    _apply(state, row, action, rng)
    _advance(state, str(row.get("window") or ""), rng)


def _resolve_window(state: dict, action: str, rng: random.Random) -> None:
    """A guaranteed blow at an open Phoenix, with no answer coming back.

    Burning does not tick here on purpose: a window is time stolen from the boss, not a
    turn it was given, and charging the player twice per opening would make the reward
    for a perfect read smaller than the reward for a safe one.
    """
    multiplier = (VULNERABLE_FULL_MULTIPLIER if str(state.get("vulnerable")) == "full"
                  else VULNERABLE_SMALL_MULTIPLIER)
    kind = "magic" if action == MAGIC else "physical"
    dealt = _hero_hit(state, rng, _hit(kind, 1.0), multiplier=multiplier)
    state["boss_hp"] = max(0, int(state["boss_hp"]) - dealt)
    state["log"].append(f"💥 Открытый удар: Феникс теряет {dealt} здоровья.")
    state["vulnerable"] = ""
    _advance(state, "", rng)


def _advance(state: dict, window: str, rng: random.Random) -> None:
    if int(state["hero_hp"]) <= 0:
        _defeat(state)
        return
    if int(state["boss_hp"]) <= 0:
        if int(state.get("phase", 1) or 1) == 1:
            _enter_rebirth(state)
        else:
            _victory(state)
        return
    if window:
        state["phase_state"] = VULNERABLE
        state["vulnerable"] = window
        state["telegraph"] = ""
        state["log"].append("💥 УЯЗВИМ")
        return
    state["phase_state"] = TELEGRAPH
    state["vulnerable"] = ""
    _set_attack(state, _pick_attack(state, rng), rng)


def _victory(state: dict) -> None:
    state["phase_state"] = VICTORY
    state["boss_hp"] = 0
    state["telegraph"] = ""
    state["vulnerable"] = ""
    state["scene"] = SCENE_VICTORY


def _defeat(state: dict) -> None:
    state["phase_state"] = DEFEAT
    state["hero_hp"] = 0
    state["telegraph"] = ""
    state["vulnerable"] = ""
    state["scene"] = SCENE_DEFEAT


# --------------------------------------------------------------------------- возрождение
def _enter_rebirth(state: dict) -> None:
    """Phase one ends the moment the bar empties, and the overflow is thrown away.

    Whatever the killing blow had left over does NOT roll into the second life. Carrying
    it would make the rebirth a formality for anybody strong enough to overkill, and the
    rebirth is the part of the fight that has to be played rather than out-statted.
    """
    state["boss_hp"] = 0
    state["phase_state"] = REBIRTH
    state["vulnerable"] = ""
    state["rebirth"] = {"step": 0, "core_damage": 0, "burst": ""}
    state["scene"] = SCENE_PHASE_1_DEATH
    _set_core(state)


def _set_core(state: dict) -> None:
    step = int((state.get("rebirth") or {}).get("step", 0) or 0)
    state["attack"] = _REBIRTH_SEQUENCE[min(step, len(_REBIRTH_SEQUENCE) - 1)]
    state["side"] = ""
    state["step"] = 1
    state["pending"] = ""
    state["telegraph"] = str(_ATTACKS[state["attack"]]["telegraph"])


def _resolve_rebirth(state: dict, action: str, rng: random.Random) -> None:
    _burn_tick(state)
    if int(state["hero_hp"]) <= 0:
        _defeat(state)
        return

    rebirth = dict(state.get("rebirth") or {})
    code = str(state.get("attack") or "")
    row = _ATTACKS[code]["answers"][action]
    if row.get("note"):
        state["log"].append(str(row["note"]))

    hit = row.get("hit")
    if hit:
        # The core has no bar of its own; a hit here is spent on the SECOND life's health,
        # which is why it is capped against phase two rather than against phase one.
        dealt = _hero_hit(state, rng, hit, cap_hp=int(state["phase_2_max"]))
        rebirth["core_damage"] = int(rebirth.get("core_damage", 0) or 0) + dealt
        state["log"].append(f"Ядро тускнеет на {dealt}.")

    ordinary = _ordinary_damage(state, float(row.get("ordinary", 0.0) or 0.0),
                                shielded=(action == SHIELD), pierces=False)
    mistake = _mistake_damage(state, float(row.get("mistake", 0.0) or 0.0))
    cap = state["hero"]["max_hp"] * SINGLE_HIT_HERO_CAP_SHARE
    dealt = _hurt_hero(state, min(ordinary + mistake, cap))
    if dealt:
        state["log"].append(f"Герой теряет {dealt} здоровья.")
    if row.get("cleanse") and int(state.get("burn", 0) or 0):
        state["burn"] = max(0, int(state["burn"]) - int(row["cleanse"]))
    if row.get("burn"):
        state["burn"] = min(BURN_MAX_STACKS, int(state.get("burn", 0) or 0) + int(row["burn"]))
    state["mistake_streak"] = (int(state.get("mistake_streak", 0) or 0) + 1
                               if row.get("grade") == BAD else 0)

    if code == "core_burst":
        rebirth["burst"] = str(row.get("grade") or "")
    rebirth["step"] = int(rebirth.get("step", 0) or 0) + 1
    state["rebirth"] = rebirth

    if int(state["hero_hp"]) <= 0:
        _defeat(state)
        return
    if rebirth["step"] < REBIRTH_STEPS:
        _set_core(state)
        return
    _enter_phase_2(state, rng)


def _enter_phase_2(state: dict, rng: random.Random) -> None:
    rebirth = dict(state.get("rebirth") or {})
    phase_2 = max(10, int(state["phase_2_max"]))
    floor = max(1, round(phase_2 * REBIRTH_FLOOR_SHARE))
    state["phase"] = 2
    state["boss_max_hp"] = phase_2
    state["boss_hp"] = max(floor, phase_2 - int(rebirth.get("core_damage", 0) or 0))
    # Riding out the burst cleanly is what puts the fire out; a player who kept swinging
    # at a cracking core walks into the second life still burning.
    if str(rebirth.get("burst")) == PERFECT:
        state["burn"] = 0
    state["mistake_streak"] = 0
    state["phase_state"] = TELEGRAPH
    state["vulnerable"] = ""
    state["scene"] = SCENE_PHASE_2
    # The history deliberately survives the rebirth. The core's telegraphs are not the
    # bird's, so from the player's side the last move of the first life and the first move
    # of the second are still two telegraphs in a row -- and repeating one across the seam
    # reads exactly as badly as repeating one anywhere else.
    _set_attack(state, _pick_attack(state, rng), rng)
