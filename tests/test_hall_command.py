"""/hall in the bot: the link to the Hall of Fame, the administrators' history import, and
the recording of a vote into the hall when it is closed."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import hall_of_fame
import voting

CHAT = "Chat"
ADMIN = {"id": 7, "username": "admin"}
STRANGER = {"id": 8, "username": "someone"}
DM = 5
GROUP = -100500


def _cfg(url="https://example.com"):
    return SimpleNamespace(webapp_public_url=url, vote_miniapp_short_name=None, vote_announce_extra_chat=None)


class FakeApi:
    def __init__(self):
        self.sent = []
        self.answered = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode=None, disable_notification=False):
        self.sent.append((text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def answer_callback_query(self, callback_id, text=None):
        self.answered.append((callback_id, text))


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


class _Hall(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=root / "hall"),
        ]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    def _type(self, text, user=ADMIN, chat_type="private", cfg=None):
        api = FakeApi()
        chat_id = DM if chat_type == "private" else GROUP
        message = {"message_id": 1, "chat": {"id": chat_id, "type": chat_type}, "from": user, "text": text}

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(api_, chat_id_, user_, entry=None):
            return user_.get("id") == ADMIN["id"]

        async def scenario():
            with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                    patch.object(bot_listener, "_can_manage_chat", can_manage):
                await bot_listener.handle_hall_command(
                    api, None, cfg or _cfg(), message, CHAT, "testbot", log=lambda *_: None,
                )

        asyncio.run(scenario())
        return api

    def _closed_poll(self, poll_id="2026-W41"):
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at="t",
                           entries=[voting.Entry("1", 1, 11, "Аня", "anya", "", ["1_0.jpg"], "")])
        media = voting.media_path(CHAT, poll_id)
        media.mkdir(parents=True, exist_ok=True)
        (media / "1_0.jpg").write_bytes(b"not really a jpeg")
        voting.set_approved(poll, ["1"])
        voting.record_vote(poll, 9, ["1"])
        voting.close_and_announce(poll)
        voting.save_poll(poll)
        return poll


class HallCommandTests(_Hall):
    def test_in_a_group_it_is_one_link_anybody_can_open(self):
        api = self._type("/hall", chat_type="supergroup")
        text, markup = api.sent[0]
        self.assertIn("https://example.com/hall", text)
        self.assertEqual(_buttons(markup), [{"text": bot_listener.HALL_OPEN_BUTTON_TEXT,
                                             "url": "https://example.com/hall"}])

    def test_in_the_dm_an_administrator_also_gets_the_history_import(self):
        _, markup = self._type("/hall").sent[0]
        buttons = _buttons(markup)
        self.assertEqual(buttons[0]["web_app"], {"url": "https://example.com/hall"})
        self.assertEqual(buttons[1]["url"], "https://example.com/hall")
        self.assertEqual(buttons[2]["callback_data"], f"hallaction:import:{ADMIN['id']}")
        stranger = _buttons(self._type("/hall", user=STRANGER).sent[0][1])
        self.assertFalse(any("callback_data" in b for b in stranger))

    def test_the_reply_says_how_much_is_in_the_hall(self):
        self.assertIn("Пока пусто", self._type("/hall").sent[0][0])
        hall_of_fame.record_poll(self._closed_poll())
        self.assertIn("Конкурсов: 1 · художников: 1", self._type("/зал").sent[0][0])

    def test_without_a_public_address_it_says_so(self):
        api = self._type("/hall", cfg=_cfg(url=None))
        self.assertIn("WEBAPP_PUBLIC_URL", api.sent[0][0])
        self.assertIsNone(api.sent[0][1])

    def test_only_an_administrator_imports_and_only_in_the_dm(self):
        voting.save_results(self._closed_poll(), self._closed_poll().tally(), "текст")
        self.assertIn("только администраторы", self._type("/hall импорт", user=STRANGER).sent[0][0])
        self.assertIn("в личке", self._type("/hall импорт", chat_type="supergroup").sent[0][0])
        self.assertIsNone(hall_of_fame.load_contest(CHAT, "2026-W41"))

        api = self._type("/hall импорт")
        self.assertIn("Перенесено конкурсов: 1, уже были: 0", api.sent[-1][0])
        self.assertIsNotNone(hall_of_fame.load_contest(CHAT, "2026-W41"))
        self.assertIn("уже были: 1", self._type("/hall импорт").sent[-1][0])

    def test_the_import_button_replays_the_command_for_its_owner_only(self):
        api = FakeApi()
        replayed = []

        async def handle(api_, client, cfg, message, entry, bot_username, log=print):
            replayed.append((message["text"], message["from"]["id"]))

        async def scenario(user):
            tasks = set()
            callback = {"id": "cbq", "from": user, "data": f"hallaction:import:{ADMIN['id']}",
                        "message": {"message_id": 3, "chat": {"id": DM, "type": "private"}}}
            with patch.object(bot_listener, "handle_hall_command", handle):
                await bot_listener.handle_hall_action_callback(
                    api, None, _cfg(), callback, CHAT, "testbot", tasks, log=lambda *_: None)
                await asyncio.gather(*tasks)

        asyncio.run(scenario(STRANGER))
        self.assertEqual(replayed, [])
        self.assertEqual(api.answered[-1], ("cbq", "Эта кнопка не для тебя."))
        asyncio.run(scenario(ADMIN))
        self.assertEqual(replayed, [("/hall импорт", ADMIN["id"])])

    def test_the_command_is_in_both_menus(self):
        for menu in (bot_listener.PRIVATE_CHAT_COMMANDS, bot_listener.GROUP_CHAT_COMMANDS):
            self.assertIn("hall", {c["command"] for c in menu})


class RecordOnCloseTests(_Hall):
    def test_a_closed_vote_is_recorded_into_the_hall(self):
        poll = self._closed_poll()
        bot_listener._record_vote_in_hall(poll, poll.tally(), log=lambda *_: None)
        contest = hall_of_fame.load_contest(CHAT, poll.poll_id)
        self.assertEqual(contest.winner().author_name, "Аня")
        self.assertEqual(contest.works[0].photos, ["1_0.jpg"])
        # An unreadable photo gets no thumbnail; the record is still made.
        self.assertIsNone(contest.works[0].thumb)

    def test_a_hall_that_fails_does_not_fail_the_close(self):
        logged = []
        with patch.object(hall_of_fame, "record_poll", side_effect=OSError("disk full")):
            bot_listener._record_vote_in_hall(self._closed_poll(), [("x", 1)], log=logged.append)
        self.assertTrue(any("could not record" in line for line in logged))

    def test_nothing_admitted_is_nothing_to_record(self):
        with patch.object(hall_of_fame, "record_poll") as record:
            bot_listener._record_vote_in_hall(self._closed_poll(), [], log=lambda *_: None)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
