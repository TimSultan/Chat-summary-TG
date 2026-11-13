"""site_mirror.py -- the vote and the Hall of Fame, mirrored to strangerhands.com/competition.

What leaves the bot is checked here as much as how: no Telegram id, no ballot, no count of
a vote still open, and nothing pushed twice.
"""

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hall_of_fame
import site_mirror
import voting
from tests.async_case import AsyncTestCase

CHAT = "Chat"
WEEK = "2026-W41"


def _entry(entry_id, name=None):
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id) + 100,
        author_name=name or f"Автор {entry_id}", author_username=f"user{entry_id}",
        text=f"работа {entry_id}", media=[f"{entry_id}_0.jpg"], posted_at="2026-10-01T12:00:00+03:00",
    )


class _Store:
    def _set_up_store(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=root / "hall"),
        ]
        for patcher in self._patchers:
            patcher.start()
        hall_of_fame._cache.clear()
        self.addCleanup(self._clean_up_store)

    def _clean_up_store(self):
        for patcher in self._patchers:
            patcher.stop()
        hall_of_fame._cache.clear()
        self._temporary.cleanup()

    def _poll(self, poll_id=WEEK, entries=None, admit=None, votes=None, close=False, **extra):
        entries = entries if entries is not None else [_entry(1), _entry(2), _entry(3)]
        poll = voting.Poll(poll_id=poll_id, entry=CHAT, created_at="2026-10-05T08:00:00+00:00", entries=entries, **extra)
        media = voting.media_path(CHAT, poll_id)
        media.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(entries):
            for name in entry.media:
                Image.new("RGB", (64, 48), (40 * index, 90, 160)).save(media / name)
        voting.set_approved(poll, admit if admit is not None else [e.entry_id for e in entries])
        for voter, choices in (votes or {}).items():
            voting.record_vote(poll, voter, choices)
        if close:
            voting.close_and_announce(poll)
        voting.save_poll(poll)
        return poll


class BuildTests(_Store, unittest.TestCase):
    def setUp(self):
        self._set_up_store()

    def test_the_live_vote_carries_only_admitted_works_and_no_ballots(self):
        self._poll(admit=["1", "3"], votes={"9": ["1"], "10": ["3", "1"]})
        payload, files, people = site_mirror.build_live(CHAT, site_mirror.FileHashes(), "https://t.me/chat")
        [poll] = payload["polls"]
        self.assertEqual([e["entry_id"] for e in poll["entries"]], ["1", "3"])
        self.assertEqual(poll["voters"], 2)
        self.assertTrue(poll["open"])
        self.assertIsNone(poll["winner"])
        self.assertEqual(poll["kind"], "weekly")
        text = json.dumps(payload, ensure_ascii=False)
        # No ballot, no per-work count, no Telegram id anywhere.
        self.assertNotIn("votes", poll["entries"][0])
        for telegram_id in ("101", "103"):
            self.assertNotIn(f'"{telegram_id}"', text)
            self.assertNotIn(f": {telegram_id}", text)
        self.assertEqual(poll["entries"][0]["post_url"], "https://t.me/chat/1")
        self.assertEqual(len(poll["entries"][0]["photos"]), 1)
        digest = poll["entries"][0]["photos"][0]
        self.assertEqual(hashlib.sha256(files[digest].read_bytes()).hexdigest(), digest)
        self.assertEqual(set(people), {site_mirror.artist_id(101, "Автор 1"), site_mirror.artist_id(103, "Автор 3")})

    def test_a_closed_vote_names_its_winner(self):
        self._poll(votes={"9": ["2"]}, close=True)
        payload, _, _ = site_mirror.build_live(CHAT, site_mirror.FileHashes())
        self.assertFalse(payload["polls"][0]["open"])
        self.assertEqual(payload["polls"][0]["winner"], "2")

    def test_a_painter_is_a_stable_hash_and_never_their_telegram_id(self):
        one = site_mirror.artist_id(865847612, "Юлия")
        self.assertEqual(one, site_mirror.artist_id(865847612, "Юлия Галимова"))  # a rename keeps it
        self.assertRegex(one, r"^[a-f0-9]{20}$")
        self.assertNotIn("865847612", one)
        self.assertNotEqual(site_mirror.artist_id(None, "Аня"), site_mirror.artist_id(None, "Оля"))

    def test_the_hall_carries_places_votes_medals_and_covers(self):
        poll = self._poll(votes={"9": ["2"], "10": ["2", "1"]}, close=True)
        hall_of_fame.record_poll(poll, poll.tally())
        payload, files, people = site_mirror.build_hall(CHAT, site_mirror.FileHashes(), None)
        [contest] = payload["contests"]
        self.assertEqual(contest["id"], WEEK)
        self.assertEqual([(w["entry_id"], w["place"], w["votes"], w["medal"]) for w in contest["works"]],
                         [("2", 1, 2, 1), ("1", 2, 1, 2), ("3", 3, 0, None)])
        self.assertTrue(all(w["thumb"] in files for w in contest["works"]))
        self.assertTrue(all(w["post_url"] is None for w in contest["works"]))
        self.assertEqual(len(people), 3)

    def test_a_monthly_round_says_which_medal_it_crowns(self):
        self._poll(poll_id="2026-10-bronze", hashtag=voting.MONTHLY_HASHTAG, tier=3,
                   title="Лучшая работа месяца · октябрь · бронза")
        payload, _, _ = site_mirror.build_live(CHAT, site_mirror.FileHashes())
        [poll] = payload["polls"]
        self.assertEqual(poll["kind"], "monthly")
        self.assertEqual(poll["round"], {"key": "bronze", "medal": 3})

    def test_a_missing_photo_is_left_out_not_sent_as_a_dangling_name(self):
        poll = self._poll()
        (voting.media_path(CHAT, poll.poll_id) / "2_0.jpg").unlink()
        payload, _, _ = site_mirror.build_live(CHAT, site_mirror.FileHashes())
        self.assertEqual([len(e["photos"]) for e in payload["polls"][0]["entries"]], [1, 0, 1])


class _FakeSite:
    """The site's three endpoints, in memory."""

    def __init__(self):
        self.photos: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.hall = None
        self.live = None

    async def post(self, path, payload):
        self.calls.append(path)
        if path == "/api/competition/photos" and "hashes" in payload:
            return {"missing": [h for h in payload["hashes"] if h not in self.photos]}
        if path == "/api/competition/photos":
            data = base64.b64decode(payload["data"])
            assert hashlib.sha256(data).hexdigest() == payload["hash"]
            self.photos[payload["hash"]] = data
            return {"ok": True}
        if path == "/api/competition/hall":
            self.hall = payload
            return {"ok": True}
        if path == "/api/competition/live":
            self.live = payload
            return {"ok": True}
        raise AssertionError(path)


class MirrorTests(_Store, AsyncTestCase):
    def setUp(self):
        self._set_up_store()
        self.site = _FakeSite()
        self.avatars_asked = []

        async def avatar(user_id):
            self.avatars_asked.append(user_id)
            return b"\xff\xd8\xff" + bytes([user_id % 256]) * 20

        self.mirror = site_mirror.Mirror(CHAT, "https://strangerhands.example", "token", avatar=avatar, log=lambda *_: None)
        self.mirror._post = self.site.post

    async def test_a_push_uploads_every_photo_once_then_the_records(self):
        poll = self._poll(votes={"9": ["2"]}, close=True)
        hall_of_fame.record_poll(poll, poll.tally())
        result = await self.mirror.sync_once()
        self.assertEqual(result["pushed"], ["hall", "live"])
        named = set()
        for contest in self.site.hall["contests"]:
            for work in contest["works"]:
                named.update(work["photos"])
                named.add(work["thumb"])
        for artist in self.site.hall["artists"] + self.site.live["artists"]:
            self.assertIsNotNone(artist["avatar"])
            named.add(artist["avatar"])
        self.assertTrue(named <= set(self.site.photos))
        # The records come after the pictures they name, the hall before the live vote.
        self.assertEqual(self.site.calls[-2:], ["/api/competition/hall", "/api/competition/live"])

    async def test_nothing_is_pushed_when_nothing_changed(self):
        self._poll()
        await self.mirror.sync_once()
        calls = len(self.site.calls)
        result = await self.mirror.sync_once()
        self.assertEqual(result["pushed"], [])
        self.assertEqual(len(self.site.calls), calls)
        self.assertEqual(len(self.avatars_asked), 3)  # once a day, not once a minute

    async def test_admitting_a_work_pushes_the_live_vote_again_and_only_the_new_photo(self):
        poll = self._poll(admit=["1"])
        await self.mirror.sync_once()
        uploads_before = len(self.site.photos)
        voting.set_approved(poll, ["1", "2"])
        voting.save_poll(poll)
        result = await self.mirror.sync_once()
        self.assertEqual(result["pushed"], ["live"])
        self.assertEqual([e["entry_id"] for e in self.site.live["polls"][0]["entries"]], ["1", "2"])
        # The new work's photo, and its painter's avatar -- nothing already there.
        self.assertEqual(len(self.site.photos), uploads_before + 2)

    async def test_a_cleared_vote_leaves_the_site_with_no_vote(self):
        self._poll()
        await self.mirror.sync_once()
        voting.archive_all_polls(CHAT)
        await self.mirror.sync_once()
        self.assertEqual(self.site.live["polls"], [])

    async def test_a_photo_that_does_not_arrive_is_tried_again(self):
        self._poll()
        real_post = self.site.post
        failures = {"left": 1}

        async def flaky(path, payload):
            if path == "/api/competition/photos" and "data" in payload and failures["left"]:
                failures["left"] -= 1
                raise site_mirror.MirrorError("/api/competition/photos answered 503: storage down")
            return await real_post(path, payload)

        self.mirror._post = flaky
        with self.assertRaises(site_mirror.MirrorError):
            await self.mirror.sync_once()
        self.assertNotIn("live", self.mirror.pushed)
        await self.mirror.sync_once()
        self.assertIn("live", self.mirror.pushed)
        named = {p for e in self.site.live["polls"][0]["entries"] for p in e["photos"]}
        self.assertTrue(named <= set(self.site.photos))

    async def test_the_loop_survives_a_site_that_is_down(self):
        async def down(path, payload):
            raise site_mirror.MirrorError(f"{path} answered 502: bad gateway")

        self.mirror._post = down
        self.mirror.interval = 0.01
        task = asyncio.create_task(self.mirror.run())
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class SettingsTests(unittest.TestCase):
    def test_the_mirror_is_off_without_an_address_and_a_token(self):
        with patch.dict("os.environ", {"STRANGERHANDS_MIRROR_URL": "", "STRANGERHANDS_MIRROR_TOKEN": "x"}):
            self.assertIsNone(site_mirror.mirror_from_env(CHAT))
        with patch.dict("os.environ", {"STRANGERHANDS_MIRROR_URL": "https://strangerhands.com/", "STRANGERHANDS_MIRROR_TOKEN": ""}):
            self.assertIsNone(site_mirror.mirror_from_env(CHAT))

    def test_plain_http_is_refused_except_on_this_machine(self):
        with patch.dict("os.environ", {"STRANGERHANDS_MIRROR_URL": "http://strangerhands.com", "STRANGERHANDS_MIRROR_TOKEN": "x"}):
            self.assertIsNone(site_mirror.mirror_from_env(CHAT, log=lambda *_: None))
        with patch.dict("os.environ", {"STRANGERHANDS_MIRROR_URL": "https://strangerhands.com/", "STRANGERHANDS_MIRROR_TOKEN": "x"}):
            mirror = site_mirror.mirror_from_env(CHAT)
            self.assertEqual(mirror.base_url, "https://strangerhands.com")


if __name__ == "__main__":
    unittest.main()
