import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot_api
import bot_listener
import listener
import preview
import stats
import tree

# The chat's own measured output: ~3,600 XP a day across everybody.
MEASURED_DAILY_XP = 3_600


class GrowthTests(unittest.TestCase):
    def test_the_final_stage_lands_at_about_three_years(self):
        three_years = MEASURED_DAILY_XP * 365 * 3
        number, _, name = tree.tree_stage(three_years)

        self.assertEqual(number, len(tree.TREE_STAGES))
        self.assertEqual(name, "Легендарное Древо ЕПХ")
        # ...and not appreciably sooner: two and a half years must still be short of it.
        earlier, _, _ = tree.tree_stage(MEASURED_DAILY_XP * 365 * 2.5)
        self.assertLess(earlier, len(tree.TREE_STAGES))

    def test_everyone_starts_from_a_seed(self):
        number, _, name = tree.tree_stage(0)
        self.assertEqual((number, name), (1, "Семечко"))
        self.assertEqual(tree.tree_height_mm(0), 0)

    def test_stages_are_ordered_and_never_go_backwards(self):
        heights = [minimum for minimum, _, _, _ in tree.TREE_STAGES]
        self.assertEqual(heights, sorted(heights))
        self.assertEqual(len(set(heights)), len(heights))

        seen = 0
        for days in range(0, 1200, 7):
            number, _, _ = tree.tree_stage(MEASURED_DAILY_XP * days)
            self.assertGreaterEqual(number, seen)
            seen = number

    def test_height_is_capped_so_the_tree_cannot_outgrow_its_last_name(self):
        # XP accrues forever; without the cap the tree would keep climbing past any
        # stage anybody has a word for.
        self.assertEqual(tree.tree_height_mm(10**12), tree.TREE_MAX_HEIGHT_MM)
        number, _, _ = tree.tree_stage(10**12)
        self.assertEqual(number, len(tree.TREE_STAGES))

    def test_a_days_growth_tracks_how_busy_the_chat_was(self):
        base = MEASURED_DAILY_XP * 100
        quiet = tree.tree_height_mm(base + 1_600) - tree.tree_height_mm(base)
        busy = tree.tree_height_mm(base + 5_600) - tree.tree_height_mm(base)

        self.assertGreater(busy, quiet)
        # An ordinary day is a readable number of millimetres, not 0 and not a metre.
        ordinary = tree.tree_height_mm(base + MEASURED_DAILY_XP) - tree.tree_height_mm(base)
        self.assertGreaterEqual(ordinary, 5)
        self.assertLessEqual(ordinary, 60)

    def test_next_stage_counts_down_and_stops_at_the_top(self):
        name, remaining = tree.next_stage(0)
        self.assertEqual(name, "Росток")
        self.assertGreater(remaining, 0)
        self.assertIsNone(tree.next_stage(10**12))

    def test_lengths_read_in_the_unit_that_suits_them(self):
        self.assertEqual(tree.format_length(7), "7 мм")
        self.assertEqual(tree.format_length(612), "61,2 см")
        self.assertEqual(tree.format_length(19_710), "19,71 м")
        # A day's growth stays in millimetres far longer -- "18 мм" reads as progress.
        self.assertEqual(tree.format_growth(18), "18 мм")
        self.assertEqual(tree.format_growth(0), "0 мм")


class AdviceTests(unittest.TestCase):
    def test_there_are_120_distinct_lines(self):
        self.assertEqual(len(tree.DAILY_ADVICE), 120)
        self.assertEqual(len(set(tree.DAILY_ADVICE)), 120)

    def test_every_line_is_a_usable_sentence(self):
        for line in tree.DAILY_ADVICE:
            with self.subTest(line=line):
                self.assertTrue(line.strip())
                self.assertLess(len(line), 200)
                self.assertEqual(line, line.strip())
                self.assertTrue(line[0].isupper())
                self.assertIn(line[-1], ".!?")

    def test_the_rotation_avoids_harsh_failure_language(self):
        text = " ".join(tree.DAILY_ADVICE).lower()
        for negative in ("неудач", "провал", "стыдно", "кривой", "испорчен", "уродлив"):
            with self.subTest(negative=negative):
                self.assertNotIn(negative, text)

    def test_the_same_day_always_gives_the_same_line(self):
        # It is a shared greeting, not a personal fortune: everybody must see one line,
        # and a restart must not change it halfway through the morning.
        day = date(2026, 7, 26)
        self.assertEqual(tree.advice_for(day), tree.advice_for(day))
        self.assertNotEqual(tree.advice_for(day), tree.advice_for(day + timedelta(days=1)))

    def test_no_repeat_for_a_full_rotation(self):
        start = date(2026, 1, 1)
        picked = [tree.advice_for(start + timedelta(days=offset)) for offset in range(120)]
        self.assertEqual(len(set(picked)), 120)
        # Day 121 comes back round to the first.
        self.assertEqual(tree.advice_for(start + timedelta(days=120)), picked[0])


class DigestTests(unittest.TestCase):
    CONTRIBUTORS = [
        ("Первый", "first", 423),
        ("Второй", "second", 383),
        ("Третий", "third", 326),
        ("Четвёртый", "fourth", 12),
    ]

    def _digest(self, **kwargs):
        params = dict(
            total_xp=MEASURED_DAILY_XP * 100,
            yesterday_xp=MEASURED_DAILY_XP,
            contributors=self.CONTRIBUTORS,
            day=date(2026, 7, 26),
        )
        params.update(kwargs)
        return tree.format_morning_digest(**params)

    def test_it_greets_reports_growth_and_names_the_top_three(self):
        text = self._digest()

        self.assertIn("Доброе утро, ЕПХ-чане!", text)
        self.assertIn("Вчера наше дерево подросло на", text)
        self.assertIn("@first — 423 XP", text)
        self.assertIn("@third — 326 XP", text)
        # Only three, so the fourth is not named.
        self.assertNotIn("fourth", text)
        self.assertIn("Идея на день", text)

    def test_a_member_without_a_username_is_named_and_escaped(self):
        text = self._digest(contributors=[("<Худож & ник>", None, 100)])
        self.assertIn("&lt;Худож &amp; ник&gt;", text)
        self.assertNotIn("<Худож", text)

    def test_nobody_earning_anything_drops_the_whole_block(self):
        text = self._digest(yesterday_xp=0, contributors=[("Никто", "nobody", 0)])
        self.assertNotIn("Особенно помогли дереву вырасти", text)
        self.assertIn("Вчера высота дерева не изменилась", text)
        # The greeting and the advice still go out -- it is a morning post, not a report.
        self.assertIn("Доброе утро", text)
        self.assertIn("Идея на день", text)

    def test_the_growth_period_is_explicitly_yesterday(self):
        text = self._digest()
        self.assertIn("Вчера наше дерево", text)
        self.assertNotIn("Сегодня наше дерево", text)

    def test_the_countdown_disappears_at_the_final_stage(self):
        topped_out = self._digest(total_xp=10**12)
        self.assertNotIn("До стадии", topped_out)
        self.assertIn("Легендарное Древо ЕПХ", topped_out)


class StageImageTests(unittest.TestCase):
    """One picture per stage, dropped into assets/tree_stages by hand."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self._temporary.name)
        patcher = patch.object(tree, "TREE_IMAGE_DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _put(self, filename):
        (self.folder / filename).write_bytes(b"not really a picture")

    def test_every_stage_has_its_own_ascii_slug(self):
        slugs = [slug for _, _, _, slug in tree.TREE_STAGES]
        self.assertEqual(len(set(slugs)), len(slugs))
        for slug in slugs:
            self.assertTrue(slug.isascii())
            self.assertEqual(slug, slug.lower())

    def test_an_empty_folder_simply_means_no_picture(self):
        # The whole feature is optional: the post still goes out, as text.
        self.assertIsNone(tree.stage_image(0))
        self.assertEqual(len(tree.missing_stage_images()), len(tree.TREE_STAGES))

    def test_a_missing_folder_is_not_an_error(self):
        with patch.object(tree, "TREE_IMAGE_DIR", self.folder / "nope"):
            self.assertIsNone(tree.stage_image(0))

    def test_the_picture_for_the_current_stage_is_the_one_picked(self):
        self._put("01_seed.png")
        self._put("04_sapling.jpg")

        self.assertEqual(tree.stage_image(0).name, "01_seed.png")
        # ~50 cm of tree: the Саженец stage.
        self.assertEqual(tree.stage_image(500 * tree.TREE_XP_PER_MM).name, "04_sapling.jpg")
        # Nothing uploaded for the stage in between.
        self.assertIsNone(tree.stage_image(200 * tree.TREE_XP_PER_MM))
        self.assertNotIn("01_seed", tree.missing_stage_images())

    def test_extension_and_case_are_whatever_the_uploader_had(self):
        """A file saved as SEED.JPG resolves on Windows either way; on the Linux host it
        would not, and the only symptom would be a missing picture at 10:00."""
        self._put("01_SEED.JPG")
        self.assertEqual(tree.stage_image(0).name, "01_SEED.JPG")

    def test_an_unrelated_file_in_the_folder_is_ignored(self):
        self._put("README.md")
        self._put("01_seed.txt")
        self.assertIsNone(tree.stage_image(0))

    def test_the_morning_post_always_fits_in_a_caption(self):
        """It travels as a photo caption, and Telegram rejects one over 1024 characters
        outright -- so the longest possible post has to clear the limit, not the average
        one."""
        longest = max(len(name) for _, _, name, _ in tree.TREE_STAGES)
        contributors = [("Ф" * 64, None, 4_321)] * tree.TOP_CONTRIBUTORS_SHOWN
        for offset in range(len(tree.DAILY_ADVICE)):
            day = date.fromordinal(date(2026, 1, 1).toordinal() + offset)
            text = tree.format_morning_digest(360_000, 3_600, contributors, day)
            self.assertLessEqual(len(text), bot_api.CAPTION_LIMIT, f"advice {offset}")
        self.assertGreater(longest, 0)

    def test_the_greeting_carries_the_stage_it_is_reporting(self):
        seed = tree.format_morning_digest(0, 0, [], date(2026, 7, 26))
        grown = tree.format_morning_digest(360_000, 3_600, [], date(2026, 7, 26))

        self.assertTrue(seed.startswith("🌰"))
        self.assertTrue(grown.startswith(tree.tree_stage(360_000)[1]))


class DigestDeliveryTests(unittest.TestCase):
    """The morning post must survive every way its picture can be missing or broken.

    This is the code path that decides whether the chat gets a post at all, so each of
    these is "the picture is gone, is the text still there?" rather than a formatting
    check.
    """

    class FakeAPI:
        def __init__(self, photo_error=None):
            self.messages = []
            self.photos = []
            self.photo_error = photo_error

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                               reply_markup=None, parse_mode=None):
            self.messages.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
            return {"message_id": 1}

        async def send_photo_file(self, chat_id, path, caption=None, reply_to_message_id=None,
                                  reply_markup=None, parse_mode=None):
            if self.photo_error:
                raise self.photo_error
            self.photos.append({"chat_id": chat_id, "path": path, "caption": caption})
            return {"message_id": 2}

    def _send(self, photo, text="Доброе утро", api=None):
        api = api or self.FakeAPI()
        asyncio.run(bot_listener.send_stats_digest(
            api, -100, "chat", text, "HTML", photo, log=lambda *_: None,
        ))
        return api

    def test_no_picture_at_all_still_posts_the_text(self):
        api = self._send(None)

        self.assertEqual(len(api.messages), 1)
        self.assertEqual(api.photos, [])
        self.assertEqual(api.messages[0]["parse_mode"], "HTML")

    def test_a_picture_is_sent_as_a_photo_with_the_post_as_its_caption(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "01_seed.png"
            image.write_bytes(b"not really a picture")
            api = self._send(image)

        self.assertEqual(len(api.photos), 1)
        self.assertEqual(api.messages, [])
        self.assertEqual(api.photos[0]["caption"], "Доброе утро")

    def test_a_rejected_upload_falls_back_to_text_rather_than_losing_the_post(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "01_seed.png"
            image.write_bytes(b"not really a picture")
            api = self._send(image, api=self.FakeAPI(photo_error=RuntimeError("PHOTO_INVALID_DIMENSIONS")))

        self.assertEqual(api.photos, [])
        self.assertEqual(len(api.messages), 1)

    def test_a_file_that_vanished_falls_back_to_text(self):
        # The gap between "the file was there when the digest was built" and "the upload
        # reads it" is a real one: a redeploy lands in between.
        with tempfile.TemporaryDirectory() as folder:
            api = self._send(Path(folder) / "01_seed.png", api=self.FakeAPI(photo_error=FileNotFoundError()))

        self.assertEqual(len(api.messages), 1)

    def test_a_caption_over_the_limit_drops_the_picture_not_the_post(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "01_seed.png"
            image.write_bytes(b"not really a picture")
            api = self._send(image, text="я" * (bot_api.CAPTION_LIMIT + 1))

        self.assertEqual(api.photos, [])
        self.assertEqual(len(api.messages), 1)

    def test_even_a_failing_send_never_raises_at_the_caller(self):
        """It runs inside a queue consumer: an exception escaping here would take down
        the loop that posts every scheduled message."""
        class DeadAPI(self.FakeAPI):
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("Telegram is down")

        asyncio.run(bot_listener.send_stats_digest(
            DeadAPI(), -100, "chat", "текст", "HTML", None, log=lambda *_: None,
        ))

    def test_the_morning_post_survives_an_empty_assets_folder_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tree, "TREE_IMAGE_DIR", Path(folder)):
                image = tree.stage_image(360_000)
                text = tree.format_morning_digest(360_000, 3_600, [], date(2026, 7, 26))
                api = self._send(image, text=text)

        self.assertIsNone(image)
        self.assertIn("Доброе утро", api.messages[0]["text"])


class PlantingTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_the_opening_post_announces_the_planting_and_calls_people_in(self):
        text = tree.format_planting_message()

        self.assertIn("посадили семечко", text)
        self.assertIn("дерево ЕПХ", text)
        self.assertIn("каждое сообщение", text)
        self.assertIn("Давайте растить его вместе", text)
        # No numbers: on planting day a height of "0 мм" would undercut the moment.
        self.assertNotIn("мм", text)
        self.assertNotIn("Особенно помогли дереву вырасти", text)

    def test_roll_call_reuses_the_same_planting_story(self):
        self.assertIn(tree.format_planting_message(), tree.format_planting_roll_call([("Аня", None)]))

    def test_the_opening_post_never_reveals_where_it_ends(self):
        """Same rule /stat already follows: naming "thirteen stages, three years" turns
        an open-ended thing the chat is growing into a progress bar with a visible end."""
        text = tree.format_planting_message()

        for giveaway in ("тринадцать", "13", "три года", "трёх лет", "Легендарн"):
            self.assertNotIn(giveaway, text)
        # And no stage name from further up the ladder leaks in either.
        for _, _, name, _ in tree.TREE_STAGES[1:]:
            self.assertNotIn(name, text)

    def test_the_planting_day_is_recorded_once_and_never_moved(self):
        first = date(2026, 7, 26)
        stats.mark_tree_planted("chat", first)
        self.assertEqual(stats.tree_planted_on("chat"), first)

        # Re-marking must not resize the tree by moving its origin.
        stats.mark_tree_planted("chat", date(2026, 9, 1))
        self.assertEqual(stats.tree_planted_on("chat"), first)

    def test_replanting_overwrites_the_planting_date(self):
        """The automatic path must never move the planting date; the administrator's
        deliberate one must."""
        first, again = date(2026, 7, 26), date(2026, 8, 10)
        stats.mark_tree_planted("chat", first)
        self.assertEqual(stats.tree_planted_on("chat"), first)

        stats.replant_tree("chat", again)

        self.assertEqual(stats.tree_planted_on("chat"), again)

    def test_replanting_counts_as_that_days_post(self):
        """The announcement the caller just posted IS that morning's post. Without this
        the 10:00 loop would follow it with "выросло на 0 мм, Семечко — 0 мм", because
        the tree was planted moments earlier and has nothing to report."""
        today = date(2026, 8, 10)
        stats.replant_tree("chat", today)

        self.assertFalse(stats.should_send_tree_digest("chat", today))
        # Tomorrow's morning post happens as normal, reporting today's growth.
        self.assertTrue(stats.should_send_tree_digest("chat", today + timedelta(days=1)))

    def test_replanting_a_chat_that_was_never_planted_works(self):
        stats.replant_tree("chat", date(2026, 7, 26))
        self.assertEqual(stats.tree_planted_on("chat"), date(2026, 7, 26))

    def test_the_announcement_is_never_scheduled_for_deletion(self):
        """It is the post the whole thing opens with -- it has to stay in the chat."""
        import inspect

        import bot_listener

        consumer = inspect.getsource(bot_listener.run_bot_listener)
        # Anchored on the `async def`, not the bare name: sibling consumers mention
        # _consume_stats_digests in their comments, and splitting on the name alone
        # picked up whichever one happened to be written first.
        consumer = consumer.split("async def _consume_stats_digests")[1].split("async def")[0]
        for source in (
            inspect.getsource(bot_listener.handle_replant_command),
            inspect.getsource(bot_listener.send_stats_digest),
            consumer,
        ):
            self.assertNotIn("schedule_bot_delete", source)

    def test_an_unplanted_chat_has_no_planting_day(self):
        self.assertIsNone(stats.tree_planted_on("chat"))

    def test_height_is_measured_from_planting_not_from_the_chats_whole_history(self):
        """The chat had months of tracked XP before the tree existed. Counting it would
        plant a seed that is already a metre tall on day one."""
        planted = date(2026, 7, 26)
        stats.mark_tree_planted("chat", planted)

        # Whatever came before simply is not in the range the total is summed over.
        self.assertEqual(stats.tree_planted_on("chat"), planted)
        self.assertEqual(tree.tree_height_mm(0), 0)
        number, _, name = tree.tree_stage(0)
        self.assertEqual((number, name), (1, "Семечко"))


class CeremonyStoreTests(unittest.TestCase):
    """The guest list is written by bot_listener (button presses) and read by listener
    (the 10:00 post) -- two processes, one file, so it gets checked like an interface."""

    ENTRY = "chat"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _open(self):
        stats.open_planting(self.ENTRY, -100123, 55, date(2026, 7, 26))

    def test_nothing_is_open_to_begin_with(self):
        self.assertFalse(stats.planting_is_open(self.ENTRY))
        self.assertEqual(stats.planters(self.ENTRY), [])

    def test_a_press_signs_you_up_once(self):
        self._open()
        self.assertTrue(stats.add_planter(self.ENTRY, 1, "Аня", "anya"))
        self.assertFalse(stats.add_planter(self.ENTRY, 1, "Аня", "anya"))
        self.assertEqual(stats.planters(self.ENTRY), [("Аня", "anya")])

    def test_presses_keep_the_order_they_arrived_in(self):
        self._open()
        for user_id, name in ((1, "Первый"), (2, "Второй"), (3, "Третий")):
            stats.add_planter(self.ENTRY, user_id, name, None)
        self.assertEqual(
            [name for name, _ in stats.planters(self.ENTRY)], ["Первый", "Второй", "Третий"]
        )

    def test_a_press_with_no_ceremony_open_is_dropped(self):
        self.assertFalse(stats.add_planter(self.ENTRY, 1, "Аня", "anya"))

    def test_names_are_stored_at_press_time(self):
        # The roll call has to name members who never wrote a word in the chat, and those
        # are exactly the ones no stats file knows about.
        self._open()
        stats.add_planter(self.ENTRY, 1, "Молчун", None)
        stats.close_planting(self.ENTRY)
        self._open()
        self.assertEqual(stats.planters(self.ENTRY), [])

    def test_closing_stops_collection(self):
        self._open()
        stats.close_planting(self.ENTRY)
        self.assertFalse(stats.planting_is_open(self.ENTRY))
        self.assertFalse(stats.add_planter(self.ENTRY, 1, "Аня", None))

    def test_a_corrupt_guest_list_reads_as_no_ceremony(self):
        # Consulted from the 10:00 loop: a broken file must not stop the morning post.
        self._open()
        (Path(self._temporary.name) / f"{stats._cache_key(self.ENTRY)}_planting.json").write_text(
            "{not json", encoding="utf-8"
        )
        self.assertIsNone(stats.planting_state(self.ENTRY))
        self.assertFalse(stats.planting_is_open(self.ENTRY))


class FounderBadgeTests(unittest.TestCase):
    ENTRY = "chat"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        stats.open_planting(self.ENTRY, -100123, 55, date(2026, 7, 26))

    def test_everyone_who_pressed_gets_it(self):
        for user_id, name in ((1, "Аня"), (2, "Боря"), (3, "Вера")):
            stats.add_planter(self.ENTRY, user_id, name, None)

        self.assertEqual(stats.award_founder_badges(self.ENTRY), 3)
        for user_id in (1, 2, 3):
            badges = stats.custom_badges_for_user(self.ENTRY, user_id)
            self.assertEqual([badge.badge_id for badge in badges], [stats.FOUNDER_BADGE_ID])
            self.assertEqual(badges[0].name, "Основатель")

    def test_awarding_twice_changes_nothing(self):
        stats.add_planter(self.ENTRY, 1, "Аня", None)
        self.assertEqual(stats.award_founder_badges(self.ENTRY), 1)
        self.assertEqual(stats.award_founder_badges(self.ENTRY), 0)
        self.assertEqual(len(stats.custom_badges_for_user(self.ENTRY, 1)), 1)

    def test_nobody_pressed_means_nobody_is_awarded(self):
        self.assertEqual(stats.award_founder_badges(self.ENTRY), 0)
        self.assertEqual(stats.list_custom_badges(self.ENTRY), [])

    def test_it_survives_a_chat_that_filled_its_badge_budget(self):
        # Otherwise the chat plants its tree and nobody gets anything for it.
        for index in range(stats.MAX_CUSTOM_BADGES):
            stats.create_custom_badge(self.ENTRY, "🎯", f"Значок {index}", 9, "Admin")
        stats.add_planter(self.ENTRY, 1, "Аня", None)

        self.assertEqual(stats.award_founder_badges(self.ENTRY), 1)
        self.assertEqual(
            [badge.badge_id for badge in stats.custom_badges_for_user(self.ENTRY, 1)],
            [stats.FOUNDER_BADGE_ID],
        )

    def test_it_is_a_unique_badge_not_an_earnable_one(self):
        badge = stats.ensure_founder_badge(self.ENTRY)
        self.assertTrue(badge.custom)
        self.assertNotIn(
            stats.FOUNDER_BADGE_ID, [badge_id for badge_id, _, _, _ in stats.AUTOMATIC_BADGES]
        )


class PlantHandlerTests(unittest.TestCase):
    """The command that opens the ceremony and the button 190 people will press."""

    ENTRY = "chat"
    GROUP = -100123
    ADMIN = {"id": 7, "username": "sultan_kembayev", "first_name": "Sultan"}
    MEMBER = {"id": 8, "username": "anya", "first_name": "Аня"}

    class API:
        def __init__(self):
            self.sent = []
            self.answers = []
            self._next_id = 54

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                               reply_markup=None, parse_mode=None):
            self._next_id += 1
            self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
            return {"message_id": self._next_id}

        async def answer_callback_query(self, callback_query_id, text=None):
            self.answers.append(text)

        async def get_chat_administrators(self, chat_id):
            return []

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.api = self.API()

    def _plant(self, actor=None, chat_id=None):
        message = {
            "message_id": 1,
            "chat": {"id": chat_id if chat_id is not None else self.GROUP, "type": "supergroup"},
            "from": actor or self.ADMIN,
        }
        asyncio.run(bot_listener.handle_plant_command(
            self.api, message, self.ENTRY, self.GROUP, log=lambda *_: None,
        ))

    def _press(self, presser):
        asyncio.run(bot_listener.handle_plant_callback(
            self.api,
            {"id": "cb", "data": "plant:join", "from": presser,
             "message": {"message_id": 55, "chat": {"id": self.GROUP, "type": "supergroup"}}},
            self.ENTRY, log=lambda *_: None,
        ))

    def _remind(self, actor=None, chat_id=None):
        message = {
            "message_id": 2,
            "chat": {"id": chat_id if chat_id is not None else self.GROUP, "type": "supergroup"},
            "from": actor or self.ADMIN,
        }
        asyncio.run(bot_listener.handle_plant_reminder_command(
            self.api, message, self.ENTRY, self.GROUP, log=lambda *_: None,
        ))

    def _send(self, command_text, actor=None, chat_type="private"):
        message = {
            "message_id": 3,
            "chat": {"id": 999, "type": chat_type},
            "from": actor or self.ADMIN,
        }
        asyncio.run(bot_listener.handle_send_command(
            self.api, message, command_text, self.ENTRY, self.GROUP, log=lambda *_: None,
        ))

    def test_it_posts_the_invitation_and_opens_collection(self):
        self._plant()
        invitation = self.api.sent[0]
        self.assertEqual(invitation["chat_id"], self.GROUP)
        self.assertIn("начинаем общую посадку", invitation["text"])
        self.assertEqual(
            invitation["reply_markup"]["inline_keyboard"][0][0]["text"], tree.SEED_BUTTON_TEXT
        )
        self.assertTrue(stats.planting_is_open(self.ENTRY))

    def test_the_invitation_button_is_the_real_one_not_the_sample(self):
        self._plant()
        data = self.api.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(data.startswith(f"{bot_listener.PLANT_CALLBACK_PREFIX}:"))
        self.assertEqual(preview.parse_callback(data), None)

    def test_a_member_cannot_open_it(self):
        self._plant(actor=self.MEMBER)
        self.assertFalse(stats.planting_is_open(self.ENTRY))
        self.assertEqual(len(self.api.sent), 1)  # the refusal, and nothing in the chat
        self.assertIn("только администратор", self.api.sent[0]["text"])

    def test_it_refuses_to_open_twice(self):
        self._plant()
        self.api = self.API()
        self._plant()
        self.assertIn("уже открыта", self.api.sent[0]["text"])
        self.assertEqual(len(self.api.sent), 1)

    def test_it_refuses_once_the_tree_exists(self):
        stats.mark_tree_planted(self.ENTRY, date(2026, 7, 20))
        self._plant()
        self.assertIn("уже растёт", self.api.sent[0]["text"])
        self.assertFalse(stats.planting_is_open(self.ENTRY))

    def test_a_failed_post_does_not_open_collection(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("telegram is down")

        self.api.send_message = boom
        try:
            self._plant()
        except RuntimeError:
            self.fail("the failure escaped the handler")
        self.assertFalse(stats.planting_is_open(self.ENTRY))

    def test_pressing_signs_you_up_and_answers_on_your_own_screen(self):
        self._plant()
        before = len(self.api.sent)
        self._press(self.MEMBER)

        self.assertEqual(stats.planters(self.ENTRY), [("Аня", "anya")])
        self.assertEqual(self.api.answers, [tree.SEED_BUTTON_ACK])
        # 190 members pressing a button must not become 190 messages in the chat.
        self.assertEqual(len(self.api.sent), before)

    def test_pressing_twice_says_so_and_signs_you_up_once(self):
        self._plant()
        self._press(self.MEMBER)
        self._press(self.MEMBER)
        self.assertEqual(len(stats.planters(self.ENTRY)), 1)
        self.assertEqual(self.api.answers[-1], tree.SEED_BUTTON_ALREADY)

    def test_reminder_shows_only_the_unique_planter_count(self):
        self._plant()
        self._press(self.MEMBER)
        self._press(self.MEMBER)
        self.api = self.API()

        self._remind()

        reminder = self.api.sent[0]
        self.assertEqual(reminder["chat_id"], self.GROUP)
        self.assertIn("Уже участвуют: <b>1</b>", reminder["text"])
        self.assertNotIn("Аня", reminder["text"])
        self.assertEqual(
            reminder["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "plant:join"
        )

    def test_reminder_requires_an_open_planting(self):
        self._remind()
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("не открыта", self.api.sent[0]["text"])

    def test_member_cannot_send_a_reminder(self):
        self._plant()
        self.api = self.API()
        self._remind(actor=self.MEMBER)
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("только администратор", self.api.sent[0]["text"])

    def test_send_posts_the_exact_message_to_the_group(self):
        self._send("/send Первая строка\nВторая строка")

        self.assertEqual(self.api.sent[0]["chat_id"], self.GROUP)
        self.assertEqual(self.api.sent[0]["text"], "Первая строка\nВторая строка")
        self.assertEqual(self.api.sent[1]["chat_id"], 999)
        self.assertIn("отправлено", self.api.sent[1]["text"])

    def test_send_requires_an_admin_and_message_text(self):
        self._send("/send Сообщение", actor=self.MEMBER)
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("только администраторы", self.api.sent[0]["text"])

        self.api = self.API()
        self._send("/send")
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("Использование", self.api.sent[0]["text"])

    def test_pressing_after_it_closed_says_so(self):
        self._plant()
        stats.close_planting(self.ENTRY)
        self._press(self.MEMBER)
        self.assertEqual(self.api.answers, ["Эта посадка уже завершена."])


class TenOClockTests(unittest.TestCase):
    """What the chat actually wakes up to. Drives listener._send_tree_digests, the code
    that turns a day of button presses into the roll call."""

    ENTRY = "chat"

    class Config:
        def __init__(self, entry):
            self.listener_allowed_chats = [entry]

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.queue = asyncio.Queue()

    def _run_ten_am(self, when=datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)):
        with patch("listener.datetime") as fake:
            fake.now.return_value = when
            asyncio.run(listener._send_tree_digests(
                None, self.Config(self.ENTRY), timezone.utc, self.queue, log=lambda *_: None,
            ))
        return [] if self.queue.empty() else [self.queue.get_nowait()]

    def _open_with(self, names):
        stats.open_planting(self.ENTRY, -100123, 55, date(2026, 7, 26))
        for index, name in enumerate(names, start=1):
            stats.add_planter(self.ENTRY, index, name, None)

    def test_the_roll_call_names_everyone_and_plants_the_tree(self):
        self._open_with(["Аня", "Боря", "Вера"])
        posted = self._run_ten_am()

        self.assertEqual(len(posted), 1)
        entry, text, parse_mode, image = posted[0]
        self.assertEqual((entry, parse_mode), (self.ENTRY, "HTML"))
        # No picture: the roll call runs far past Telegram's caption limit.
        self.assertIsNone(image)
        self.assertIn("Сегодня мы все вместе посадили семечко", text)
        self.assertIn("Семечко посадили:", text)
        for name in ("Аня", "Боря", "Вера"):
            self.assertIn(name, text)
        self.assertEqual(stats.tree_planted_on(self.ENTRY), date(2026, 7, 27))
        self.assertFalse(stats.planting_is_open(self.ENTRY))

    def test_the_roll_call_hands_out_the_founder_badge(self):
        self._open_with(["Аня", "Боря"])
        self._run_ten_am()
        for user_id in (1, 2):
            self.assertEqual(
                [b.badge_id for b in stats.custom_badges_for_user(self.ENTRY, user_id)],
                [stats.FOUNDER_BADGE_ID],
            )

    def test_nobody_pressed_leaves_the_ceremony_open_and_the_tree_unplanted(self):
        self._open_with([])
        posted = self._run_ten_am()

        self.assertIn("Семечко ждёт своих участников", posted[0][1])
        self.assertTrue(stats.planting_is_open(self.ENTRY))
        self.assertIsNone(stats.tree_planted_on(self.ENTRY))

    def test_a_second_run_the_same_morning_posts_nothing(self):
        # A restart at 10:05 must not re-announce the planting.
        self._open_with(["Аня"])
        self.assertEqual(len(self._run_ten_am()), 1)
        self.assertEqual(self._run_ten_am(), [])

    def test_it_waits_for_ten_before_planting_anything(self):
        self._open_with(["Аня"])
        posted = self._run_ten_am(datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc))

        self.assertEqual(posted, [])
        self.assertTrue(stats.planting_is_open(self.ENTRY))
        self.assertIsNone(stats.tree_planted_on(self.ENTRY))

    def test_the_morning_after_is_an_ordinary_digest(self):
        self._open_with(["Аня"])
        self._run_ten_am()
        with patch("stats.chat_tree_totals", return_value=(7_200, 3_600, [("Аня", None, 3_600)])):
            posted = self._run_ten_am(datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc))

        self.assertIn("Доброе утро", posted[0][1])
        self.assertIn("Вчера наше дерево подросло на", posted[0][1])
        self.assertNotIn("Сегодня мы все вместе посадили семечко", posted[0][1])

    def test_the_morning_post_queues_the_current_stages_picture(self):
        self._open_with(["Аня"])
        self._run_ten_am()
        with tempfile.TemporaryDirectory() as folder:
            seed = Path(folder) / "01_seed.png"
            seed.write_bytes(b"not really a picture")
            with patch.object(tree, "TREE_IMAGE_DIR", Path(folder)), \
                 patch("stats.chat_tree_totals", return_value=(7_200, 3_600, [])):
                posted = self._run_ten_am(datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc))

            self.assertEqual(posted[0][3], seed)

    def test_a_morning_with_no_picture_uploaded_still_posts(self):
        self._open_with(["Аня"])
        self._run_ten_am()
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tree, "TREE_IMAGE_DIR", Path(folder)), \
                 patch("stats.chat_tree_totals", return_value=(7_200, 3_600, [])):
                posted = self._run_ten_am(datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc))

        self.assertIsNone(posted[0][3])
        self.assertIn("Доброе утро", posted[0][1])


class TreeCommandTests(unittest.TestCase):
    CONTRIBUTORS = [("Первый", "first", 423), ("Второй", "second", 383),
                    ("Третий", "third", 326), ("Четвёртый", "fourth", 12)]

    def test_it_reports_total_yesterday_and_the_top_three(self):
        text = tree.format_tree_status(
            MEASURED_DAILY_XP * 34, MEASURED_DAILY_XP, self.CONTRIBUTORS
        )

        self.assertIn("Высота нашего дерева ЕПХ", text)
        self.assertIn("Саженец", text)
        self.assertIn("Вчера дерево подросло на", text)
        self.assertIn("@first — 423 XP", text)
        self.assertIn("@third — 326 XP", text)
        self.assertNotIn("fourth", text)
        self.assertIn("Каждое сообщение, ответ и показанная работа помогают ему расти.", text)

    def test_a_quiet_yesterday_drops_the_block_but_keeps_the_height(self):
        text = tree.format_tree_status(MEASURED_DAILY_XP * 34, 0, [])
        self.assertNotIn("Особенно помогли дереву вырасти", text)
        self.assertIn("Высота нашего дерева ЕПХ", text)
        self.assertIn("Каждое сообщение", text)

    def test_a_fresh_chat_still_gets_a_sensible_answer(self):
        text = tree.format_tree_status(0)
        self.assertIn("0 мм", text)
        self.assertIn("Семечко", text)

    def test_the_countdown_disappears_at_the_top(self):
        text = tree.format_tree_status(10**12)
        self.assertNotIn("До стадии", text)
        self.assertIn("Легендарное Древо ЕПХ", text)

    def test_it_names_the_same_people_the_morning_post_would(self):
        # Both go through _contributor_lines, so they cannot drift apart.
        status = tree.format_tree_status(360_000, 3_600, self.CONTRIBUTORS)
        morning = tree.format_morning_digest(360_000, 3_600, self.CONTRIBUTORS, date(2026, 7, 26))
        for line in ("@first — 423 XP", "@second — 383 XP", "@third — 326 XP"):
            self.assertIn(line, status)
            self.assertIn(line, morning)

    def test_it_is_offered_in_the_private_menu(self):
        import bot_listener

        self.assertIn("tree", {command["command"] for command in bot_listener.PRIVATE_CHAT_COMMANDS})
        self.assertNotIn("tree", {command["command"] for command in bot_listener.GROUP_CHAT_COMMANDS})


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("stats._stats_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_it_is_pinned_to_moscow_at_ten(self):
        self.assertEqual(stats.TREE_DIGEST_HOUR, 10)
        self.assertEqual(str(stats.tree_digest_tz()), "Europe/Moscow")

    def test_a_restart_the_same_morning_does_not_greet_twice(self):
        today = date(2026, 7, 26)
        self.assertTrue(stats.should_send_tree_digest("chat", today))

        stats.mark_tree_digest_sent("chat", today)
        self.assertFalse(stats.should_send_tree_digest("chat", today))
        # Tomorrow is a fresh morning.
        self.assertTrue(stats.should_send_tree_digest("chat", today + timedelta(days=1)))

    def test_nothing_is_sent_before_the_hour_on_a_restart(self):
        """The loop also checks on startup. Without the guard, shipping this at 05:00
        would plant the tree at 05:00 instead of the 10:00 it was promised for."""
        import inspect

        import listener

        source = inspect.getsource(listener._send_tree_digests)
        self.assertIn("now.hour < stats.TREE_DIGEST_HOUR", source)
        self.assertIn("return", source.split("now.hour < stats.TREE_DIGEST_HOUR")[1][:80])

    def test_the_marker_is_per_chat(self):
        today = date(2026, 7, 26)
        stats.mark_tree_digest_sent("chat", today)
        self.assertTrue(stats.should_send_tree_digest("other", today))

    def test_the_digest_is_html_and_the_queue_carries_that(self):
        """The two digests sharing one queue need different parse modes: sending the
        tree post as plain text prints its tags verbatim, and sending the procrastinator
        list as HTML has Telegram reject it over an unescaped display name."""
        import inspect

        import listener

        text = tree.format_morning_digest(
            360_000, 3_600, [("Кто-то", "someone", 10)], date(2026, 7, 26)
        )
        self.assertIn("<b>", text)

        source = inspect.getsource(listener._send_tree_digests)
        self.assertIn('put((entry, text, "HTML", image))', source)
        self.assertIn("put((entry, text, None, None))", inspect.getsource(listener._send_procrastinator_digests))


if __name__ == "__main__":
    unittest.main()
