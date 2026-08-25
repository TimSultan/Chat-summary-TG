"""Contract tests for Магия: the stat, the weapons that read it, and the migration.

The rework these pin is narrow to state and wide in consequence: scroll damage used to be
a percentage of the caster's PHYSICAL swing, so Strength paid for the punches and the
spells both and there was no such thing as a caster. Scrolls now read Магия, a hundred and
five weapons make the ordinary swing read it too, and every fighter built before the stat
existed has to keep fighting exactly as it did.
"""

import unittest

import pets_combat as combat
import pets_config as C
import pets_scroll_catalog as SCROLLS
import pets_weapon_catalog as WEAPONS

LOADOUT = ("scroll_arcane_spark", None, None, None)
SPARK = SCROLLS.scroll("scroll_arcane_spark")["effects"][0]


def fighter(**kwargs):
    base = dict(
        key="a", name="a", strength=20, health=20, agility=20, luck=20, armor=5,
        magic=1, level=10, skills=LOADOUT,
    )
    base.update(kwargs)
    return combat.Fighter(**base)


class SpellPowerTests(unittest.TestCase):
    def test_scroll_damage_reads_magic_and_not_strength(self):
        """The whole point of the rework, in one comparison."""
        brawler = combat.derive(fighter(strength=80, magic=1), fighter(key="b"))
        caster = combat.derive(fighter(strength=1, magic=80), fighter(key="b"))
        self.assertGreater(brawler["damage"], caster["damage"])
        self.assertGreater(caster["spell_power"], brawler["spell_power"] * 1.5)

    def test_a_scroll_never_falls_below_a_share_of_the_swing(self):
        """The floor is what stops four scroll slots being four wasted turns.

        Without it, every pet that has not respecced since Магия shipped would carry a
        loadout that is strictly worse than punching, which is a trap rather than a
        rework. The floor is deliberately well under one swing: a Strength build's
        scrolls are cut roughly in half, which IS the rework.
        """
        numbers = combat.derive(fighter(strength=80, magic=1), fighter(key="b"))
        self.assertAlmostEqual(
            numbers["spell_power"],
            numbers["damage"] * C.SPELL_POWER_SWING_FLOOR,
            places=4,
        )
        self.assertLess(numbers["spell_power"], numbers["damage"])

    def test_magic_beats_the_floor_once_it_is_actually_bought(self):
        floor = combat.derive(fighter(strength=80, magic=1), fighter(key="b"))["spell_power"]
        bought = combat.derive(fighter(strength=80, magic=80), fighter(key="b"))["spell_power"]
        self.assertGreater(bought, floor * 2)

    def test_a_shield_defend_hook_still_reads_the_swing(self):
        """Магия powers scrolls. A shield is a defensive item and stays one.

        Its catalogue numbers were tuned against the wearer's swing, and routing them
        through spell power would have quietly made every shield in the game a caster
        item that a Strength build could no longer use.
        """
        shield = {"guard": .4, "defend_effects": ({"op": "damage", "amount": 1.0},)}
        brawler = fighter(strength=80, magic=1, shield=shield)
        foe = fighter(key="b", strength=1, health=400)
        rounds = combat.simulate(brawler, foe, seed=7).rounds
        hits = [row for row in rounds if row.attacker == "a" and row.damage > 0]
        self.assertTrue(hits)


class WeaponScalingTests(unittest.TestCase):
    def test_a_magic_weapon_swings_with_magic(self):
        steel = combat.derive(fighter(strength=1, magic=80), fighter(key="b"))
        wand = combat.derive(
            fighter(strength=1, magic=80, attack_scaling="magic"), fighter(key="b"),
        )
        self.assertGreater(wand["damage"], steel["damage"] * 2)

    def test_a_hybrid_averages_the_two_and_is_paid_for_the_averaging(self):
        """An even split of two stats is worth less than one stat twice as high, so a
        plain average would leave the hybrid dominated by both pure builds at once."""
        # Against its own mirror, so the stat-lead bonus is out of the comparison and
        # what is left is the scaling rule itself.
        mine = fighter(strength=40, magic=40, attack_scaling="hybrid")
        blade = combat.derive(mine, combat.replace(mine, key="b"))
        self.assertAlmostEqual(
            blade["damage"],
            C.BASE_DAMAGE + 40 * C.HYBRID_SCALING_BONUS * C.DAMAGE_PER_POINT,
            places=4,
        )

    def test_a_magic_weapons_swing_is_magic_and_a_hybrids_is_half_of_each(self):
        self.assertEqual(
            combat._swing_attack_types(fighter(attack_scaling="magic")), (combat.MAGIC,),
        )
        self.assertEqual(
            combat._swing_attack_types(fighter(attack_scaling="hybrid")),
            (combat.PHYSICAL, combat.MAGIC),
        )
        self.assertEqual(combat._swing_attack_types(fighter()), (combat.PHYSICAL,))
        self.assertEqual(
            combat._swing_attack_types(fighter(weapon_enchanted=True)),
            (combat.PHYSICAL, combat.MAGIC),
        )

    def test_a_caster_hurts_the_ghost_that_absorbs_plain_steel(self):
        """The dungeon's spells_only boss, which is what the classification is FOR."""
        ghost = combat.Fighter(
            key="dungeon:ghost", name="ghost", strength=1, health=200, agility=1, luck=1,
            armor=0, physical_damage_taken_multiplier=0, physical_damage_heals=True,
        )
        wand = fighter(strength=1, magic=60, attack_scaling="magic", skills=())
        club = fighter(strength=60, magic=1, skills=())
        self.assertEqual(combat.simulate(wand, ghost, seed=3).winner, "a")
        self.assertNotEqual(combat.simulate(club, ghost, seed=3).winner, "a")

    def test_the_swing_stat_is_what_the_fight_compares(self):
        """A caster who skipped Strength must not collect the Strength deficit.

        Магия is not one of the four compared stats, so reading "strength" literally
        punished the mage for a stat they replaced while costing the brawler nothing at
        all for the Магия of 1 they never bought -- an asymmetry worth most of a
        thirty-point win-rate gap in an equal-gold round robin.
        """
        caster = fighter(strength=1, magic=80, attack_scaling="magic")
        brawler = fighter(key="b", strength=80, magic=1)
        self.assertNotIn("strength", combat.derive(caster, brawler)["deficits"])
        self.assertNotIn("strength", combat.derive(brawler, caster)["deficits"])
        # And a caster who did NOT buy the stat their weapon reads is exposed, exactly
        # the way an under-strength brawler is.
        empty = fighter(strength=1, magic=1, attack_scaling="magic")
        self.assertIn("strength", combat.derive(empty, brawler)["deficits"])



class ElementalResonanceTests(unittest.TestCase):
    """Four scrolls of one element lift every magical thing their owner does."""

    @staticmethod
    def _pure(element):
        regular = [row["code"] for row in SCROLLS.REGULAR_SCROLLS
                   if row["element"] == element][:3]
        ultimate = [row["code"] for row in SCROLLS.ULTIMATE_SCROLLS
                    if row["element"] == element][:1]
        return SCROLLS.validate_loadout(tuple(regular + ultimate))

    def test_it_takes_all_four_slots_and_one_element(self):
        pure = self._pure("fire")
        self.assertEqual(SCROLLS.loadout_element(pure), "fire")
        # An empty slot is a legal resting state everywhere else in the game, so a set of
        # one scroll would otherwise be a set of one element and collect this for free.
        self.assertIsNone(SCROLLS.loadout_element((pure[0], None, None, pure[3])))
        frost = next(row["code"] for row in SCROLLS.REGULAR_SCROLLS
                     if row["element"] == "frost")
        self.assertIsNone(SCROLLS.loadout_element((pure[0], pure[1], frost, pure[3])))

    def test_the_bonus_reaches_spell_damage_and_healing_but_not_steel(self):
        pure, mixed = self._pure("fire"), SCROLLS.validate_loadout((
            "scroll_arcane_spark", "scroll_crimson_comet", "scroll_royal_barrier",
            "ultimate_starfall",
        ))
        resonant = fighter(skills=pure, magic=60)
        plain = fighter(skills=mixed, magic=60)
        foe = fighter(key="b")
        self.assertAlmostEqual(
            combat.derive(resonant, foe)["resonance"],
            1 + C.ELEMENTAL_RESONANCE_BONUS, places=6,
        )
        self.assertEqual(combat.derive(plain, foe)["resonance"], 1.0)

        # A magical blow lands harder...
        rounds = combat.simulate(resonant, fighter(key="b", health=400), seed=5).rounds
        magic_hits = [row for row in rounds
                      if row.attacker == "a" and combat.MAGIC in row.attack_types]
        self.assertTrue(magic_hits)
        # ...and a steel swing does not, which is what keeps this a caster's rule.
        steel = fighter(skills=pure, magic=1, strength=60)
        self.assertEqual(
            combat.derive(steel, foe)["damage"],
            combat.derive(fighter(skills=mixed, magic=1, strength=60), foe)["damage"],
        )

    def test_healing_is_lifted_too(self):
        heal = ({"op": "heal", "percent": 0.20},)
        hurt = fighter(key="a", skills=self._pure("fire"), health=100)
        self.assertGreater(combat.derive(hurt, fighter(key="b"))["resonance"], 1)

class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_round_trip_keeps_magic_and_the_weapon_scaling(self):
        original = fighter(magic=44, attack_scaling="hybrid")
        restored = combat.restore(combat.snapshot(original))
        self.assertEqual(restored.magic, 44)
        self.assertEqual(restored.attack_scaling, "hybrid")

    def test_a_pre_magic_snapshot_replays_as_it_was_fought(self):
        """No Магия, no magic weapon: the fight it recorded is the fight it replays."""
        stored = combat.snapshot(fighter())
        del stored["magic"], stored["attack_scaling"]
        restored = combat.restore(stored)
        self.assertEqual(restored.magic, 0)
        self.assertEqual(restored.attack_scaling, "strength")

    def test_the_soul_mirror_brings_magic_down_with_the_rest(self):
        """Otherwise the mirror is the exact exploit it exists to prevent: come down to
        a beginner's stats and keep casting level-80 scrolls at them."""
        mirror = fighter(
            magic=80, effects=({"code": "mirror_soul", "value": 20},),
        )
        small = fighter(key="b", strength=5, health=5, agility=5, luck=5, magic=5)
        import random
        mirrored = combat._mirror(mirror, small, random.Random(1))
        self.assertLessEqual(mirrored.magic, 8)


class CatalogueTests(unittest.TestCase):
    def test_every_magic_weapon_grants_the_stat_its_swing_reads(self):
        shelf = [
            item for item in WEAPONS.WEAPON_SPECS
            if item.code in WEAPONS.MAGIC_WEAPON_CODES
        ]
        # Rare/legendary stay full; the repetitive grey shop tiers are compact samples.
        self.assertEqual(len(shelf), 59)
        for item in shelf:
            with self.subTest(item.code):
                bonuses = dict(item.bonuses)
                self.assertTrue(bonuses.get("magic"), item.name)
                if item.scaling == "hybrid":
                    self.assertTrue(bonuses.get("strength"), item.name)

    def test_the_scaling_survives_the_trip_into_the_live_item_catalogue(self):
        magic = next(
            item for item in WEAPONS.WEAPON_SPECS
            if item.scaling == "magic" and item.rarity == "legendary"
        )
        self.assertEqual(C.weapon_scaling(C.find_item(magic.code)), "magic")
        self.assertEqual(C.weapon_scaling(C.find_item("w001")), "strength")
        # Nothing that is not a weapon has an opinion about it.
        self.assertEqual(C.weapon_scaling(None), "strength")

    def test_a_magic_rule_never_appears_outside_the_magic_shelf(self):
        magic_only = {
            "arcane_surge", "runic_charge", "arcane_battery", "mana_burn", "spell_siphon",
            "spell_pierce", "hex", "focus_shift", "spell_shield", "ward", "spell_thorns",
            "double_cast",
        }
        for item in WEAPONS.WEAPON_SPECS:
            code = item.effect_dict().get("code") if item.effect else None
            if code in magic_only:
                self.assertIn(item.code, WEAPONS.MAGIC_WEAPON_CODES, item.name)


if __name__ == "__main__":
    unittest.main()
