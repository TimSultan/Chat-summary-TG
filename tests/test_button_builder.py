import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot_listener
import button_builder
import stats


ADMIN = {"id": 7, "username": "sultan_kembayev", "first_name": "Sultan"}
MEMBER = {"id": 8, "username": "member", "first_name": "Участник"}
DM_CHAT = 999
GROUP_CHAT = -1001234567890
ENTRY = "chat"


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.edits = []
        self.caption_edits = []
        self.answers = []
        self.deleted = []
        self._next_id = 500

    async def send_message(
        self, chat_id, text, reply_to_message_id=None, reply_markup=None, parse_mode=None
    ):
        self._next_id += 1
        item = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.sent.append(item)
        return item

    async def send_photo(
        self, chat_id, photo, caption, reply_to_message_id=None,
        reply_markup=None, parse_mode=None,
    ):
        self._next_id += 1
        item = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.photos.append(item)
        return item

    async def edit_message_text(
        self, chat_id, message_id, text, reply_markup=None, parse_mode=None
    ):
        self.edits.append({
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })

    async def edit_message_caption(
        self, chat_id, message_id, caption, reply_markup=None, parse_mode=None
    ):
        self.caption_edits.append({
            "chat_id": chat_id, "message_id": message_id, "caption": caption,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def get_chat_administrators(self, chat_id):
        return [{"user": ADMIN}]


def callback(data, actor=ADMIN, chat_id=DM_CHAT, message_id=42, with_photo=False):
    message = {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}}
    if with_photo:
        message["photo"] = [{"file_id": "preview-photo"}]
    return {"id": "cb", "data": data, "from": actor, "message": message}


class RenderingTests(unittest.TestCase):
    def test_count_is_selected_between_one_and_two(self):
        keyboard = button_builder.choose_count_keyboard("flow")
        buttons = keyboard["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in buttons], ["1 кнопка", "2 кнопки"])
        self.assertEqual(
            [button_builder.parse_callback(button["callback_data"])[2] for button in buttons],
            [1, 2],
        )

    def test_post_renders_each_counter_and_matching_button(self):
        buttons = [{"text": "Да", "count": 4}, {"text": "Нет", "count": 2}]
        text = button_builder.render_post("Выбирайте", buttons)
        keyboard = button_builder.post_keyboard("post", buttons)
        self.assertIn("• Да — 4", text)
        self.assertIn("• Нет — 2", text)
        self.assertEqual(
            [row[0]["text"] for row in keyboard["inline_keyboard"]], ["Да", "Нет"]
        )

    def test_every_callback_fits_telegrams_limit(self):
        samples = [
            button_builder.callback_data("count", "f" * 10, 2),
            button_builder.callback_data("press", "p" * 10, 1),
            button_builder.callback_data("delete", "p" * 10),
        ]
        for data in samples:
            self.assertLessEqual(len(data.encode()), 64)

    def test_refresh_interval_is_exactly_three_seconds(self):
        self.assertEqual(button_builder.COUNTER_REFRESH_SECONDS, 3)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.patch = patch("stats._stats_dir", return_value=Path(self.temporary.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def test_every_tap_counts_and_survives_a_fresh_read(self):
        stats.create_button_post(
            ENTRY, "post", GROUP_CHAT, 501, "Текст", ["Да", "Нет"], ADMIN["id"], DM_CHAT
        )
        self.assertEqual(stats.increment_button_post(ENTRY, "post", GROUP_CHAT, 501, 0), 1)
        self.assertEqual(stats.increment_button_post(ENTRY, "post", GROUP_CHAT, 501, 0), 2)
        self.assertEqual(stats.increment_button_post(ENTRY, "post", GROUP_CHAT, 501, 1), 1)
        post = stats.button_post(ENTRY, "post")
        self.assertEqual([button["count"] for button in post["buttons"]], [2, 1])

    def test_an_old_message_cannot_increment_or_delete_a_new_post(self):
        stats.create_button_post(
            ENTRY, "post", GROUP_CHAT, 501, "Текст", ["Да"], ADMIN["id"], DM_CHAT
        )
        self.assertIsNone(stats.increment_button_post(ENTRY, "post", GROUP_CHAT, 999, 0))
        self.assertIsNone(stats.delete_button_post(ENTRY, "post", GROUP_CHAT, 999))
        self.assertIsNotNone(stats.button_post(ENTRY, "post"))


class BuilderFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.patch = patch("stats._stats_dir", return_value=Path(self.temporary.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.api = FakeAPI()
        self.flows = {}

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def start(self):
        self._run(bot_listener.handle_button_builder_command(
            self.api,
            {
                "message_id": 1,
                "chat": {"id": DM_CHAT, "type": "private"},
                "from": ADMIN,
                "text": "/buttons",
            },
            ENTRY,
            GROUP_CHAT,
            self.flows,
        ))
        flow_id = next(iter(self.flows))
        return flow_id, self.flows[flow_id]

    def reply(self, flow, text=None, photo=None):
        message = {
            "message_id": self.api._next_id + 1,
            "chat": {"id": DM_CHAT, "type": "private"},
            "from": ADMIN,
            "reply_to_message": {"message_id": flow["prompt_message_id"]},
        }
        if text is not None:
            message["text"] = text
        if photo is not None:
            message["photo"] = [{"file_id": photo}]
        return self._run(
            bot_listener.handle_button_builder_text_input(
                self.api, message, self.flows, log=lambda *_: None
            )
        )

    def press(self, data, actor=ADMIN, chat_id=DM_CHAT, message_id=42, with_photo=False):
        return self._run(bot_listener.handle_button_builder_callback(
            self.api,
            callback(data, actor, chat_id, message_id, with_photo=with_photo),
            ENTRY,
            self.flows,
            log=lambda *_: None,
        ))

    def build_labels(self, count=2):
        flow_id, flow = self.start()
        self.reply(flow, text="Какой вариант выбираем?")
        self.press(button_builder.callback_data("count", flow_id, count))
        self.reply(flow, text="Первый")
        if count == 2:
            self.reply(flow, text="Второй")
        return flow_id, flow

    def test_the_flow_asks_for_text_count_and_each_selected_label(self):
        flow_id, flow = self.build_labels(count=2)
        self.assertEqual(flow["message_text"], "Какой вариант выбираем?")
        self.assertEqual(flow["button_count"], 2)
        self.assertEqual(flow["button_texts"], ["Первый", "Второй"])
        self.assertEqual(flow["awaiting"], "photo_choice")
        self.assertIn("Добавить картинку", self.api.sent[-1]["text"])
        count_keyboard = self.api.sent[1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in count_keyboard], ["1 кнопка", "2 кнопки"])

    def test_one_button_without_a_photo_publishes_counts_and_delete_control(self):
        flow_id, flow = self.build_labels(count=1)
        self.press(button_builder.callback_data("photo", flow_id, 0))
        preview_message = self.api.sent[-1]
        self.assertIn("• Первый — 0", preview_message["text"])

        self.press(
            button_builder.callback_data("send", flow_id),
            message_id=preview_message["message_id"],
        )
        published = next(item for item in self.api.sent if item["chat_id"] == GROUP_CHAT)
        self.assertIn("Нажатия:", published["text"])
        post_data = published["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        action, post_id, index = button_builder.parse_callback(post_data)
        self.assertEqual((action, index), ("press", 0))
        self.assertIsNotNone(stats.button_post(ENTRY, post_id))
        self.assertEqual(
            self.api.edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"],
            "🗑 Удалить из чата",
        )

        self.press(post_data, actor=MEMBER, chat_id=GROUP_CHAT, message_id=published["message_id"])
        self.press(post_data, actor=MEMBER, chat_id=GROUP_CHAT, message_id=published["message_id"])
        rendered = {post_id: (0,)}
        self._run(
            bot_listener.refresh_button_counters_once(
                self.api, ENTRY, rendered, log=lambda *_: None
            )
        )
        self.assertIn("• Первый — 2", self.api.edits[-1]["text"])

        delete_data = self.api.edits[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.press(delete_data, message_id=preview_message["message_id"])
        self.assertIn((GROUP_CHAT, published["message_id"]), self.api.deleted)
        self.assertIsNone(stats.button_post(ENTRY, post_id))

    def test_optional_photo_uses_a_caption_for_preview_publish_and_refresh(self):
        flow_id, flow = self.build_labels(count=2)
        self.press(button_builder.callback_data("photo", flow_id, 1))
        self.assertEqual(flow["awaiting"], "photo")
        self.reply(flow, photo="photo-file-id")
        preview = self.api.photos[-1]
        self.assertEqual(preview["chat_id"], DM_CHAT)
        self.assertIn("• Второй — 0", preview["caption"])

        self.press(
            button_builder.callback_data("send", flow_id),
            message_id=preview["message_id"],
            with_photo=True,
        )
        published = next(item for item in self.api.photos if item["chat_id"] == GROUP_CHAT)
        post_data = published["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        _, post_id, _ = button_builder.parse_callback(post_data)
        self.press(post_data, actor=MEMBER, chat_id=GROUP_CHAT, message_id=published["message_id"])
        self._run(
            bot_listener.refresh_button_counters_once(
                self.api, ENTRY, {post_id: (0, 0)}, log=lambda *_: None
            )
        )
        self.assertIn("• Второй — 1", self.api.caption_edits[-1]["caption"])

    def test_preview_buttons_do_not_change_a_counter(self):
        flow_id, flow = self.build_labels(count=1)
        self.press(button_builder.callback_data("photo", flow_id, 0))
        before = stats.active_button_posts(ENTRY)
        self.press(button_builder.callback_data("sample", flow_id, 0))
        self.assertEqual(stats.active_button_posts(ENTRY), before)
        self.assertEqual(self.api.answers[-1], "Это предпросмотр — счётчик не изменился.")

    def test_a_transient_edit_failure_is_retried_on_the_next_cycle(self):
        stats.create_button_post(
            ENTRY, "post", GROUP_CHAT, 501, "Текст", ["Да"], ADMIN["id"], DM_CHAT
        )
        rendered = {}

        class FailsOnce(FakeAPI):
            def __init__(self):
                super().__init__()
                self.failed = False

            async def edit_message_text(self, *args, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("temporary network problem")
                await super().edit_message_text(*args, **kwargs)

        api = FailsOnce()
        self._run(
            bot_listener.refresh_button_counters_once(
                api, ENTRY, rendered, log=lambda *_: None
            )
        )
        self.assertNotIn("post", rendered)
        self._run(
            bot_listener.refresh_button_counters_once(
                api, ENTRY, rendered, log=lambda *_: None
            )
        )
        self.assertEqual(rendered["post"], (0,))
        self.assertEqual(len(api.edits), 1)

    def test_a_manually_deleted_post_is_removed_from_the_refresh_store(self):
        stats.create_button_post(
            ENTRY, "post", GROUP_CHAT, 501, "Текст", ["Да"], ADMIN["id"], DM_CHAT
        )

        class MissingPost(FakeAPI):
            async def edit_message_text(self, *args, **kwargs):
                raise RuntimeError("Bad Request: message to edit not found")

        self._run(
            bot_listener.refresh_button_counters_once(
                MissingPost(), ENTRY, {}, log=lambda *_: None
            )
        )
        self.assertIsNone(stats.button_post(ENTRY, "post"))

    def test_non_admin_cannot_start_the_generator(self):
        self._run(bot_listener.handle_button_builder_command(
            self.api,
            {
                "message_id": 1,
                "chat": {"id": DM_CHAT, "type": "private"},
                "from": MEMBER,
                "text": "/buttons",
            },
            ENTRY,
            GROUP_CHAT,
            self.flows,
        ))
        self.assertEqual(self.flows, {})
        self.assertIn("только администраторы", self.api.sent[-1]["text"])

    def test_the_buttons_command_reaches_the_constructor_through_dispatch(self):
        self._run(bot_listener._dispatch_update(
            {
                "message": {
                    "message_id": 1,
                    "chat": {"id": DM_CHAT, "type": "private"},
                    "from": ADMIN,
                    "text": "/buttons",
                }
            },
            self.api,
            None,
            SimpleNamespace(listener_allowed_chats=[ENTRY]),
            None,
            "bot",
            99,
            set(),
            asyncio.Queue(),
            {},
            set(),
            set(),
            ENTRY,
            {ENTRY: GROUP_CHAT},
            {},
            None,
            {},
            {},
            {},
            self.flows,
            log=lambda *_: None,
        ))
        self.assertEqual(len(self.flows), 1)
        self.assertIn("Отправь текст", self.api.sent[-1]["text"])


if __name__ == "__main__":
    unittest.main()
