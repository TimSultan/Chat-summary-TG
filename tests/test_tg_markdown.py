"""tg_markdown.prepare: an administrator's message -> text + entities the bot can post."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tg_markdown
from tg_markdown import prepare


def _link(offset, length, url):
    return {"type": "text_link", "url": url, "offset": offset, "length": length}


class KeepsTheSendersFormattingTests(unittest.TestCase):
    def test_entities_are_kept_as_they_came(self):
        entities = [_link(9, 7, "https://t.me/papka/5"), {"type": "bold", "offset": 0, "length": 8}]
        text, kept = prepare("Голосуем! Правила", entities)
        self.assertEqual(text, "Голосуем! Правила")
        self.assertEqual(kept, [{"type": "bold", "offset": 0, "length": 8}, _link(9, 7, "https://t.me/papka/5")])

    def test_trimming_moves_the_entities_with_the_text(self):
        text, kept = prepare("  \nПривет  ", [{"type": "bold", "offset": 3, "length": 6}])
        self.assertEqual((text, kept), ("Привет", [{"type": "bold", "offset": 0, "length": 6}]))

    def test_offsets_count_an_emoji_as_two_like_telegram_does(self):
        text, kept = prepare("🎨 [ссылка](https://a.ru)")
        self.assertEqual(text, "🎨 ссылка")
        self.assertEqual(kept, [_link(3, 6, "https://a.ru")])

    def test_custom_emoji_is_dropped_but_its_character_stays(self):
        text, kept = prepare("🔥 Голосуем", [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}])
        self.assertEqual((text, kept), ("🔥 Голосуем", []))

    def test_empty_text_is_empty(self):
        self.assertEqual(prepare("   ", [{"type": "bold", "offset": 0, "length": 3}]), ("", []))
        self.assertEqual(prepare(None), ("", []))


class TypedMarkdownTests(unittest.TestCase):
    def test_a_link(self):
        self.assertEqual(
            prepare("Голосуем: [правила](https://t.me/papka/5)!"),
            ("Голосуем: правила!", [_link(10, 7, "https://t.me/papka/5")]),
        )

    def test_the_address_telegram_already_detected_inside_the_brackets_goes_away_with_them(self):
        text, kept = prepare("[тут](https://x.com)", [{"type": "url", "offset": 6, "length": 13}])
        self.assertEqual((text, kept), ("тут", [_link(0, 3, "https://x.com")]))

    def test_bold_italic_strike_spoiler_and_code(self):
        text, kept = prepare("**ж** __к__ ~~з~~ ||с|| `код`")
        self.assertEqual(text, "ж к з с код")
        self.assertEqual([e["type"] for e in kept], ["bold", "italic", "strikethrough", "spoiler", "code"])
        self.assertEqual([e["offset"] for e in kept], [0, 2, 4, 6, 8])

    def test_a_link_inside_bold(self):
        text, kept = prepare("**[жирная ссылка](https://a.ru)**")
        self.assertEqual(text, "жирная ссылка")
        self.assertEqual(sorted(e["type"] for e in kept), ["bold", "text_link"])
        self.assertTrue(all((e["offset"], e["length"]) == (0, 13) for e in kept))

    def test_nothing_inside_code_is_converted(self):
        text, kept = prepare("`**как есть**`")
        self.assertEqual((text, kept), ("**как есть**", [{"type": "code", "offset": 0, "length": 12}]))
        # Code the sender's app already made is just as untouchable.
        text, _ = prepare("**как есть**", [{"type": "code", "offset": 0, "length": 12}])
        self.assertEqual(text, "**как есть**")

    def test_single_stars_and_underscores_are_left_alone(self):
        for sample in ("@some_user_name", "цена *со скидкой", "a ** b ** c", "2 * 3 = 6"):
            self.assertEqual(prepare(sample), (sample, []))

    def test_short_addresses_are_completed(self):
        self.assertEqual(prepare("[x](t.me/papka)")[1], [_link(0, 1, "https://t.me/papka")])
        self.assertEqual(prepare("[x](@papka_bot)")[1], [_link(0, 1, "https://t.me/papka_bot")])

    def test_what_is_not_an_address_stays_as_typed(self):
        for sample in ("[x](javascript:alert)", "[x](просто слова)", "[x](abc)"):
            self.assertEqual(prepare(sample), (sample, []))


class LinkTargetTests(unittest.TestCase):
    def test_schemes(self):
        self.assertEqual(tg_markdown.link_target("https://a.ru/x?y=1"), "https://a.ru/x?y=1")
        self.assertEqual(tg_markdown.link_target("tg://resolve?domain=x"), "tg://resolve?domain=x")
        self.assertIsNone(tg_markdown.link_target("javascript:alert(1)"))
        self.assertIsNone(tg_markdown.link_target("https:"))


if __name__ == "__main__":
    unittest.main()
