"""The in-chat ballot: the inline-keyboard photo carousel that lives in the voter's own DM
beside the Mini App.

One message per voter for the whole ballot, edited in place on every tap. That single
design decision is what most of this file protects:

  * the current position is read back out of the message's own caption and kept nowhere
    else, so _carousel_caption and _carousel_position must agree exactly -- if they ever
    drift, a redeploy (or any tap) silently throws every voter back to work 1;
  * because no button carries an index, a navigation step sends NO reply_markup at all.
    The moment one leaks back into a plain step, every tap makes the client re-render the
    keyboard -- the exact cost the constant keyboard exists to avoid;
  * a burst of taps collapses into ONE edit: Telegram sustains roughly one edit per second
    per chat, so five queued edits are a stuttering carousel and eventually a 429;
  * hiding is strictly personal (never another voter's view, never the admitted set) and
    reversible, and voting still obeys max_choices, allow_revote and a closed poll;
  * every path answers the callback query. A button that is never answered spins on the
    presser's screen until Telegram gives up, which reads as a dead bot;
  * the photos are uploaded exactly once, at collect time, and every later render is a
    file_id -- prewarm_entry_file_ids is what stands between the carousel and an upload
    per tap.

Nothing here touches the network or the real voting directory: a FakeApi records calls,
and voting._voting_dir is patched onto a tempfile directory.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import voting

ENTRY = "Chat"
POLL_ID = "2026-08-04"
VOTER = 4242
OTHER_VOTER = 777
MESSAGE_ID = 31
ADMIN_CHAT = 555


def _run(coro):
    return asyncio.run(coro)


def _entry(number: int, file_ids: bool = True, media: bool = True) -> voting.Entry:
    return voting.Entry(
        entry_id=f"e{number}",
        message_id=number,
        author_id=100 + number,
        author_name=f"Автор {number}",
        author_username=f"user{number}",
        text=f"работа номер {number}",
        media=[f"photo{number}.jpg"] if media else [],
        posted_at="2026-08-03T12:00:00+03:00",
        file_ids=[f"fid{number}"] if file_ids else [],
    )


def _poll(count: int = 3, approved=None, file_ids: bool = True, **fields) -> voting.Poll:
    entries = [_entry(n, file_ids=file_ids) for n in range(1, count + 1)]
    poll = voting.Poll(
        poll_id=POLL_ID, entry=ENTRY, created_at="2026-08-04T00:00:00+00:00", entries=entries
    )
    voting.set_approved(poll, [e.entry_id for e in entries] if approved is None else approved)
    for name, value in fields.items():
        setattr(poll, name, value)
    return poll


def _caption(index: int, total: int = 3) -> str:
    """A photo-view caption shaped exactly like the one _carousel_caption writes -- the
    only place the carousel keeps its position."""
    return f"Работа {index + 1} из {total} · выбрано 0\nАвтор 1 (@user1)"


class FakeApi:
    """Records what would have gone to Telegram instead of sending it.

    `on_answer` exists for the coalescing tests: answerCallbackQuery is the await the
    handler makes before it draws, which is exactly where a *second* tap realistically
    lands while the first one is still in flight.
    """

    def __init__(self, photo_response=None, failing_uploads=(), on_answer=None):
        self.answered = []
        self.messages = []
        self.photos = []
        self.media_edits = []
        self.caption_edits = []
        self.uploads = []
        self.deleted = []
        self._photo_response = photo_response
        self._failing_uploads = set(failing_uploads)
        self._on_answer = on_answer

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append(text)
        if self._on_answer is not None:
            self._on_answer()

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           reply_markup=None, parse_mode=None):
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 500 + len(self.messages)}

    async def send_photo(self, chat_id, photo, caption, reply_to_message_id=None,
                         reply_markup=None, parse_mode=None):
        self.photos.append({
            "chat_id": chat_id, "file_id": photo, "caption": caption, "reply_markup": reply_markup,
        })
        return {"message_id": 600 + len(self.photos)}

    async def send_photo_file(self, chat_id, path, caption=None, reply_to_message_id=None,
                              reply_markup=None, parse_mode=None):
        name = Path(path).name
        self.uploads.append(name)
        if name in self._failing_uploads:
            raise RuntimeError("Bad Request: failed to upload photo")
        return self._photo_response

    async def edit_message_media_photo(self, chat_id, message_id, file_id, caption=None,
                                       parse_mode=None, reply_markup=None):
        self.media_edits.append({
            "chat_id": chat_id, "message_id": message_id, "file_id": file_id,
            "caption": caption, "reply_markup": reply_markup,
        })

    async def edit_message_caption(self, chat_id, message_id, caption,
                                   reply_markup=None, parse_mode=None):
        self.caption_edits.append({
            "chat_id": chat_id, "message_id": message_id, "caption": caption,
            "reply_markup": reply_markup,
        })

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    @property
    def edits(self):
        return self.media_edits + self.caption_edits


class CarouselTestCase(unittest.TestCase):
    """A saved three-entry poll in a throwaway directory, plus one helper that presses a
    button on it. The handler reloads the poll from disk on every call, so assertions read
    it back with voting.latest_poll rather than trusting an in-memory object."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)
        self.taps: dict = {}
        self.member_ok = True
        self.member_checks: list = []
        self.save(_poll())

    def save(self, poll: voting.Poll) -> voting.Poll:
        voting.save_poll(poll)
        return poll

    def stored(self) -> voting.Poll:
        return voting.latest_poll(ENTRY)

    def press(self, action, argument="", caption=None, user_id=VOTER, api=None,
              taps=None, message_id=MESSAGE_ID, with_message=True):
        api = api if api is not None else FakeApi()
        callback = {
            "id": "cbq-1",
            "data": bot_listener._carousel_callback_data(action, argument),
            "from": {"id": user_id, "username": f"voter{user_id}"},
        }
        if with_message:
            callback["message"] = {
                "message_id": message_id,
                "chat": {"id": user_id},
                "caption": _caption(0) if caption is None else caption,
            }

        async def member_ok(*args, **kwargs):
            # (api, telethon_client, entry, user_id, known_chat_ids)
            self.member_checks.append(args[2] if len(args) > 2 else None)
            return self.member_ok

        with patch.object(bot_listener, "_carousel_member_ok", member_ok):
            _run(bot_listener.handle_vote_carousel_callback(
                api, None, callback, ENTRY, {}, self.taps if taps is None else taps,
                log=lambda *_: None,
            ))
        return api


class CarouselCallbackDataTests(unittest.TestCase):
    """The wire format. It is the only thing a button can carry, so it has to survive an
    entry_id that looks like anything."""

    def test_an_action_round_trips(self):
        data = bot_listener._carousel_callback_data("next")
        self.assertEqual(bot_listener._parse_carousel_callback(data), ("next", ""))

    def test_an_argument_round_trips(self):
        data = bot_listener._carousel_callback_data("go", "7")
        self.assertEqual(bot_listener._parse_carousel_callback(data), ("go", "7"))

    def test_an_argument_containing_a_colon_is_kept_whole(self):
        parsed = bot_listener._parse_carousel_callback("votecar:unhide:12:34")
        self.assertEqual(parsed, ("unhide", "12:34"))

    def test_an_unknown_action_is_rejected(self):
        self.assertIsNone(bot_listener._parse_carousel_callback("votecar:explode:1"))

    def test_another_feature_s_callback_is_rejected(self):
        self.assertIsNone(bot_listener._parse_carousel_callback("voteaction:collect:-100:1"))

    def test_malformed_data_is_rejected(self):
        self.assertIsNone(bot_listener._parse_carousel_callback("votecar"))
        self.assertIsNone(bot_listener._parse_carousel_callback(""))
        self.assertIsNone(bot_listener._parse_carousel_callback(None))


class CarouselPositionRoundTripTests(CarouselTestCase):
    """_carousel_caption writes the position, _carousel_position reads it back. Nothing
    else remembers where a voter is, so this pair is the whole navigation state."""

    def test_the_caption_the_carousel_writes_is_the_one_it_reads_back(self):
        poll = _poll(count=12)
        for index in range(12):
            caption = bot_listener._carousel_caption(
                poll, poll.entries[index], index, 12, 0, []
            )
            self.assertEqual(bot_listener._carousel_position({"caption": caption}), index)

    def test_the_caption_says_which_work_of_how_many(self):
        poll = _poll(count=12)
        caption = bot_listener._carousel_caption(poll, poll.entries[2], 2, 12, 0, [])
        self.assertTrue(caption.startswith("Работа 3 из 12"))

    def test_a_position_survives_a_hidden_count_and_a_choice_marker(self):
        poll = _poll(count=12)
        caption = bot_listener._carousel_caption(poll, poll.entries[4], 4, 12, 3, ["e5"])
        self.assertIn("скрыто 3", caption)
        self.assertIn("✔ Твой голос за эту работу", caption)
        self.assertEqual(bot_listener._carousel_position({"caption": caption}), 4)

    def test_the_list_view_caption_has_no_position_so_it_reads_as_the_first_work(self):
        self.assertEqual(
            bot_listener._carousel_position({"caption": "Работы на голосовании:\n\n1. Автор 1"}), 0
        )

    def test_the_hidden_view_caption_reads_as_the_first_work(self):
        self.assertEqual(
            bot_listener._carousel_position({"caption": "Скрытые работы (только у тебя):"}), 0
        )

    def test_a_message_with_no_caption_reads_as_the_first_work(self):
        self.assertEqual(bot_listener._carousel_position({"message_id": 1}), 0)

    def test_a_missing_message_reads_as_the_first_work(self):
        self.assertEqual(bot_listener._carousel_position(None), 0)
        self.assertEqual(bot_listener._carousel_position({}), 0)


class CarouselKeyboardTests(unittest.TestCase):
    """The photo view's keyboard is a constant, and that is load-bearing: it is why a
    navigation step can omit reply_markup entirely."""

    def test_no_button_carries_a_position(self):
        keyboard = bot_listener._carousel_keyboard()
        for row in keyboard["inline_keyboard"]:
            for button in row:
                self.assertTrue(button["callback_data"].endswith(":"), button["callback_data"])

    def test_the_keyboard_is_identical_every_time_it_is_built(self):
        self.assertEqual(bot_listener._carousel_keyboard(), bot_listener._carousel_keyboard())


class CarouselNavigationTests(CarouselTestCase):
    """Stepping between nominees: an editMessageMedia with NO reply_markup, clamped to the
    ends of that voter's visible list."""

    def _stepped_to(self, api) -> str:
        self.assertEqual(len(api.media_edits), 1, api.edits)
        return api.media_edits[0]["caption"]

    def test_stepping_forward_edits_the_photo_in_place(self):
        api = self.press("next", caption=_caption(0))
        self.assertTrue(self._stepped_to(api).startswith("Работа 2 из 3"))
        self.assertEqual(api.media_edits[0]["file_id"], "fid2")
        self.assertEqual(api.media_edits[0]["message_id"], MESSAGE_ID)

    def test_stepping_forward_does_not_resend_the_keyboard(self):
        api = self.press("next", caption=_caption(0))
        self.assertIsNone(api.media_edits[0]["reply_markup"])

    def test_stepping_back_does_not_resend_the_keyboard(self):
        api = self.press("prev", caption=_caption(2))
        self.assertTrue(self._stepped_to(api).startswith("Работа 2 из 3"))
        self.assertIsNone(api.media_edits[0]["reply_markup"])

    def test_the_five_step_jumps_do_not_resend_the_keyboard(self):
        self.save(_poll(count=12))
        forward = self.press("fwd5", caption=_caption(0, 12))
        self.assertTrue(forward.media_edits[0]["caption"].startswith("Работа 6 из 12"))
        self.assertIsNone(forward.media_edits[0]["reply_markup"])
        backward = self.press("back5", caption=_caption(9, 12))
        self.assertTrue(backward.media_edits[0]["caption"].startswith("Работа 5 из 12"))
        self.assertIsNone(backward.media_edits[0]["reply_markup"])

    def test_coming_back_from_the_list_view_does_pass_the_keyboard(self):
        """The list view replaced the photo keyboard with a grid of jump buttons, so this
        one edit genuinely has to put the photo keyboard back."""
        api = self.press("go", "2", caption="Работы на голосовании:\n\n1. Автор 1")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 3 из 3"))
        self.assertEqual(api.media_edits[0]["reply_markup"], bot_listener._carousel_keyboard())

    def test_forward_on_the_last_work_stays_on_the_last_work(self):
        api = self.press("next", caption=_caption(2))
        self.assertTrue(self._stepped_to(api).startswith("Работа 3 из 3"))

    def test_back_on_the_first_work_stays_on_the_first_work(self):
        api = self.press("prev", caption=_caption(0))
        self.assertTrue(self._stepped_to(api).startswith("Работа 1 из 3"))

    def test_a_five_step_jump_clamps_to_both_ends(self):
        forward = self.press("fwd5", caption=_caption(0))
        self.assertTrue(forward.media_edits[0]["caption"].startswith("Работа 3 из 3"))
        backward = self.press("back5", caption=_caption(2))
        self.assertTrue(backward.media_edits[0]["caption"].startswith("Работа 1 из 3"))

    def test_a_jump_to_a_number_that_no_longer_exists_clamps(self):
        api = self.press("go", "99", caption="Работы на голосовании:")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 3 из 3"))

    def test_a_jump_with_an_unparseable_argument_falls_back_to_the_first_work(self):
        api = self.press("go", "not-a-number", caption="Работы на голосовании:")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 1 из 3"))

    def test_every_navigation_tap_answers_the_button(self):
        for action in ("prev", "next", "back5", "fwd5", "go"):
            api = self.press(action, "0", caption=_caption(1))
            self.assertEqual(len(api.answered), 1, action)

    def test_navigation_never_pays_for_a_membership_check(self):
        """Navigation is the overwhelming majority of taps; a getChatMember on each one
        would make browsing as slow as the check."""
        self.press("next", caption=_caption(0))
        self.assertEqual(self.member_checks, [])

    def test_an_entry_whose_photo_never_uploaded_edits_the_caption_instead(self):
        poll = _poll(count=2)
        poll.entries[1].file_ids = []
        self.save(poll)
        api = self.press("next", caption=_caption(0, 2))
        self.assertEqual(api.media_edits, [])
        self.assertIn("(фото не загрузилось)", api.caption_edits[0]["caption"])

    def test_a_tap_on_a_poll_that_no_longer_exists_says_so_instead_of_crashing(self):
        for path in Path(self._temporary.name).glob("*.json"):
            path.unlink()
        api = self.press("next", caption=_caption(0))
        self.assertEqual(api.answered, ["Голосование ещё не создано."])
        self.assertEqual(api.edits, [])

    def test_a_callback_with_no_message_is_still_answered(self):
        api = self.press("next", with_message=False)
        self.assertEqual(len(api.answered), 1)
        self.assertEqual(api.edits, [])


class CarouselTapCoalescingTests(CarouselTestCase):
    """Five quick presses of ▶ must not mean five sequential edits. Each tap claims a token
    in carousel_taps; a tap that has been overtaken by the time it is ready to draw drops
    itself and lets the newer one render."""

    def test_an_ordinary_single_tap_does_render(self):
        api = self.press("next", caption=_caption(0))
        self.assertEqual(len(api.media_edits), 1)

    def test_each_tap_claims_the_next_token(self):
        self.press("next", caption=_caption(0))
        self.assertEqual(self.taps[VOTER], 1)
        self.press("next", caption=_caption(1))
        self.assertEqual(self.taps[VOTER], 2)

    def test_a_tap_overtaken_by_a_newer_one_never_draws(self):
        """The newer tap lands while this one is awaiting answerCallbackQuery -- the same
        window a real second press arrives in."""
        api = FakeApi(on_answer=lambda: self.taps.__setitem__(VOTER, self.taps[VOTER] + 1))
        self.press("next", caption=_caption(0), api=api)
        self.assertEqual(len(api.answered), 1)  # still answered: the spinner must stop
        self.assertEqual(api.edits, [])

    def test_being_overtaken_still_saves_the_vote_it_was_asked_to_cast(self):
        """Coalescing may drop a redraw. It must never drop the write behind it."""
        api = FakeApi(on_answer=lambda: self.taps.__setitem__(VOTER, self.taps[VOTER] + 1))
        self.press("pick", caption=_caption(0), api=api)
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])
        self.assertEqual(api.edits, [])

    def test_one_voter_being_overtaken_does_not_silence_another(self):
        self.taps[OTHER_VOTER] = 9
        api = self.press("next", caption=_caption(0), user_id=VOTER)
        self.assertEqual(len(api.media_edits), 1)
        self.assertEqual(self.taps[OTHER_VOTER], 9)


class CarouselVotingTests(CarouselTestCase):
    """✅ Голосовать: a toggle on the work currently displayed, written straight into the
    same poll file the Mini App writes."""

    def test_picking_records_the_vote_and_says_so(self):
        api = self.press("pick", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e2"])
        self.assertEqual(api.answered, ["Голос учтён"])

    def test_the_new_choice_is_marked_in_the_redrawn_caption(self):
        api = self.press("pick", caption=_caption(1))
        self.assertIn("✔ Твой голос за эту работу", api.media_edits[0]["caption"])
        self.assertIn("выбрано 1", api.media_edits[0]["caption"])

    def test_picking_the_same_work_again_takes_the_vote_back(self):
        self.save(_poll(votes={str(VOTER): ["e2"]}))
        api = self.press("pick", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], [])
        self.assertEqual(api.answered, ["Голос снят"])

    def test_a_second_work_can_be_added_to_a_ballot(self):
        self.save(_poll(votes={str(VOTER): ["e1"]}))
        self.press("pick", caption=_caption(2))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1", "e3"])

    def test_max_choices_is_enforced(self):
        self.save(_poll(max_choices=1, votes={str(VOTER): ["e1"]}))
        api = self.press("pick", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])
        self.assertEqual(len(api.answered), 1)
        self.assertIn("не более", api.answered[0])

    def test_a_full_ballot_can_still_take_a_vote_back(self):
        """The limit is on adding. A voter who has spent every choice must still be able
        to free one up, or the button becomes a trap."""
        self.save(_poll(max_choices=1, votes={str(VOTER): ["e1"]}))
        self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes[str(VOTER)], [])

    def test_a_first_vote_is_allowed_even_when_revoting_is_off(self):
        self.save(_poll(allow_revote=False))
        api = self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])
        self.assertEqual(api.answered, ["Голос учтён"])

    def test_a_locked_ballot_refuses_a_second_vote(self):
        self.save(_poll(allow_revote=False, votes={str(VOTER): ["e1"]}))
        api = self.press("pick", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])
        self.assertEqual(len(api.answered), 1)
        self.assertIn("Менять голос нельзя", api.answered[0])

    def test_the_lock_still_holds_after_a_real_first_vote(self):
        """The lock itself, reached the way a voter reaches it: one pick, then another."""
        self.save(_poll(allow_revote=False))
        self.press("pick", caption=_caption(0))
        api = self.press("pick", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])
        self.assertIn("Менять голос нельзя", api.answered[0])

    def test_a_ballot_emptied_by_hiding_does_not_lock_the_voter_out(self):
        """Regression: the lock used to be `str(user_id) in poll.votes` -- key presence,
        not "has a vote". A voter who picked a work and then hid it (which drops it from
        their ballot) was left with a recorded-but-empty ballot and could never vote at
        all: every later tap answered "Менять голос нельзя" while their ballot said []."""
        self.save(_poll(allow_revote=False))
        self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])

        self.press("hide", caption=_caption(0))
        self.assertEqual(self.stored().votes[str(VOTER)], [])  # recorded, but empty

        # Position 0 is now e2 -- e1 slid out of this voter's visible list.
        api = self.press("pick", caption=_caption(0, 2))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e2"])
        self.assertEqual(api.answered, ["Голос учтён"])

    def test_a_ballot_emptied_by_an_un_admit_does_not_lock_the_voter_out(self):
        """The same empty-ballot state, reached without hiding: record_vote drops choices
        that are no longer admitted, so an administrator un-admitting a voter's only pick
        empties their ballot too."""
        self.save(_poll(allow_revote=False, approved=["e2", "e3"], votes={str(VOTER): []}))
        api = self.press("pick", caption=_caption(0, 2))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e2"])
        self.assertEqual(api.answered, ["Голос учтён"])

    def test_another_voter_s_locked_ballot_does_not_lock_this_one(self):
        self.save(_poll(allow_revote=False, votes={str(OTHER_VOTER): ["e1"]}))
        self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])

    def test_a_closed_poll_refuses_a_vote(self):
        self.save(_poll(open=False))
        api = self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes, {})
        self.assertEqual(api.answered, ["Голосование уже закрыто."])

    def test_picking_when_there_is_nothing_admitted_still_answers(self):
        self.save(_poll(approved=[]))
        api = self.press("pick", caption=_caption(0))
        self.assertEqual(len(api.answered), 1)
        self.assertEqual(self.stored().votes, {})

    def test_the_vote_lands_on_the_work_the_caption_says_is_showing(self):
        """The only thing that decides *which* work a pick applies to is the position in
        the caption -- there is no server-side cursor to fall back on."""
        self.press("pick", caption=_caption(2))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e3"])

    def test_every_voting_path_answers_the_button(self):
        cases = [
            (_poll(), _caption(0)),
            (_poll(open=False), _caption(0)),
            (_poll(max_choices=1, votes={str(VOTER): ["e1"]}), _caption(1)),
            (_poll(allow_revote=False, votes={str(VOTER): ["e1"]}), _caption(1)),
            (_poll(approved=[]), _caption(0)),
        ]
        for poll, caption in cases:
            self.save(poll)
            api = self.press("pick", caption=caption)
            self.assertEqual(len(api.answered), 1, poll.to_dict())


class CarouselHidingTests(CarouselTestCase):
    """🙈 Скрыть is a personal filter, never a moderation tool: it changes what ONE voter
    sees and nothing else."""

    def test_hiding_takes_the_work_out_of_that_voter_s_list(self):
        self.press("hide", caption=_caption(1))
        poll = self.stored()
        self.assertEqual(
            [e.entry_id for e in voting.visible_entries(poll, VOTER)], ["e1", "e3"]
        )

    def test_hiding_leaves_another_voter_s_list_untouched(self):
        self.press("hide", caption=_caption(1))
        poll = self.stored()
        self.assertEqual(
            [e.entry_id for e in voting.visible_entries(poll, OTHER_VOTER)], ["e1", "e2", "e3"]
        )
        self.assertEqual(voting.hidden_for(poll, OTHER_VOTER), [])

    def test_hiding_leaves_the_admitted_set_alone(self):
        self.press("hide", caption=_caption(1))
        self.assertEqual(self.stored().approved, ["e1", "e2", "e3"])

    def test_hiding_a_work_you_voted_for_drops_it_from_your_ballot(self):
        self.save(_poll(votes={str(VOTER): ["e1", "e2"]}))
        self.press("hide", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(VOTER)], ["e1"])

    def test_hiding_on_an_open_poll_still_takes_the_vote_out_of_the_tally(self):
        """The other half of the closed-poll rule below: while the vote is running,
        dropping the vote is the whole point -- a vote for something the voter can no
        longer see is the confusing outcome."""
        self.save(_poll(votes={str(VOTER): ["e2"], str(OTHER_VOTER): ["e2"]}))
        self.press("hide", caption=_caption(1))
        self.assertEqual(
            [(e.entry_id, n) for e, n in self.stored().tally()],
            [("e2", 1), ("e1", 0), ("e3", 0)],
        )

    def test_hiding_on_a_closed_poll_never_touches_the_ballot_or_the_tally(self):
        """Regression: 🙈 is not gated on poll.open (deliberately -- tidying up a finished
        ballot is harmless), so before the fix a tap on a work the voter had chosen quietly
        retracted that vote AFTER the contest was over and rewrote the standings. The
        button still hides; it just no longer counts."""
        self.save(_poll(open=False, votes={str(VOTER): ["e1", "e2"], str(OTHER_VOTER): ["e2"]}))
        before = [(e.entry_id, n) for e, n in self.stored().tally()]

        self.press("hide", caption=_caption(1))  # 🙈 on e2, which this voter chose

        poll = self.stored()
        self.assertEqual(poll.votes[str(VOTER)], ["e1", "e2"])
        self.assertEqual(poll.votes[str(OTHER_VOTER)], ["e2"])
        self.assertEqual([(e.entry_id, n) for e, n in poll.tally()], before)
        self.assertEqual([(e.entry_id, n) for e, n in poll.tally()],
                         [("e2", 2), ("e1", 1), ("e3", 0)])
        # ...and it really did hide it, for this voter only.
        self.assertEqual(voting.hidden_for(poll, VOTER), ["e2"])
        self.assertEqual([e.entry_id for e in voting.visible_entries(poll, VOTER)], ["e1", "e3"])
        self.assertEqual(len(voting.visible_entries(poll, OTHER_VOTER)), 3)

    def test_hiding_does_not_touch_anybody_else_s_ballot(self):
        self.save(_poll(votes={str(VOTER): ["e2"], str(OTHER_VOTER): ["e2"]}))
        self.press("hide", caption=_caption(1))
        self.assertEqual(self.stored().votes[str(OTHER_VOTER)], ["e2"])

    def test_the_next_work_slides_into_the_hidden_one_s_place(self):
        """Hiding advances by standing still -- a first pass of "не моё" is one tap per
        rejection, not hide-then-next."""
        api = self.press("hide", caption=_caption(1))
        self.assertEqual(api.media_edits[0]["file_id"], "fid3")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 2 из 2 · скрыто 1"))

    def test_hiding_the_last_work_steps_back_onto_the_one_before_it(self):
        api = self.press("hide", caption=_caption(2))
        self.assertEqual(api.media_edits[0]["file_id"], "fid2")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 2 из 2"))

    def test_hiding_everything_leaves_a_way_back_rather_than_a_blank_card(self):
        self.save(_poll(count=1))
        api = self.press("hide", caption=_caption(0, 1))
        self.assertEqual(api.media_edits, [])
        self.assertIn("Все работы скрыты", api.caption_edits[0]["caption"])
        self.assertIn(
            "showall",
            api.caption_edits[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
        )

    def test_hiding_answers_the_button_and_says_it_is_private(self):
        api = self.press("hide", caption=_caption(0))
        self.assertEqual(len(api.answered), 1)
        self.assertIn("только у тебя", api.answered[0])

    def test_showall_brings_everything_back_for_that_voter(self):
        self.save(_poll(hidden={str(VOTER): ["e1", "e2"]}))
        api = self.press("showall", caption="Скрытые работы (только у тебя):")
        poll = self.stored()
        self.assertEqual(voting.hidden_for(poll, VOTER), [])
        self.assertEqual(len(voting.visible_entries(poll, VOTER)), 3)
        self.assertEqual(api.answered, ["Показаны все работы"])

    def test_showall_puts_the_photo_keyboard_back_on_the_first_work(self):
        self.save(_poll(hidden={str(VOTER): ["e1"]}))
        api = self.press("showall", caption="Скрытые работы (только у тебя):")
        self.assertEqual(api.media_edits[0]["file_id"], "fid1")
        self.assertEqual(api.media_edits[0]["reply_markup"], bot_listener._carousel_keyboard())

    def test_showall_leaves_another_voter_s_hidden_list_alone(self):
        self.save(_poll(hidden={str(VOTER): ["e1"], str(OTHER_VOTER): ["e2"]}))
        self.press("showall", caption="Скрытые работы (только у тебя):")
        self.assertEqual(voting.hidden_for(self.stored(), OTHER_VOTER), ["e2"])

    def test_unhide_brings_back_exactly_one_work(self):
        self.save(_poll(hidden={str(VOTER): ["e1", "e3"]}))
        api = self.press("unhide", "e3", caption="Скрытые работы (только у тебя):")
        poll = self.stored()
        self.assertEqual(voting.hidden_for(poll, VOTER), ["e1"])
        self.assertEqual([e.entry_id for e in voting.visible_entries(poll, VOTER)], ["e2", "e3"])
        self.assertEqual(api.answered, ["Работа возвращена"])

    def test_unhide_does_not_re_cast_the_vote_hiding_dropped(self):
        """The ballot is the voter's to rebuild -- silently re-casting a dropped vote is
        the same surprise in the other direction."""
        self.save(_poll(votes={str(VOTER): []}, hidden={str(VOTER): ["e2"]}))
        self.press("unhide", "e2", caption="Скрытые работы (только у тебя):")
        self.assertEqual(self.stored().votes[str(VOTER)], [])

    def test_unhiding_something_unknown_writes_no_phantom_id(self):
        api = self.press("unhide", "does-not-exist", caption="Скрытые работы (только у тебя):")
        self.assertEqual(voting.hidden_for(self.stored(), VOTER), [])
        self.assertEqual(len(api.answered), 1)

    def test_the_hidden_view_lists_what_is_hidden_with_a_way_back(self):
        self.save(_poll(hidden={str(VOTER): ["e2"]}))
        api = self.press("hidden", caption=_caption(0))
        text = api.caption_edits[0]["caption"]
        self.assertIn("Скрытые работы", text)
        self.assertIn("Автор 2", text)
        self.assertEqual(len(api.answered), 1)

    def test_the_hidden_view_says_so_when_nothing_is_hidden(self):
        api = self.press("hidden", caption=_caption(0))
        self.assertIn("Ты ничего не скрывал", api.caption_edits[0]["caption"])

    def test_the_list_view_offers_a_jump_button_per_visible_work(self):
        self.save(_poll(hidden={str(VOTER): ["e2"]}))
        api = self.press("list", caption=_caption(0))
        rows = api.caption_edits[0]["reply_markup"]["inline_keyboard"]
        jumps = [b["callback_data"] for row in rows for b in row]
        self.assertEqual(jumps[:2], ["votecar:go:0", "votecar:go:1"])
        self.assertNotIn("Автор 2", api.caption_edits[0]["caption"])

    def test_the_list_view_remembers_where_the_voter_was(self):
        api = self.press("list", caption=_caption(2))
        rows = api.caption_edits[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[-1][0]["callback_data"], "votecar:go:2")

    def test_done_clears_the_keyboard_and_reports_the_ballot(self):
        self.save(_poll(votes={str(VOTER): ["e1", "e3"]}))
        api = self.press("done", caption=_caption(0))
        self.assertIn("Выбрано работ: 2", api.caption_edits[0]["caption"])
        self.assertEqual(api.caption_edits[0]["reply_markup"], {"inline_keyboard": []})


class CarouselMembershipTests(CarouselTestCase):
    """The same "голосовать могут только участники чата" gate the Mini App applies --
    checked when the ballot is opened and again on every vote."""

    def test_opening_the_ballot_sends_the_first_photo_to_a_member(self):
        api = self.press("open")
        self.assertEqual(len(api.photos), 1)
        self.assertEqual(api.photos[0]["file_id"], "fid1")
        self.assertTrue(api.photos[0]["caption"].startswith("Работа 1 из 3"))
        self.assertEqual(api.photos[0]["reply_markup"], bot_listener._carousel_keyboard())
        self.assertEqual(len(api.answered), 1)

    def test_opening_the_ballot_refuses_a_non_member(self):
        self.member_ok = False
        api = self.press("open")
        self.assertEqual(api.photos, [])
        self.assertEqual(len(api.messages), 1)
        self.assertIn("только участники чата", api.messages[0]["text"])
        self.assertEqual(len(api.answered), 1)

    def test_picking_records_nothing_for_a_non_member(self):
        self.member_ok = False
        api = self.press("pick", caption=_caption(0))
        self.assertEqual(self.stored().votes, {})
        self.assertEqual(len(api.answered), 1)
        self.assertIn("только участники чата", api.answered[0])

    def test_picking_checks_membership_against_the_home_chat(self):
        self.press("pick", caption=_caption(0))
        self.assertEqual(self.member_checks, [ENTRY])

    def test_opening_onto_an_unmoderated_poll_says_why_it_is_empty(self):
        self.save(_poll(approved=[]))
        api = self.press("open")
        self.assertEqual(api.photos, [])
        self.assertIn("ещё не допущены", api.messages[0]["text"])

    def test_opening_with_no_photos_anywhere_asks_for_a_re_collect(self):
        # No file_ids AND no files on disk to warm from -- the only case left where the
        # ballot genuinely cannot open. See CarouselLazyWarmTests for the case that matters
        # in production, where the pictures are there and simply have not been uploaded yet.
        self.save(_poll(file_ids=False))
        api = self.press("open")
        self.assertEqual(api.photos, [])
        self.assertIn("Фотографии работ ещё не готовы", api.messages[0]["text"])


class CarouselLazyWarmTests(CarouselTestCase):
    """A poll collected BEFORE the in-chat ballot existed has no file_ids at all, and there
    was a live one of those with real votes in it. The ballot has to warm those photos on
    demand rather than dead-ending, and that write must touch nothing but file_ids.
    """

    PHOTO_RESPONSE = {"message_id": 9, "photo": [{"file_id": "small"}, {"file_id": "big"}]}

    def _cold_poll(self, on_disk=(1, 2, 3), **fields):
        """A saved poll whose entries carry media names but no file_ids -- exactly the shape
        of a poll collected before this feature. `on_disk` is which entries' pictures were
        actually downloaded at collect time."""
        poll = _poll(file_ids=False, **fields)
        self.save(poll)
        media_dir = voting.media_path(ENTRY, POLL_ID)
        media_dir.mkdir(parents=True, exist_ok=True)
        for number in on_disk:
            (media_dir / f"photo{number}.jpg").write_bytes(b"not-really-a-jpeg")
        return poll

    def test_opening_a_cold_poll_uploads_the_work_and_shows_it(self):
        self._cold_poll()
        api = FakeApi(photo_response=self.PHOTO_RESPONSE)
        self.press("open", api=api)
        self.assertEqual(api.uploads, ["photo1.jpg"])
        self.assertEqual(len(api.photos), 1)
        self.assertEqual(api.photos[0]["file_id"], "big")
        self.assertTrue(api.photos[0]["caption"].startswith("Работа 1 из 3"))
        # The carrier message goes immediately -- a file_id outlives the message it came on.
        self.assertEqual(api.deleted, [(VOTER, 9)])

    def test_the_warmed_file_id_is_saved_so_nobody_uploads_it_twice(self):
        self._cold_poll()
        self.press("open", api=FakeApi(photo_response=self.PHOTO_RESPONSE))
        self.assertEqual(self.stored().entries[0].file_ids, ["big"])

        second = FakeApi(photo_response=self.PHOTO_RESPONSE)
        self.press("open", api=second, user_id=OTHER_VOTER)
        self.assertEqual(second.uploads, [], "the second voter paid for an upload again")
        self.assertEqual(second.photos[0]["file_id"], "big")

    def test_warming_leaves_every_vote_and_hidden_list_alone(self):
        # The whole risk of writing to a live poll: this one already has ballots in it.
        poll = self._cold_poll()
        voting.record_vote(poll, VOTER, ["e1", "e2"])
        voting.record_vote(poll, OTHER_VOTER, ["e3"])
        voting.toggle_hidden(poll, OTHER_VOTER, "e1")
        self.save(poll)
        before = (self.stored().votes, self.stored().hidden, self.stored().approved)

        self.press("open", api=FakeApi(photo_response=self.PHOTO_RESPONSE))

        after = self.stored()
        self.assertEqual(after.votes, before[0])
        self.assertEqual(after.hidden, before[1])
        self.assertEqual(after.approved, before[2])
        self.assertEqual(after.entries[0].file_ids, ["big"])

    def test_a_work_whose_picture_never_downloaded_is_stepped_over(self):
        # One unrenderable nominee must not cost the voter the entire ballot.
        self._cold_poll(on_disk=(2, 3))
        api = FakeApi(photo_response=self.PHOTO_RESPONSE)
        self.press("open", api=api)
        self.assertEqual(api.uploads, ["photo2.jpg"])
        self.assertEqual(len(api.photos), 1)
        self.assertTrue(api.photos[0]["caption"].startswith("Работа 2 из 3"))

    def test_navigating_onto_a_cold_work_warms_it_in_place(self):
        self._cold_poll()
        api = FakeApi(photo_response=self.PHOTO_RESPONSE)
        self.press("next", caption=_caption(0), api=api)
        self.assertEqual(api.uploads, ["photo2.jpg"])
        self.assertEqual(len(api.media_edits), 1)
        self.assertEqual(api.media_edits[0]["file_id"], "big")
        self.assertTrue(api.media_edits[0]["caption"].startswith("Работа 2 из 3"))


class LargestPhotoFileIdTests(unittest.TestCase):
    """sendPhoto answers with every size Telegram generated, smallest first."""

    def test_the_last_size_is_the_one_kept(self):
        sent = {"photo": [{"file_id": "small"}, {"file_id": "medium"}, {"file_id": "big"}]}
        self.assertEqual(bot_listener._largest_photo_file_id(sent), "big")

    def test_a_response_with_no_photo_has_no_file_id(self):
        self.assertIsNone(bot_listener._largest_photo_file_id({"message_id": 1}))

    def test_no_response_at_all_has_no_file_id(self):
        self.assertIsNone(bot_listener._largest_photo_file_id(None))


class PrewarmEntryFileIdsTests(unittest.TestCase):
    """The upload-once step: the administrator's "/vote собрать" pays for every photo, so
    no voter ever waits on an upload. Best-effort throughout -- a nominee whose upload
    fails keeps an empty file_ids and is skipped by the carousel, rather than failing the
    whole collect."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def _poll_with_media(self, count=2, on_disk=None, **fields):
        poll = _poll(count=count, file_ids=False, **fields)
        media_dir = voting.media_path(ENTRY, POLL_ID)
        media_dir.mkdir(parents=True, exist_ok=True)
        names = [f"photo{n}.jpg" for n in range(1, count + 1)] if on_disk is None else on_disk
        for name in names:
            (media_dir / name).write_bytes(b"jpeg-bytes")
        return poll

    @staticmethod
    def _response(message_id=900):
        return {
            "message_id": message_id,
            "photo": [{"file_id": "small"}, {"file_id": "medium"}, {"file_id": "big"}],
        }

    def _warm(self, poll, api):
        return _run(bot_listener.prewarm_entry_file_ids(
            api, ADMIN_CHAT, poll, log=lambda *_: None
        ))

    def test_the_largest_file_id_is_stored_on_the_entry(self):
        poll = self._poll_with_media(count=1)
        api = FakeApi(photo_response=self._response())
        warmed = self._warm(poll, api)
        self.assertEqual(warmed, 1)
        self.assertEqual(poll.entries[0].file_ids, ["big"])

    def test_the_carrier_message_is_deleted_again(self):
        """The file_id outlives the message, so the upload leaves nothing behind in the
        admin's DM."""
        poll = self._poll_with_media(count=1)
        api = FakeApi(photo_response=self._response(message_id=904))
        self._warm(poll, api)
        self.assertEqual(api.deleted, [(ADMIN_CHAT, 904)])

    def test_every_entry_is_warmed(self):
        poll = self._poll_with_media(count=2)
        api = FakeApi(photo_response=self._response())
        self.assertEqual(self._warm(poll, api), 2)
        self.assertEqual(api.uploads, ["photo1.jpg", "photo2.jpg"])

    def test_an_entry_that_already_has_file_ids_is_never_re_uploaded(self):
        poll = self._poll_with_media(count=2)
        poll.entries[0].file_ids = ["already-warm"]
        api = FakeApi(photo_response=self._response())
        self.assertEqual(self._warm(poll, api), 1)
        self.assertEqual(api.uploads, ["photo2.jpg"])
        self.assertEqual(poll.entries[0].file_ids, ["already-warm"])

    def test_an_upload_that_raises_does_not_stop_the_rest_of_the_collect(self):
        poll = self._poll_with_media(count=2)
        api = FakeApi(photo_response=self._response(), failing_uploads=["photo1.jpg"])
        self.assertEqual(self._warm(poll, api), 1)
        self.assertEqual(poll.entries[0].file_ids, [])
        self.assertEqual(poll.entries[1].file_ids, ["big"])

    def test_an_entry_with_no_pictures_is_skipped(self):
        poll = _poll(count=1, file_ids=False)
        poll.entries[0].media = []
        api = FakeApi(photo_response=self._response())
        self.assertEqual(self._warm(poll, api), 0)
        self.assertEqual(api.uploads, [])

    def test_a_picture_missing_from_disk_is_skipped(self):
        poll = self._poll_with_media(count=1, on_disk=[])
        api = FakeApi(photo_response=self._response())
        self.assertEqual(self._warm(poll, api), 0)
        self.assertEqual(api.uploads, [])
        self.assertEqual(poll.entries[0].file_ids, [])

    def test_a_response_without_a_photo_warms_nothing_but_still_tidies_up(self):
        poll = self._poll_with_media(count=1)
        api = FakeApi(photo_response={"message_id": 907})
        self.assertEqual(self._warm(poll, api), 0)
        self.assertEqual(poll.entries[0].file_ids, [])
        self.assertEqual(api.deleted, [(ADMIN_CHAT, 907)])

    def test_the_warmed_file_ids_are_what_the_carousel_later_renders(self):
        poll = self._poll_with_media(count=1)
        api = FakeApi(photo_response=self._response())
        self._warm(poll, api)
        self.assertEqual(bot_listener._carousel_file_id(poll.entries[0]), "big")


class CarouselDispatchTests(unittest.TestCase):
    """The votecar: prefix has to actually reach its handler -- this codebase has been
    bitten before by callback routing bugs a leaf-handler test would never catch."""

    class _FakeApi:
        async def answer_callback_query(self, callback_id, text=None):
            pass

    def _dispatch(self, data: str):
        handled = []

        async def handle(*args, **kwargs):
            handled.append(args[2])  # (api, telethon_client, callback, ...)

        async def go():
            background_tasks: set = set()
            with patch.object(bot_listener, "handle_vote_carousel_callback", handle):
                await bot_listener._dispatch_update(
                    {"callback_query": {
                        "id": "cbq1", "data": data,
                        "from": {"id": VOTER}, "message": {"message_id": MESSAGE_ID},
                    }},
                    self._FakeApi(), None, None, None, None, 1, set(), asyncio.Queue(),
                    background_tasks, ENTRY, {}, {}, {}, {}, {},
                    log=lambda *_: None,
                )
            if background_tasks:
                await asyncio.gather(*background_tasks)

        _run(go())
        return handled

    def test_a_carousel_button_reaches_its_handler(self):
        handled = self._dispatch(bot_listener._carousel_callback_data("next"))
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["data"], "votecar:next:")

    def test_an_unrelated_callback_does_not_reach_it(self):
        self.assertEqual(self._dispatch("someotherprefix:whatever"), [])


if __name__ == "__main__":
    unittest.main()
