"""What a /summary request covers, and how its answer reaches Telegram.

Pinned down after a night in prod where a bare /summary at 00:28 summarised the 28
minutes since midnight, and the retries people tried next -- "/summary 12", "/summary 4",
"/summary 3" -- were all read as "today" too, because nothing understood a bare number.
"""

import asyncio
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import intent_v2
import listener

MSK = ZoneInfo("Europe/Moscow")


def _routed(day, lookback=None, username=None):
    return {
        "start_date": day, "end_date": day, "lookback_hours": lookback,
        "username": username, "cleaned_question": "x",
    }


class FakeOpenAI:
    """Answers every chat.completions.create with one fixed JSON body."""

    def __init__(self, body: dict):
        content = json.dumps(body, ensure_ascii=False)
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_: response)
        )


class BareHoursTests(unittest.TestCase):
    def test_a_bare_number_is_hours(self):
        for text, hours in (
            ("/summary 12", 12), ("/summary@echhchat_bot 4", 4), ("/summary 3ч", 3),
            ("/summary 1,5 часа", 1.5), ("@echhchat_bot summary 6", 6),
        ):
            with self.subTest(text=text):
                self.assertEqual(intent_v2.bare_lookback_hours(text), hours)

    def test_anything_more_than_a_number_is_left_to_the_router(self):
        for text in ("/summary", "/summary 12.10", "/summary 12 октября", "/summary 0",
                     "/summary 3 @bob", "/summary вчера", "/summary 2026-10-12"):
            with self.subTest(text=text):
                self.assertIsNone(intent_v2.bare_lookback_hours(text))

    def test_the_router_result_is_overridden_with_the_hours_and_a_real_question(self):
        # Exactly what gpt-5.4-nano returned for "/summary 12" in prod.
        body = {
            "start_date": "2026-10-13", "end_date": "2026-10-13", "lookback_hours": None,
            "username": None, "cleaned_question": "Сделай summary за 12.",
        }
        with patch.object(intent_v2, "OpenAI", lambda api_key: FakeOpenAI(body)):
            routed = intent_v2.route_request(
                api_key="k", model="m", text="/summary 12",
                reference_date=date(2026, 10, 13), mentioned_usernames=[],
            )
        self.assertEqual(routed["lookback_hours"], 12)
        self.assertIn("последние 12", routed["cleaned_question"])


class DefaultWindowTests(unittest.TestCase):
    TODAY = date(2026, 10, 13)

    def _hours(self, text, routed, hour, minute=0):
        request_dt = datetime(2026, 10, 13, hour, minute, tzinfo=MSK)
        return listener.default_lookback_hours(text, routed, request_dt, MSK)

    def test_a_bare_request_just_after_midnight_covers_the_last_twelve_hours(self):
        self.assertEqual(
            self._hours("/summary", _routed(self.TODAY), 0, 28),
            listener.DEFAULT_MIN_WINDOW_HOURS,
        )

    def test_from_noon_on_the_calendar_day_is_already_long_enough(self):
        self.assertIsNone(self._hours("/summary", _routed(self.TODAY), 12, 0))
        self.assertIsNone(self._hours("/summary", _routed(self.TODAY), 23, 6))

    def test_an_explicit_period_is_never_widened(self):
        self.assertIsNone(self._hours("/summary сегодня", _routed(self.TODAY), 0, 28))
        self.assertIsNone(self._hours("/summary за 3 часа", _routed(self.TODAY, lookback=3), 0, 28))
        self.assertIsNone(self._hours("/summary вчера", _routed(date(2026, 10, 12)), 0, 28))

    def test_an_impression_keeps_its_own_fallback(self):
        """Its sparse-history step adds yesterday itself; widening as well would feed
        the model yesterday's evening twice."""
        routed = _routed(self.TODAY, username="bob")
        self.assertIsNone(self._hours("/summary впечатление о @bob", routed, 0, 28))

    def test_the_widened_window_reaches_back_across_midnight(self):
        request_dt = datetime(2026, 10, 13, 0, 28, tzinfo=MSK)
        start, end, window_start, window_end, hours = listener.resolve_time_window(
            self.TODAY, self.TODAY,
            listener.default_lookback_hours("/summary", _routed(self.TODAY), request_dt, MSK),
            request_dt, MSK, log=lambda *_: None,
        )
        self.assertEqual((start, end), (date(2026, 10, 12), self.TODAY))
        self.assertEqual(window_start, datetime(2026, 10, 12, 12, 28, tzinfo=MSK))
        self.assertEqual(window_end, request_dt)


class TelegramHtmlTests(unittest.TestCase):
    def test_bold_topic_names_survive_and_text_is_escaped(self):
        self.assertEqual(
            bot_listener._telegram_html("**Цены <5к>** — Nataly & Леонид"),
            "<b>Цены &lt;5к&gt;</b> — Nataly &amp; Леонид",
        )

    def test_backticks_headings_and_star_bullets_become_plain_text(self):
        self.assertEqual(
            bot_listener._telegram_html("### **Бот**\nтестил `@echhchat_bot`  \n* пункт"),
            "<b>Бот</b>\nтестил @echhchat_bot\n• пункт",
        )

    def test_a_stray_pair_marker_does_not_embolden_the_paragraphs_below(self):
        html = bot_listener._telegram_html("**Тема — без закрытия\n\n**Другая** — текст")
        self.assertEqual(html, "**Тема — без закрытия\n\n<b>Другая</b> — текст")


class SplitTests(unittest.TestCase):
    def test_a_long_answer_is_cut_between_paragraphs_never_inside_bold_or_words(self):
        topic = "**Тема номер {n}** — " + "слово " * 60
        text = "\n\n".join(topic.format(n=n).strip() for n in range(30))
        chunks = bot_listener._split_for_telegram(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)
            self.assertTrue(chunk.startswith("**Тема номер"), chunk[:40])
            self.assertTrue(chunk.endswith("слово"), chunk[-40:])
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_one_endless_paragraph_falls_back_to_word_boundaries(self):
        text = "слово " * 500
        chunks = bot_listener._split_for_telegram(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_a_short_answer_is_one_message(self):
        self.assertEqual(bot_listener._split_for_telegram("коротко"), ["коротко"])

    def test_only_the_first_part_replies_to_the_request(self):
        sent = []

        class Api:
            async def send_message(self, chat_id, text, **kwargs):
                sent.append(kwargs)
                return {"message_id": len(sent)}

        text = "\n\n".join(f"**Тема {n}** — " + "слово " * 200 for n in range(10))
        ids = asyncio.run(bot_listener.send_long_bot_message(Api(), 1, text, 77))
        self.assertEqual(len(ids), len(sent))
        self.assertGreater(len(sent), 1)
        self.assertEqual([k["reply_to_message_id"] for k in sent], [77] + [None] * (len(sent) - 1))


if __name__ == "__main__":
    unittest.main()
