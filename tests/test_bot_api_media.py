"""bot_api.TelegramBotAPI.edit_message_media_photo -- the call the vote carousel leans on
to swap one message's picture instead of posting a fresh photo per nominee.

Protects the three things a carousel silently breaks on if they regress: the wrong Bot API
method name (editMessageCaption cannot change an image), an explicit null smuggled inside
`media` (Telegram rejects the whole edit rather than treating it as "unset"), and a
reply_markup sent on every navigation step (an omitted one leaves the keyboard alone, a
sent one makes the client re-render the buttons on each tap).

The HTTP layer is not exercised: _call is replaced with a recorder that mirrors the one
behaviour of the real one this method depends on -- dropping top-level None params -- so
the assertions describe the request Telegram would actually receive.
"""

import asyncio
import unittest

import bot_api
from errors import ChatSummaryError

CHAT = -1001234567890
MESSAGE_ID = 4242
FILE_ID = "AgACAgIAAxkBAAI-photo-file-id"


class RecordingAPI(bot_api.TelegramBotAPI):
    """A real TelegramBotAPI whose _call records instead of talking to Telegram.

    Subclassed rather than monkeypatched onto an instance so the method under test runs
    exactly as it does in production, session and all -- the session is never touched,
    because _call is where every request would have gone through.
    """

    def __init__(self, raises: Exception | None = None):
        super().__init__("dummy-token", session=None)
        self.calls = []
        self._raises = raises

    async def _call(self, method, _http_timeout=20.0, **params):
        # Same None-stripping as bot_api.TelegramBotAPI._call: a param left at None is
        # never put on the wire, which is how "omitted reply_markup" reaches Telegram.
        self.calls.append((method, {k: v for k, v in params.items() if v is not None}))
        if self._raises is not None:
            raise self._raises
        return {}


def _edit(api, **kwargs):
    return asyncio.run(
        api.edit_message_media_photo(CHAT, MESSAGE_ID, FILE_ID, **kwargs)
    )


class EditMessageMediaPhotoTests(unittest.TestCase):
    def test_calls_the_edit_message_media_method_with_the_target_message(self):
        api = RecordingAPI()
        _edit(api)
        self.assertEqual(len(api.calls), 1)
        method, params = api.calls[0]
        self.assertEqual(method, "editMessageMedia")
        self.assertEqual(params["chat_id"], CHAT)
        self.assertEqual(params["message_id"], MESSAGE_ID)

    def test_sends_the_file_id_as_a_photo_media_object(self):
        api = RecordingAPI()
        _edit(api)
        media = api.calls[0][1]["media"]
        self.assertEqual(media["type"], "photo")
        self.assertEqual(media["media"], FILE_ID)

    def test_omits_caption_and_parse_mode_from_media_when_not_given(self):
        api = RecordingAPI()
        _edit(api)
        media = api.calls[0][1]["media"]
        self.assertNotIn("caption", media)
        self.assertNotIn("parse_mode", media)

    def test_carries_caption_and_parse_mode_inside_media_when_given(self):
        api = RecordingAPI()
        _edit(api, caption="Номинант 1", parse_mode="Markdown")
        media = api.calls[0][1]["media"]
        self.assertEqual(media["caption"], "Номинант 1")
        self.assertEqual(media["parse_mode"], "Markdown")

    def test_leaves_reply_markup_out_of_the_request_when_the_caller_omits_it(self):
        api = RecordingAPI()
        _edit(api)
        self.assertNotIn("reply_markup", api.calls[0][1])

    def test_sends_reply_markup_when_the_caller_passes_one(self):
        api = RecordingAPI()
        keyboard = {"inline_keyboard": [[{"text": "→", "callback_data": "vote:next"}]]}
        _edit(api, reply_markup=keyboard)
        self.assertEqual(api.calls[0][1]["reply_markup"], keyboard)

    def test_swallows_not_modified_because_tapping_the_same_button_twice_is_normal(self):
        api = RecordingAPI(
            raises=ChatSummaryError(
                "Telegram Bot API editMessageMedia failed: Bad Request: message is not modified"
            )
        )
        _edit(api)  # must not raise

    def test_reraises_any_other_telegram_failure(self):
        api = RecordingAPI(
            raises=ChatSummaryError(
                "Telegram Bot API editMessageMedia failed: Bad Request: MEDIA_INVALID"
            )
        )
        with self.assertRaises(ChatSummaryError):
            _edit(api)


if __name__ == "__main__":
    unittest.main()
