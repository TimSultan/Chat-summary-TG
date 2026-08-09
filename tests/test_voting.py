"""voting.py: album grouping into entries, the initData signature check, and the poll
(moderation + one-ballot-per-user + tally)."""

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import voting

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


def _msg(id, text="", grouped_id=None):
    return SimpleNamespace(id=id, text=text, grouped_id=grouped_id)


class _FakeMessage:
    """Just enough of a Telethon Message for collect_entries: an id/text/grouped_id/date
    to group and filter by, a `photo` truthy flag, and an async get_sender() that records
    into `resolved` -- how the tests confirm a SKIPPED (already-known) entry never gets
    this far."""

    def __init__(self, id, text="", grouped_id=None, date=None, photo=True, sender_id=1, resolved=None):
        self.id = id
        self.text = text
        self.grouped_id = grouped_id
        self.date = date or datetime.now(timezone.utc)
        self.photo = photo
        self.action = None
        self._sender = SimpleNamespace(id=sender_id, username=f"user{sender_id}")
        self._resolved = resolved if resolved is not None else []

    async def get_sender(self):
        self._resolved.append(self.id)
        return self._sender


class _FakeClient:
    """Just enough of a Telethon client for collect_entries -- newest-first message
    listing (matching real iter_messages(reverse=False)) and a download that only ever
    writes a marker file, tracking which message ids it was actually asked to touch."""

    def __init__(self, messages):
        self._messages = messages
        self.downloads: list[int] = []

    async def iter_messages(self, entity, reverse=False):
        for message in self._messages:
            yield message

    async def download_media(self, message, file=None):
        self.downloads.append(message.id)
        Path(file).write_bytes(b"fake-photo")


def _sign_init_data(fields: dict, bot_token: str = BOT_TOKEN) -> str:
    """Builds a real, correctly-signed initData string -- the same construction
    voting.verify_init_data checks -- so tests exercise the actual algorithm instead of a
    stand-in for it."""
    payload = dict(fields)
    check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _fields(user_id=111, username="voter", auth_date=None):
    return {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAEfake",
        "user": json.dumps({"id": user_id, "username": username, "first_name": "V"}),
    }


class GroupIntoEntriesTests(unittest.TestCase):
    def test_a_single_hashtagged_message_is_its_own_entry(self):
        messages = [_msg(1, "просто болтовня"), _msg(2, "работа #итогинедели")]
        groups = voting.group_into_entries(messages)
        self.assertEqual([[m.id for m in g] for g in groups], [[2]])

    def test_an_album_qualifies_if_any_message_in_it_carries_the_hashtag(self):
        # Real Telegram behavior: only one message in an album carries the caption.
        messages = [
            _msg(10, "", grouped_id=555),
            _msg(11, "моя лучшая работа #итогинедели", grouped_id=555),
            _msg(12, "", grouped_id=555),
        ]
        groups = voting.group_into_entries(messages)
        self.assertEqual(len(groups), 1)
        self.assertEqual([m.id for m in groups[0]], [10, 11, 12])  # sorted, not caption-first

    def test_an_album_with_no_hashtag_anywhere_is_not_nominated(self):
        messages = [_msg(20, "", grouped_id=7), _msg(21, "просто фото", grouped_id=7)]
        self.assertEqual(voting.group_into_entries(messages), [])

    def test_a_lookalike_hashtag_does_not_match(self):
        messages = [_msg(1, "#итогинеделиапрель")]
        self.assertEqual(voting.group_into_entries(messages), [])

    def test_newest_post_first(self):
        messages = [_msg(1, "#итогинедели"), _msg(2, "#итогинедели"), _msg(3, "#итогинедели")]
        groups = voting.group_into_entries(messages)
        self.assertEqual([g[0].id for g in groups], [3, 2, 1])

    def test_clean_caption_strips_the_hashtag_but_keeps_the_rest(self):
        group = [_msg(1, "моя работа #итогинедели  на этой неделе")]
        self.assertEqual(voting._clean_caption(group), "моя работа на этой неделе")


class CollectEntriesTests(unittest.IsolatedAsyncioTestCase):
    """collect_entries against a fake Telethon client -- in particular, that
    skip_entry_ids actually skips resolving/downloading already-known entries rather
    than just filtering the result afterwards."""

    async def asyncSetUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.media_dir = Path(self._temporary.name) / "media"
        self.resolved: list[int] = []

    async def asyncTearDown(self):
        self._temporary.cleanup()

    def _messages(self, *ids):
        return [
            _FakeMessage(i, text="работа #итогинедели", resolved=self.resolved)
            for i in ids
        ]

    async def test_a_first_collection_resolves_and_downloads_everything_found(self):
        client = _FakeClient(self._messages(1, 2))
        entries = await voting.collect_entries(client, object(), timezone.utc, self.media_dir, log=lambda *_: None)
        self.assertEqual({e.entry_id for e in entries}, {"1", "2"})
        self.assertEqual(sorted(self.resolved), [1, 2])
        self.assertEqual(sorted(client.downloads), [1, 2])

    async def test_already_known_entries_are_never_resolved_or_downloaded(self):
        client = _FakeClient(self._messages(1, 2, 3))
        entries = await voting.collect_entries(
            client, object(), timezone.utc, self.media_dir,
            skip_entry_ids={"1", "2"}, log=lambda *_: None,
        )
        # Only the genuinely new one comes back...
        self.assertEqual([e.entry_id for e in entries], ["3"])
        # ...and the known ones never even had get_sender()/download_media called on them
        # -- that's the whole point, not just that they're filtered from the result.
        self.assertEqual(self.resolved, [3])
        self.assertEqual(client.downloads, [3])

    async def test_posts_from_before_this_monday_are_left_in_their_own_week(self):
        """The contest window is Monday..now, so last week's posts must not be pulled in.

        Anchored to the real week start rather than a frozen clock: one message a second
        before Monday 00:00 and one exactly on it, newest first the way iter_messages
        yields them.
        """
        week_start = voting.contest_week_start(datetime.now(timezone.utc))
        client = _FakeClient([
            _FakeMessage(2, text="эта неделя #итогинедели", date=week_start,
                         resolved=self.resolved),
            _FakeMessage(1, text="прошлая неделя #итогинедели",
                         date=week_start - timedelta(seconds=1), resolved=self.resolved),
        ])

        entries = await voting.collect_entries(
            client, object(), timezone.utc, self.media_dir, log=lambda *_: None,
        )

        self.assertEqual([e.entry_id for e in entries], ["2"])
        self.assertEqual(self.resolved, [2])

    async def test_the_whole_week_is_collected_not_just_the_last_day_or_two(self):
        """Collecting happens on Sunday; everything posted since Monday has to be found."""
        week_start = voting.contest_week_start(datetime.now(timezone.utc))
        client = _FakeClient([
            _FakeMessage(day, text="работа #итогинедели",
                         date=week_start + timedelta(days=day, hours=12),
                         resolved=self.resolved)
            for day in reversed(range(7))
            if week_start + timedelta(days=day, hours=12) <= datetime.now(timezone.utc)
        ])

        entries = await voting.collect_entries(
            client, object(), timezone.utc, self.media_dir, log=lambda *_: None,
        )

        self.assertEqual(len(entries), len(client._messages))
        self.assertGreaterEqual(len(entries), 1)

    async def test_nothing_new_means_an_empty_list_not_an_error(self):
        client = _FakeClient(self._messages(1, 2))
        entries = await voting.collect_entries(
            client, object(), timezone.utc, self.media_dir,
            skip_entry_ids={"1", "2"}, log=lambda *_: None,
        )
        self.assertEqual(entries, [])
        self.assertEqual(self.resolved, [])
        self.assertEqual(client.downloads, [])


class ContestWeekStartTests(unittest.TestCase):
    """Monday 00:00 of whatever week the moment falls in -- the contest's own boundary."""

    def test_sunday_collection_reaches_back_to_that_weeks_monday(self):
        # 2026-08-09 is a Sunday; its week began Monday 2026-08-03.
        sunday = datetime(2026, 8, 9, 21, 30, tzinfo=timezone.utc)
        self.assertEqual(
            voting.contest_week_start(sunday),
            datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
        )

    def test_monday_is_its_own_week_start_and_never_reaches_back_seven_days(self):
        """Collecting a few minutes after midnight must not swallow the week just ended."""
        monday = datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(
            voting.contest_week_start(monday),
            datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_every_day_of_one_week_resolves_to_the_same_monday(self):
        monday = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        for offset in range(7):
            moment = monday + timedelta(days=offset, hours=13, minutes=7)
            with self.subTest(day=moment.strftime("%A")):
                self.assertEqual(voting.contest_week_start(moment), monday)

    def test_the_window_agrees_with_the_iso_week_the_poll_is_keyed_on(self):
        """The poll id uses isocalendar(); a different week start would silently mismatch."""
        for offset in range(21):
            moment = datetime(2026, 8, 3, 12, tzinfo=timezone.utc) + timedelta(days=offset)
            with self.subTest(day=moment.date()):
                self.assertEqual(
                    voting.contest_week_start(moment).isocalendar()[:2],
                    moment.isocalendar()[:2],
                )


class InitDataTests(unittest.TestCase):
    def test_a_correctly_signed_payload_is_accepted(self):
        raw = _sign_init_data(_fields(user_id=42, username="anzhelika"))
        user = voting.verify_init_data(raw, BOT_TOKEN)
        self.assertEqual(user["id"], 42)
        self.assertEqual(user["username"], "anzhelika")

    def test_a_tampered_field_is_rejected(self):
        raw = _sign_init_data(_fields(user_id=42))
        # Flip the user id after signing -- same shape of attack as editing the URL by hand.
        tampered = raw.replace("%22id%22%3A+42", "%22id%22%3A+99")
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data(tampered, BOT_TOKEN)

    def test_signed_by_a_different_bot_token_is_rejected(self):
        raw = _sign_init_data(_fields(), bot_token="999999:OTHER-BOT")
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data(raw, BOT_TOKEN)

    def test_missing_hash_is_rejected(self):
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data(urlencode(_fields()), BOT_TOKEN)

    def test_empty_init_data_is_rejected(self):
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data("", BOT_TOKEN)

    def test_an_old_auth_date_is_rejected(self):
        raw = _sign_init_data(_fields(auth_date=time.time() - 999_999))
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data(raw, BOT_TOKEN, max_age_seconds=3600)

    def test_no_bot_token_configured_is_rejected_rather_than_trusted(self):
        raw = _sign_init_data(_fields())
        with self.assertRaises(voting.InitDataError):
            voting.verify_init_data(raw, "")


class PollTests(unittest.TestCase):
    def _poll(self, entry_ids=("a", "b", "c")):
        entries = [
            voting.Entry(entry_id=eid, message_id=int(eid, 36), author_id=1, author_name="A", author_username=None, text="")
            for eid in entry_ids
        ]
        return voting.Poll(poll_id="2026-08-02", entry="Chat", created_at="2026-08-02T00:00:00+00:00", entries=entries)

    def test_only_approved_entries_are_visible_or_countable(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "c"])
        self.assertEqual([e.entry_id for e in poll.approved_entries()], ["a", "c"])

    def test_set_approved_drops_unknown_ids(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "does-not-exist"])
        self.assertEqual(poll.approved, ["a"])

    def test_voting_again_replaces_the_previous_ballot(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "b"])
        voting.record_vote(poll, 555, ["a"])
        voting.record_vote(poll, 555, ["b"])
        self.assertEqual(poll.votes["555"], ["b"])

    def test_a_vote_for_an_unapproved_entry_is_dropped(self):
        poll = self._poll()
        voting.set_approved(poll, ["a"])
        voting.record_vote(poll, 1, ["a", "b"])  # "b" never admitted
        self.assertEqual(poll.votes["1"], ["a"])

    def test_tally_counts_only_approved_entries_and_ranks_by_votes(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "b", "c"])
        voting.record_vote(poll, 1, ["a"])
        voting.record_vote(poll, 2, ["a"])
        voting.record_vote(poll, 3, ["b"])
        ranked = poll.tally()
        self.assertEqual([(e.entry_id, c) for e, c in ranked], [("a", 2), ("b", 1), ("c", 0)])

    def test_un_admitting_an_entry_drops_its_votes_from_the_tally(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "b"])
        voting.record_vote(poll, 1, ["a"])
        voting.set_approved(poll, ["b"])  # "a" un-admitted after the vote was cast
        ranked = poll.tally()
        self.assertEqual([e.entry_id for e, _ in ranked], ["b"])

    def test_close_and_announce_picks_the_top_voted_entry_and_closes_the_poll(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "b"])
        voting.record_vote(poll, 1, ["a"])
        voting.record_vote(poll, 2, ["a"])
        voting.record_vote(poll, 3, ["b"])

        result = voting.close_and_announce(poll)

        self.assertIsNotNone(result)
        winner, votes = result
        self.assertEqual(winner.entry_id, "a")
        self.assertEqual(votes, 2)
        self.assertFalse(poll.open)
        self.assertEqual(poll.winner_entry_id, "a")
        self.assertEqual(poll.winner().entry_id, "a")

    def test_close_and_announce_refuses_when_nobody_voted(self):
        poll = self._poll()
        voting.set_approved(poll, ["a"])
        result = voting.close_and_announce(poll)
        self.assertIsNone(result)
        self.assertTrue(poll.open)  # untouched -- nothing to announce
        self.assertIsNone(poll.winner_entry_id)

    def test_close_and_announce_refuses_when_nothing_is_admitted_yet(self):
        poll = self._poll()  # no set_approved call at all
        self.assertIsNone(voting.close_and_announce(poll))

    def test_closing_again_recomputes_rather_than_refusing(self):
        poll = self._poll()
        voting.set_approved(poll, ["a", "b"])
        voting.record_vote(poll, 1, ["a"])
        voting.close_and_announce(poll)
        voting.record_vote(poll, 2, ["b"])
        voting.record_vote(poll, 3, ["b"])
        winner, votes = voting.close_and_announce(poll)
        self.assertEqual(winner.entry_id, "b")
        self.assertEqual(votes, 2)

    def test_dict_round_trip_preserves_everything(self):
        poll = self._poll()
        voting.set_approved(poll, ["a"])
        voting.record_vote(poll, 7, ["a"])
        voting.close_and_announce(poll)
        poll.max_choices = 2
        poll.allow_revote = False
        restored = voting.Poll.from_dict(json.loads(json.dumps(poll.to_dict())))
        self.assertEqual(restored.approved, poll.approved)
        self.assertEqual(restored.votes, poll.votes)
        self.assertEqual(restored.open, poll.open)
        self.assertEqual(restored.winner_entry_id, poll.winner_entry_id)
        self.assertEqual(restored.max_choices, 2)
        self.assertEqual(restored.allow_revote, False)
        self.assertEqual([e.entry_id for e in restored.entries], [e.entry_id for e in poll.entries])

    def test_settings_default_to_unlimited_and_revote_allowed(self):
        poll = self._poll()
        self.assertIsNone(poll.max_choices)
        self.assertTrue(poll.allow_revote)


class BuildPollMergeTests(unittest.TestCase):
    def _entry(self, entry_id):
        return voting.Entry(entry_id=entry_id, message_id=1, author_id=1, author_name="A", author_username=None, text="")

    def test_re_collecting_keeps_prior_moderation_and_votes(self):
        existing = voting.Poll(poll_id="p", entry="Chat", created_at="t0", entries=[self._entry("a"), self._entry("b")])
        voting.set_approved(existing, ["a"])
        voting.record_vote(existing, 1, ["a"])

        # Re-collected: "a" still there, "b" gone (deleted post), "c" is new.
        fresh = [self._entry("a"), self._entry("c")]
        merged = voting.build_poll("Chat", "p", fresh, existing=existing)

        self.assertEqual(merged.approved, ["a"])
        self.assertEqual(merged.votes, {"1": ["a"]})
        self.assertEqual(merged.created_at, "t0")  # the poll's identity doesn't reset
        self.assertEqual([e.entry_id for e in merged.entries], ["a", "c"])

    def test_re_collecting_keeps_the_max_choices_and_allow_revote_settings(self):
        existing = voting.Poll(poll_id="p", entry="Chat", created_at="t0", entries=[self._entry("a")])
        existing.max_choices = 2
        existing.allow_revote = False

        merged = voting.build_poll("Chat", "p", [self._entry("a")], existing=existing)

        self.assertEqual(merged.max_choices, 2)
        self.assertFalse(merged.allow_revote)

    def test_a_first_collection_has_no_existing_poll_to_merge(self):
        fresh = [self._entry("a")]
        merged = voting.build_poll("Chat", "p", fresh, existing=None)
        self.assertEqual(merged.approved, [])
        self.assertEqual(merged.votes, {})

    def test_a_recorded_winner_carries_over_if_still_present(self):
        existing = voting.Poll(poll_id="p", entry="Chat", created_at="t0", entries=[self._entry("a")])
        voting.set_approved(existing, ["a"])
        voting.record_vote(existing, 1, ["a"])
        voting.close_and_announce(existing)

        merged = voting.build_poll("Chat", "p", [self._entry("a")], existing=existing)
        self.assertEqual(merged.winner_entry_id, "a")

    def test_a_recorded_winner_is_dropped_if_its_post_is_gone(self):
        existing = voting.Poll(poll_id="p", entry="Chat", created_at="t0", entries=[self._entry("a")])
        voting.set_approved(existing, ["a"])
        voting.record_vote(existing, 1, ["a"])
        voting.close_and_announce(existing)

        merged = voting.build_poll("Chat", "p", [self._entry("b")], existing=existing)
        self.assertIsNone(merged.winner_entry_id)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        patcher = patch("voting._voting_dir", return_value=Path(self._temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_save_then_load_round_trips(self):
        poll = voting.Poll(poll_id="2026-08-02", entry="Chat", created_at="t0", entries=[])
        voting.set_approved(poll, [])
        voting.save_poll(poll)
        loaded = voting.load_poll("Chat", "2026-08-02")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.poll_id, "2026-08-02")

    def test_loading_a_poll_that_was_never_saved_returns_none(self):
        self.assertIsNone(voting.load_poll("Chat", "nope"))

    def test_latest_poll_picks_the_newest_by_created_at(self):
        older = voting.Poll(poll_id="2026-08-01", entry="Chat", created_at="2026-08-01T00:00:00+00:00", entries=[])
        newer = voting.Poll(poll_id="2026-08-02", entry="Chat", created_at="2026-08-02T00:00:00+00:00", entries=[])
        voting.save_poll(older)
        voting.save_poll(newer)
        latest = voting.latest_poll("Chat")
        self.assertEqual(latest.poll_id, "2026-08-02")

    def test_latest_poll_is_scoped_to_its_own_chat(self):
        voting.save_poll(voting.Poll(poll_id="p", entry="Chat A", created_at="t0", entries=[]))
        self.assertIsNone(voting.latest_poll("Chat B"))

    def test_delete_poll_removes_the_file_and_its_media(self):
        voting.save_poll(voting.Poll(poll_id="p", entry="Chat", created_at="t0", entries=[]))
        media_dir = voting.media_path("Chat", "p")
        media_dir.mkdir(parents=True)
        (media_dir / "photo.jpg").write_bytes(b"x")

        existed = voting.delete_poll("Chat", "p")

        self.assertTrue(existed)
        self.assertIsNone(voting.load_poll("Chat", "p"))
        self.assertFalse(media_dir.exists())

    def test_deleting_a_poll_that_never_existed_reports_so(self):
        self.assertFalse(voting.delete_poll("Chat", "never-existed"))

    def test_deleting_one_poll_does_not_touch_another(self):
        voting.save_poll(voting.Poll(poll_id="p1", entry="Chat", created_at="t0", entries=[]))
        voting.save_poll(voting.Poll(poll_id="p2", entry="Chat", created_at="t1", entries=[]))
        voting.delete_poll("Chat", "p1")
        self.assertIsNone(voting.load_poll("Chat", "p1"))
        self.assertIsNotNone(voting.load_poll("Chat", "p2"))

    def test_no_poll_directory_yet_is_not_an_error(self):
        self.assertIsNone(voting.latest_poll("anything"))


if __name__ == "__main__":
    unittest.main()
