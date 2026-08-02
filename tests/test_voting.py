"""voting.py: album grouping into entries, the initData signature check, and the poll
(moderation + one-ballot-per-user + tally)."""

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import voting

BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


def _msg(id, text="", grouped_id=None):
    return SimpleNamespace(id=id, text=text, grouped_id=grouped_id)


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

    def test_dict_round_trip_preserves_everything(self):
        poll = self._poll()
        voting.set_approved(poll, ["a"])
        voting.record_vote(poll, 7, ["a"])
        restored = voting.Poll.from_dict(json.loads(json.dumps(poll.to_dict())))
        self.assertEqual(restored.approved, poll.approved)
        self.assertEqual(restored.votes, poll.votes)
        self.assertEqual([e.entry_id for e in restored.entries], [e.entry_id for e in poll.entries])


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

    def test_a_first_collection_has_no_existing_poll_to_merge(self):
        fresh = [self._entry("a")]
        merged = voting.build_poll("Chat", "p", fresh, existing=None)
        self.assertEqual(merged.approved, [])
        self.assertEqual(merged.votes, {})


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

    def test_no_poll_directory_yet_is_not_an_error(self):
        self.assertIsNone(voting.latest_poll("anything"))


if __name__ == "__main__":
    unittest.main()
