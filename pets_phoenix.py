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

Scrolls
-------
✨ is a shelf, not a spell. Pressing it opens the equipped loadout, and the scroll the
player picks is what answers the telegraph. Each scroll is spendable ONCE across the whole
encounter -- both lives and the rebirth between them -- so four casts have to cover a fight
that runs twenty-odd turns, and "spend the heal now or save it" is a real question.

The telegraph and the loadout are read together, never separately. The graded answer row
says what the Phoenix does back and how well magic lands against this particular move; the
scroll says what the hero actually did. That is why a barrier cast into a charging Phoenix
does not interrupt it: the move asks for damage, and the loadout is what decides whether
there was any.

A hero profile with no `spells` at all keeps the plain, unlimited ✨ of a fight that was
saved before a loadout was ever attached to it.
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
# ✨ names a shelf, not a spell: pressing it opens the loadout and one of the scrolls
# below is the actual answer. `cancel` closes the shelf again.
CANCEL: Final = "cancel"
# Slot numbers rather than scroll codes, because these travel in a Telegram callback,
# which is length-limited, and because a slot is stable while a saved fight is asleep.
SPELL_PREFIX: Final = "spell_"

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
    CANCEL: "◀️ Назад",
}


def spell_code(slot: int) -> str:
    return f"{SPELL_PREFIX}{int(slot)}"


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

# A scroll that stuns buys the largest opening the fight has. Those scrolls carry the
# weakest damage in the catalogue, and a cast is one of four for the whole encounter, so
# anything smaller would make the stun line strictly worse than a plain damage scroll.
SPELL_STUN_WINDOW: Final = "full"

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
#
# `needs_damage` marks the moves that are being INTERRUPTED rather than merely answered.
# Interrupting is a physical fact about the cast: a barrier or a bandage thrown at a
# Phoenix that is winding up does not stop the wind-up, however well the player read the
# telegraph. Those moves carry a `magic_soft` row for exactly that cast.
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
        "needs_damage": True,
        "telegraph": "Пламя на перьях Феникса начинает тускнеть. Весь огонь словно "
                     "стягивается к его клюву.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.0), window="full",
                            note="Удар в клюв сбивает накопленный огонь. Феникс "
                                 "захлёбывается собственным пламенем."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.1), window="full",
                           note="Заклинание рвёт огонь у самого клюва. Феникс "
                                "сбивается."),
            "magic_soft": _answer(BAD, ordinary=1.05, mistake=MISTAKE_SMALL, burn=1,
                                  note="Свиток раскрывается, но огня у клюва он не "
                                       "трогает — пламя уходит в зал целиком."),
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
        "needs_damage": True,
        "telegraph": "Феникс замирает. Угли вокруг его ран начинают разгораться "
                     "всё ярче.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.15), window="small",
                            note="Удар разбивает угли раньше, чем они успевают "
                                 "затянуть раны."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.25), window="small",
                           note="Заклинание гасит угли на ранах."),
            "magic_soft": _answer(BAD, ordinary=0.3, mistake=MISTAKE_SMALL,
                                  heal=DEVOUR_HEAL_SHARE,
                                  note="Свиток не сбивает углей, и раны Феникса "
                                       "спокойно затягиваются."),
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
        "needs_damage": True,
        "telegraph": "Всё пламя в зале внезапно гаснет. Феникс опускает голову. "
                     "Даже пепел перестаёт двигаться.",
        "answers": {
            ATTACK: _answer(PERFECT, hit=_hit("physical", 1.25), window="full",
                            note="Удар приходит в опущенную голову. Зал снова "
                                 "наполняется обычным огнём."),
            MAGIC: _answer(PERFECT, hit=_hit("magic", 1.35), window="full",
                           note="Заклинание бьёт в темноту и сбивает то, что "
                                "Феникс собирал."),
            "magic_soft": _answer(BAD, ordinary=1.15, mistake=MISTAKE_SERIOUS, burn=1,
                                  note="Свиток вспыхивает в темноте и гаснет впустую. "
                                       "Свет возвращается разом."),
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
def _spell_profile(row: dict, index: int) -> dict:
    """One equipped scroll, clamped to the fields this engine spends.

    Every share is read defensively: the catalogue grows, and a scroll effect this fight
    has no answer for must land as "does nothing" rather than as a crash mid-encounter.
    """
    row = dict(row or {})
    return {
        "slot": max(1, int(row.get("slot", index) or index)),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or "Свиток"),
        "icon": str(row.get("icon") or "✨"),
        "ultimate": bool(row.get("ultimate")),
        "damage": max(0.0, float(row.get("damage", 0.0) or 0.0)),
        "heal": max(0.0, float(row.get("heal", 0.0) or 0.0)),
        "shield": max(0.0, float(row.get("shield", 0.0) or 0.0)),
        "burn": max(0.0, float(row.get("burn", 0.0) or 0.0)),
        "lifesteal": min(1.0, max(0.0, float(row.get("lifesteal", 0.0) or 0.0))),
        "cleanse": bool(row.get("cleanse")),
        "stun": bool(row.get("stun")),
    }


def _spell_list(rows) -> list[dict]:
    """The loadout in slot order, one scroll per slot.

    A duplicated slot number would mean two buttons that spend each other, so the first
    row to claim a slot keeps it.
    """
    spells, seen = [], []
    for index, row in enumerate(rows or (), start=1):
        spell = _spell_profile(row, index)
        if spell["slot"] in seen:
            continue
        seen.append(spell["slot"])
        spells.append(spell)
    return sorted(spells, key=lambda spell: spell["slot"])


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
        # Empty means the caller sent no loadout -- a fight saved before loadouts existed,
        # or a harness that only cares about the numbers -- and that hero keeps the plain
        # unlimited ✨ rather than losing the button mid-encounter. A pet with genuinely no
        # scrolls is already covered by `has_magic`.
        "spells": _spell_list(hero.get("spells")),
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
        # Slot numbers, not scroll codes: the loadout may hold the same scroll twice, and
        # only the slot that was actually pressed is the one that burns.
        "spent_spells": [],
        # Whether the scroll shelf is open. It rides on the state rather than on the
        # phase, so the telegraph stays on screen while the player reads their loadout.
        "picking": False,
        "rebirth": {"step": 0, "core_damage": 0, "burst": ""},
        "telegraph": "",
        "scene": SCENE_INTRO,
        "log": [],
        "vulnerable": "",
        "actions_taken": 0,
    }
    _set_attack(state, _pick_attack(state, rng), rng)
    return state


def _unspent(state: dict) -> list[dict]:
    """The equipped scrolls still on the shelf, in slot order."""
    spent = [int(slot) for slot in (state.get("spent_spells") or [])]
    return [dict(spell) for spell in ((state.get("hero") or {}).get("spells") or [])
            if int(spell.get("slot", 0) or 0) not in spent]


def _has_loadout(state: dict) -> bool:
    return bool(((state.get("hero") or {}).get("spells") or []))


def _picker_opens(state: dict) -> bool:
    """Whether ✨ opens the shelf rather than resolving on its own.

    It opens EVERYWHERE the button is offered, and that consistency is the rule rather
    than a convenience. A button that usually asks which scroll and occasionally spends
    the turn instead is the same button doing two different things behind one label, and
    the player finds out which by losing a turn to it.

    The only state where ✨ still resolves on its own is a fighter with no scrolls at all,
    where there is nothing to choose between and the button is raw spell power.
    """
    return bool(_has_loadout(state) and _unspent(state))


def _magic_offered(state: dict) -> bool:
    """Whether ✨ is on the table at all.

    A pet with no scrolls has nothing to cast, and a pet that has spent every scroll is in
    exactly the same position -- so the button leaves rather than becoming a dead end.
    Every move that wants ✨ keeps a second answer that is at least safe, which is what
    lets the button vanish without ever closing off a telegraph.
    """
    hero = state.get("hero") or {}
    if not hero.get("has_magic"):
        return False
    # The first wing of the double is the one press that resolves nothing: it only records
    # whether the hero stepped clear, and everything lands on the mirrored second wing. A
    # scroll spent into it therefore struck no one and healed against no incoming hit --
    # the shelf was one lighter and the fight had not moved. Weapon and shield can be
    # thrown away there for free, so they stay; the scroll leaves and comes straight back
    # for the wing that actually answers.
    attack = _ATTACKS.get(str(state.get("attack") or "")) or {}
    if (attack.get("kind") == "double" and int(state.get("step", 1) or 1) == 1
            and str(state.get("phase_state") or "") == TELEGRAPH):
        return False
    return bool(_unspent(state)) if _has_loadout(state) else True


def actions(state: dict) -> tuple[dict, ...]:
    """What the player may press right now, as {"code", "label"} rows.

    Direction buttons appear only for the moves that are about direction. Offering them
    every turn would turn a readable telegraph into a coin flip.

    With the shelf open the rows are the unspent scrolls themselves. Both clients draw
    whatever comes back here verbatim, so the picker needs nothing else to exist.
    """
    state = state or {}
    phase_state = str(state.get("phase_state") or "")
    if phase_state in (VICTORY, DEFEAT):
        return ()
    if state.get("picking") and _picker_opens(state):
        rows = [{"code": spell_code(spell["slot"]),
                 "label": f"{spell['icon']} {spell['name']}"} for spell in _unspent(state)]
        rows.append({"code": CANCEL, "label": ACTION_LABELS[CANCEL]})
        return tuple(rows)
    if phase_state == VULNERABLE:
        codes = [ATTACK, MAGIC]
    else:
        attack = _ATTACKS.get(str(state.get("attack") or ""))
        codes = list(attack["buttons"]) if attack else [ATTACK, SHIELD, DODGE, MAGIC]
    if not _magic_offered(state):
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
    # Opening and closing the shelf is navigation, not a turn: the Phoenix does not move,
    # nothing burns, nothing is graded, and the previous outcome stays on screen under a
    # telegraph the player is still answering.
    if code == MAGIC and _picker_opens(nxt):
        nxt["picking"] = True
        return nxt
    if code == CANCEL:
        nxt["picking"] = False
        return nxt
    nxt["picking"] = False
    spell = _spend_spell(nxt, code)
    if spell is not None:
        # From here the cast IS the ✨ answer. The whole telegraph, grading and window
        # machinery reads it as such; only what the hero did differs.
        code = MAGIC
    rng = random.Random(seed)
    nxt["log"] = []
    nxt["scene"] = ""
    nxt["grade"] = ""
    nxt["actions_taken"] = int(nxt.get("actions_taken", 0) or 0) + 1
    phase_state = str(nxt.get("phase_state") or "")
    if phase_state == VULNERABLE:
        _resolve_window(nxt, code, rng, spell)
    elif phase_state == REBIRTH:
        _resolve_rebirth(nxt, code, rng, spell)
    else:
        _resolve_telegraph(nxt, code, rng, spell)
    nxt["log"] = nxt["log"][-LOG_LINES:]
    return nxt


def _spend_spell(state: dict, code: str) -> dict | None:
    """Take the chosen scroll off the shelf for good, or None if this was not a cast."""
    if not code.startswith(SPELL_PREFIX):
        return None
    wanted = code[len(SPELL_PREFIX):]
    for spell in _unspent(state):
        if str(spell["slot"]) == wanted:
            state["spent_spells"] = [int(slot) for slot in (state.get("spent_spells") or [])]
            state["spent_spells"].append(int(spell["slot"]))
            return spell
    raise ValueError("Этот свиток уже использован.")


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
          "log":         [str],  # the LAST answer's outcome, newest last; empty on entry
          "grade":       str,    # "perfect" | "fine" | "bad" | "" -- how that answer read
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
        "grade": str(state.get("grade") or ""),
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
def _bites(spell: dict | None) -> bool:
    """Whether this cast actually hurts the Phoenix.

    A cast with no loadout behind it is the plain ✨ of a fight saved before loadouts, and
    that one has always bitten.
    """
    if spell is None:
        return True
    return float(spell.get("damage", 0) or 0) > 0 or float(spell.get("burn", 0) or 0) > 0


def _grade(state: dict, action: str, spell: dict | None = None) -> dict:
    """The answer row for what the player just pressed.

    The loadout is read here, alongside the telegraph, because a move that is being
    interrupted cares what the cast was made of and not how well it was timed.
    """
    attack = _ATTACKS[str(state.get("attack") or "")]
    if action == MAGIC and attack.get("needs_damage") and not _bites(spell):
        return attack["answers"]["magic_soft"]
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
              cap_hp: int | None = None, spent: int = 0) -> int:
    """One blow from the hero, capped at a share of the CURRENT phase's bar.

    The cap is what keeps "a strong pet finishes faster" from becoming "a strong pet
    deletes a phase in one press": gear buys fewer windows, never zero.

    `spent` is what the same exchange has already removed. A scroll that strikes and then
    burns is still ONE exchange, so the second half draws on what the first left of the
    cap rather than getting a fresh one.
    """
    hero = state["hero"]
    base = hero["spell_power"] if hit.get("kind") == "magic" else hero["damage"]
    raw = base * float(hit.get("share", 1.0)) * multiplier
    if rng.random() < hero["crit"]:
        raw *= hero["crit_power"]
        state["log"].append("Критический удар.")
    ceiling = round((cap_hp or state["boss_max_hp"]) * PHASE_DAMAGE_CAP_SHARE) - max(0, spent)
    if spent and ceiling <= 0:
        return 0
    return max(1, min(round(raw), max(1, ceiling)))


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


def _heal_hero(state: dict, amount: float) -> int:
    before = int(state["hero_hp"])
    state["hero_hp"] = min(int(state["hero_max_hp"]), before + max(0, round(amount)))
    return int(state["hero_hp"]) - before


def _spell_strike(state: dict, rng: random.Random, row: dict, spell: dict, *,
                  core: bool = False) -> int:
    """What the scroll does TO the Phoenix, and what that damage feeds back to the hero.

    The two halves of the number come from different places on purpose. The answer row's
    share says how well magic lands against this particular move -- a spell walks through
    folded wings and barely finds a Phoenix that has already scattered into ash -- and the
    scroll says how much magic there was. A scroll with no damage at all therefore does
    nothing here however well the telegraph was read, which is the same fact the interrupt
    rule is built on.
    """
    hit = row.get("hit") or {}
    if not hit:
        return 0
    cap_hp = int(state["phase_2_max"]) if core else None
    share = float(hit.get("share", 0.0) or 0.0)
    dealt = 0

    strike = share * float(spell.get("damage", 0.0) or 0.0)
    if strike > 0:
        dealt += _damage_boss(state, rng, strike, cap_hp=cap_hp, spent=dealt, core=core)

    lifesteal = float(spell.get("lifesteal", 0.0) or 0.0)
    if lifesteal > 0 and dealt > 0:
        healed = _heal_hero(state, dealt * lifesteal)
        if healed:
            state["log"].append(f"🩸 Свиток возвращает {healed} здоровья.")

    burn = float(spell.get("burn", 0.0) or 0.0)
    if burn > 0:
        burned = _damage_boss(state, rng, burn, cap_hp=cap_hp, spent=dealt, core=core,
                              note="🔥 Пламя свитка догорает на Фениксе")
        dealt += burned
    return dealt


def _damage_boss(state: dict, rng: random.Random, share: float, *, cap_hp: int | None,
                 spent: int, core: bool, note: str = "") -> int:
    """One magical blow, sent to whichever pool the Phoenix currently keeps its life in.

    During the rebirth the bird has no bar: everything landed on the core is spent against
    the SECOND life, which is why it is capped against that one.
    """
    if not core and int(state["boss_hp"]) <= 0:
        return 0
    dealt = _hero_hit(state, rng, _hit("magic", share), cap_hp=cap_hp, spent=spent)
    if dealt <= 0:
        return 0
    if core:
        rebirth = dict(state.get("rebirth") or {})
        rebirth["core_damage"] = int(rebirth.get("core_damage", 0) or 0) + dealt
        state["rebirth"] = rebirth
        state["log"].append(f"Ядро тускнеет на {dealt}." if not note else f"{note}: {dealt}.")
        return dealt
    state["boss_hp"] = max(0, int(state["boss_hp"]) - dealt)
    state["log"].append(f"Феникс теряет {dealt} здоровья." if not note else f"{note}: {dealt}.")
    return dealt


def _spell_support(state: dict, spell: dict | None) -> float:
    """Everything the scroll does FOR the hero before the Phoenix's answer lands.

    Returns the barrier, because that one is not a number the hero gains but a number the
    incoming hit loses -- it has to be known before the hit is dealt, not after.
    """
    if not spell:
        return 0.0
    heal = float(spell.get("heal", 0.0) or 0.0)
    if heal > 0:
        healed = _heal_hero(state, state["hero"]["max_hp"] * heal)
        if healed:
            state["log"].append(f"💚 Свиток восстанавливает {healed} здоровья.")
    return max(0.0, float(spell.get("shield", 0.0) or 0.0)) * state["hero"]["max_hp"]


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


def _cast_line(state: dict, spell: dict) -> None:
    """The cast itself, and what it leaves on the shelf.

    The count is in the log rather than on a button because it is a fact about the fight,
    and the buttons already say which scrolls are left by simply not offering the rest.
    """
    left = len(_unspent(state))
    tail = f"осталось свитков: {left}" if left else "свитки кончились"
    state["log"].append(f"✨ {spell['icon']} {spell['name']} — {tail}.")


def _apply(state: dict, row: dict, action: str, rng: random.Random,
           spell: dict | None = None) -> str:
    """Everything one graded answer does, in the order the player sees it happen.

    Returns the vulnerability window this exchange opened, which a scroll can create out
    of nothing: a stunned Phoenix loses its next move, and a lost move IS a window.
    """
    attack = _ATTACKS.get(str(state.get("attack") or "")) or {}
    # Kept for the screen rather than for the maths: what the last answer was WORTH is
    # the one thing a player cannot work out from the numbers alone. A hero losing 2,073
    # health reads the same whether they mistimed a block or walked into the one move
    # that punishes blocking, and only the second is a lesson worth marking.
    state["grade"] = str(row.get("grade") or "")
    # The cast is announced before the outcome: the player chose the scroll, and the note
    # underneath is the Phoenix answering it.
    if spell is not None:
        _cast_line(state, spell)
    if row.get("note"):
        state["log"].append(str(row["note"]))

    hit = row.get("hit")
    if spell is not None:
        _spell_strike(state, rng, row, spell)
    elif hit and int(state["boss_hp"]) > 0:
        dealt = _hero_hit(state, rng, hit)
        state["boss_hp"] = max(0, int(state["boss_hp"]) - dealt)
        state["log"].append(f"Феникс теряет {dealt} здоровья.")

    heal = float(row.get("heal", 0.0) or 0.0)
    if heal > 0 and int(state["boss_hp"]) > 0:
        healed = round(state["boss_max_hp"] * heal)
        before = int(state["boss_hp"])
        state["boss_hp"] = min(int(state["boss_max_hp"]), before + healed)
        state["log"].append(f"Феникс восстанавливает {state['boss_hp'] - before} здоровья.")

    absorb = _spell_support(state, spell)
    ordinary = _ordinary_damage(
        state, float(row.get("ordinary", 0.0) or 0.0),
        shielded=(action == SHIELD), pierces=bool(attack.get("ignores_defence")),
    )
    mistake = _mistake_damage(state, float(row.get("mistake", 0.0) or 0.0))
    cap = state["hero"]["max_hp"] * SINGLE_HIT_HERO_CAP_SHARE
    incoming = min(ordinary + mistake, cap)
    blocked = min(incoming, absorb)
    if blocked >= 1:
        state["log"].append(f"🛡 Барьер свитка гасит {round(blocked)} урона.")
    dealt = _hurt_hero(state, incoming - blocked)
    if dealt:
        state["log"].append(f"Герой теряет {dealt} здоровья.")

    _clear_burn(state, int(row.get("cleanse") or 0), spell)
    if row.get("burn"):
        state["burn"] = min(BURN_MAX_STACKS, int(state.get("burn", 0) or 0) + int(row["burn"]))
        state["log"].append(f"🔥 Горение: x{state['burn']}")

    if row.get("grade") == BAD:
        state["mistake_streak"] = int(state.get("mistake_streak", 0) or 0) + 1
        # The streak resets; this does not. A flawless run is a thing somebody claims
        # afterwards, and it cannot be reconstructed from a state that only remembers
        # how the last few answers went.
        state["mistakes"] = int(state.get("mistakes", 0) or 0) + 1
    else:
        state["mistake_streak"] = 0

    window = str(row.get("window") or "")
    if spell and spell.get("stun"):
        window = SPELL_STUN_WINDOW
        state["log"].append("Феникс сбивается и пропускает движение.")
    return window


def _clear_burn(state: dict, rows_worth: int, spell: dict | None) -> None:
    """Горение comes off. A scroll's cleanse takes all of it; an answer takes its share."""
    stacks = int(state.get("burn", 0) or 0)
    if not stacks:
        return
    cleared = stacks if (spell and spell.get("cleanse")) else min(stacks, max(0, rows_worth))
    if cleared:
        state["burn"] = stacks - cleared
        state["log"].append(f"🔥 Горение спадает (-{cleared}).")


# --------------------------------------------------------------------------- переходы
def _resolve_telegraph(state: dict, action: str, rng: random.Random,
                       spell: dict | None = None) -> None:
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

    row = _grade(state, action, spell)
    _advance(state, _apply(state, row, action, rng, spell), rng)


def _resolve_window(state: dict, action: str, rng: random.Random,
                    spell: dict | None = None) -> None:
    """A guaranteed blow at an open Phoenix, with no answer coming back.

    Burning does not tick here on purpose: a window is time stolen from the boss, not a
    turn it was given, and charging the player twice per opening would make the reward
    for a perfect read smaller than the reward for a safe one.
    """
    multiplier = (VULNERABLE_FULL_MULTIPLIER if str(state.get("vulnerable")) == "full"
                  else VULNERABLE_SMALL_MULTIPLIER)
    kind = "magic" if action == MAGIC else "physical"
    # A scroll poured into an open Phoenix is that scroll: its own damage multiplier
    # rides the window's, and everything else it does -- the heal, the barrier, the
    # cleanse -- still happens. Spending one here used to be impossible, which quietly
    # made the best moment in the fight the one moment the loadout could not reach.
    share = float((spell or {}).get("damage", 0.0) or 0.0) if spell else 1.0
    if spell is not None:
        _cast_line(state, spell)
        _spell_support(state, spell)
        if spell.get("cleanse") and int(state.get("burn", 0) or 0):
            state["log"].append(f"🔥 Горение снято (-{int(state['burn'])}).")
            state["burn"] = 0
    dealt = 0
    if share > 0:
        dealt = _hero_hit(state, rng, _hit(kind, share), multiplier=multiplier)
        state["boss_hp"] = max(0, int(state["boss_hp"]) - dealt)
    state["log"].append(
        f"💥 Открытый удар: Феникс теряет {dealt} здоровья." if dealt
        else "💥 Открытый ход: удара не было."
    )
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


def _resolve_rebirth(state: dict, action: str, rng: random.Random,
                     spell: dict | None = None) -> None:
    _burn_tick(state)
    if int(state["hero_hp"]) <= 0:
        _defeat(state)
        return

    code = str(state.get("attack") or "")
    row = _ATTACKS[code]["answers"][action]
    if spell is not None:
        _cast_line(state, spell)
    if row.get("note"):
        state["log"].append(str(row["note"]))

    hit = row.get("hit")
    if spell is not None:
        _spell_strike(state, rng, row, spell, core=True)
        if spell.get("stun"):
            # The core does not move, so there is no movement to break. The scroll is
            # still gone: the interlude is where a mistimed loadout gets punished.
            state["log"].append("Ядро неподвижно — сбивать нечего.")
    elif hit:
        # The core has no bar of its own; a hit here is spent on the SECOND life's health,
        # which is why it is capped against phase two rather than against phase one.
        dealt = _hero_hit(state, rng, hit, cap_hp=int(state["phase_2_max"]))
        rebirth = dict(state.get("rebirth") or {})
        rebirth["core_damage"] = int(rebirth.get("core_damage", 0) or 0) + dealt
        state["rebirth"] = rebirth
        state["log"].append(f"Ядро тускнеет на {dealt}.")

    absorb = _spell_support(state, spell)
    ordinary = _ordinary_damage(state, float(row.get("ordinary", 0.0) or 0.0),
                                shielded=(action == SHIELD), pierces=False)
    mistake = _mistake_damage(state, float(row.get("mistake", 0.0) or 0.0))
    cap = state["hero"]["max_hp"] * SINGLE_HIT_HERO_CAP_SHARE
    incoming = min(ordinary + mistake, cap)
    blocked = min(incoming, absorb)
    if blocked >= 1:
        state["log"].append(f"🛡 Барьер свитка гасит {round(blocked)} урона.")
    dealt = _hurt_hero(state, incoming - blocked)
    if dealt:
        state["log"].append(f"Герой теряет {dealt} здоровья.")
    _clear_burn(state, int(row.get("cleanse") or 0), spell)
    if row.get("burn"):
        state["burn"] = min(BURN_MAX_STACKS, int(state.get("burn", 0) or 0) + int(row["burn"]))
    state["mistake_streak"] = (int(state.get("mistake_streak", 0) or 0) + 1
                               if row.get("grade") == BAD else 0)

    rebirth = dict(state.get("rebirth") or {})
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
