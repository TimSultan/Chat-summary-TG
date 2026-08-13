"""Live shield items for the pet arena.

The numerical Defend properties intentionally use the same small operation vocabulary as
the scroll table.  ``pets_combat`` interprets the data; shops, drops and inventory only
need ordinary item dictionaries.
"""

from __future__ import annotations

import pets_scroll_catalog as scrolls


_ITEM_RULES = {
    "shield_paper_buckler": {
        "price": 70, "source": "shop", "rarity": "common",
        "bonuses": {"armor": 10, "health": 3}, "drop_weight": 0,
    },
    "shield_ink_lid": {
        "price": 70, "source": "shop", "rarity": "common",
        "bonuses": {"armor": 16, "agility": -2}, "drop_weight": 0,
    },
    "shield_tower": {
        "price": 165, "source": "shop", "rarity": "rare",
        "bonuses": {"armor": 35, "agility": -6}, "drop_weight": 0,
    },
    "shield_mirror": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 96, "drop_weight": 1,
        "bonuses": {"armor": 18, "luck": 6},
    },
    "shield_thorn": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 25, "health": -3},
    },
    "shield_frost": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 22, "strength": 4},
    },
    "shield_palette": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 14, "luck": 8},
    },
    "shield_lantern": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 15, "health": 8},
    },
    "shield_kite": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 96, "drop_weight": 1,
        "bonuses": {"armor": 20, "agility": 12, "health": -5},
    },
    "shield_rune": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 96, "drop_weight": 1,
        "bonuses": {"armor": 34, "health": 10, "agility": -5},
    },
    "shield_clothespin": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 10, "strength": 7, "luck": 7},
    },
    "shield_forge_clamp": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 96, "drop_weight": 1,
        "bonuses": {"armor": 16, "strength": 10, "luck": 10},
    },
    "shield_solvent_jar": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 20, "strength": 6},
    },
    "shield_solvent_drum": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 96, "drop_weight": 1,
        "bonuses": {"armor": 28, "strength": 12},
    },
}


RAW_ITEMS = tuple(
    {
        "code": shield["code"],
        "name": shield["name"],
        "slot": "shield",
        "description": shield["short"],
        "effect": {
            "code": "defend_effect",
            "text": shield["short"],
            "guard": shield.get("guard", .40),
            "defend_effects": tuple(dict(effect) for effect in shield.get("defend_effects", ())),
        },
        **_ITEM_RULES[shield["code"]],
    }
    for shield in scrolls.SHIELDS
)


def _validate() -> None:
    codes = [row["code"] for row in RAW_ITEMS]
    if len(codes) != 14 or len(set(codes)) != 14:
        raise ValueError("shield catalogue must contain 14 unique items")
    if set(codes) != set(_ITEM_RULES):
        raise ValueError("every shield needs live item rules")
    if sum(row["source"] == "shop" for row in RAW_ITEMS) != 3:
        raise ValueError("shield shop must contain exactly three items")
    if any(row["slot"] != "shield" for row in RAW_ITEMS):
        raise ValueError("shield catalogue contains another slot")


_validate()
