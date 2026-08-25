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
    "shield_duelist_buckler": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 24, "agility": 4},
    },
    "shield_medic_emblem": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 26, "health": 10},
    },
    "shield_spiked_targe": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 28, "strength": 5},
    },
    "shield_royal_riposte": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 140, "drop_weight": 1,
        "bonuses": {"armor": 36, "luck": 10},
    },
    "shield_crimson_reliquary": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 140, "drop_weight": 1,
        "bonuses": {"armor": 38, "health": 15, "agility": -4},
    },
    "shield_judgement": {
        "price": 0, "source": "drop", "rarity": "legendary",
        "resale_price": 140, "drop_weight": 1,
        "bonuses": {"armor": 30, "strength": 13, "luck": 6},
    },
    # --- the second shelf --------------------------------------------------------------
    # Eight ordinary and six rare, all drops. Priced and weighted exactly like the ordinary
    # and rare shields already here, because the problem being fixed is that there were too
    # FEW of them -- not that the ones there were paid wrongly.
    "shield_pot_lid": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 18, "health": 2},
    },
    "shield_book_cover": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 16, "luck": 3},
    },
    "shield_tray": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 20, "health": 4},
    },
    "shield_road_sign": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 24, "agility": -3},
    },
    "shield_manhole": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 30, "agility": -7},
    },
    "shield_umbrella": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 12, "agility": 4},
    },
    "shield_baking_sheet": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 17, "strength": 3},
    },
    "shield_cutting_board": {
        "price": 0, "source": "drop", "rarity": "common",
        "resale_price": 48, "drop_weight": 4,
        "bonuses": {"armor": 13, "strength": 5},
    },
    "shield_riot_pane": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 26, "health": 6},
    },
    "shield_welding_mask": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 27, "agility": -2},
    },
    "shield_scaffold_plank": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 21, "strength": 7},
    },
    "shield_sewer_grate": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 22, "strength": 6},
    },
    "shield_iron_skillet": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 25, "strength": 8},
    },
    "shield_thermos_cap": {
        "price": 0, "source": "drop", "rarity": "rare",
        "resale_price": 70, "drop_weight": 3,
        "bonuses": {"armor": 24, "health": 8},
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
            "on_hit_effects": tuple(dict(effect) for effect in shield.get("on_hit_effects", ())),
        },
        **_ITEM_RULES[shield["code"]],
    }
    for shield in scrolls.SHIELDS
)


# Below these the slot stops working rather than merely feeling thin: the daily shelf
# offers three ordinary items and never repeats what you own, and the forge asks for four
# ordinary or five rare shields of this slot alone. A catalogue that cannot cover both
# leaves a shelf with holes in it and a recipe nobody can finish.
# Three daily offers plus four distinct forge ingredients still leave one spare design;
# more grey variants than that only dilute the interesting shield drops.
MIN_ORDINARY_SHIELDS = 8
MIN_RARE_SHIELDS = 10


def _validate() -> None:
    codes = [row["code"] for row in RAW_ITEMS]
    if len(codes) != len(set(codes)):
        raise ValueError("shield catalogue contains a duplicate code")
    if set(codes) != set(_ITEM_RULES):
        raise ValueError("every shield needs live item rules")
    if sum(row["source"] == "shop" for row in RAW_ITEMS) < 3:
        raise ValueError("shield shop must contain at least three items")
    by_rarity = {}
    for row in RAW_ITEMS:
        by_rarity[row["rarity"]] = by_rarity.get(row["rarity"], 0) + 1
    if by_rarity.get("common", 0) < MIN_ORDINARY_SHIELDS:
        raise ValueError("too few ordinary shields to fill a shelf or a forge recipe")
    if by_rarity.get("rare", 0) < MIN_RARE_SHIELDS:
        raise ValueError("too few rare shields to fill a shelf or a forge recipe")
    if any(row["slot"] != "shield" for row in RAW_ITEMS):
        raise ValueError("shield catalogue contains another slot")


_validate()
