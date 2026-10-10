"""Nothing the contests decided is lost: not by a clear, not by a narrower re-collect, not
by a lost disk.

- Every clear moves records AND photos into the archive (archive_store) -- it used to
  delete the photos, and the moderation screens' buttons deleted everything.
- A collect for narrower dates snapshots the poll before removing works from it.
- A Hall of Fame photo missing on disk is fetched back from its post in the chat.
- DATA_DIR follows an attached Railway Volume, and administrators are told when the
  history sits on a disk the next deploy wipes.
- /backup (and BACKUP_CHAT_ID after every closed vote) sends the history as a zip.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archive_store
import arena
import backup
import bot_listener
import hall_of_fame
import nominations
import storage
import voting

CHAT = "Chat"
WEEK = "2026-W41"
ADMIN = {"id": 7, "username": "admin"}
DELEGATE = {"id": 8, "username": "delegate"}
DM = 5


def _entry(entry_id, posted=date(2026, 10, 1)):
    return voting.Entry(
        entry_id=str(entry_id), message_id=int(entry_id), author_id=int(entry_id) + 100,
        author_name=f"Автор {entry_id}", author_username=None, text="", media=[f"{entry_id}_0.jpg"],
        posted_at=datetime.combine(posted, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12).isoformat(),
    )


class _Disk(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._patchers = [
            patch("voting._voting_dir", return_value=self.root / "voting"),
            patch("hall_of_fame._hall_dir", return_value=self.root / "hall_of_fame"),
            patch("arena._arena_dir", return_value=self.root / "arena"),
            patch("nominations._nominations_dir", return_value=self.root / "nominations"),
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


class ArchiveStoreTests(_Disk):
    def test_a_record_and_its_photos_move_together_under_one_name(self):
        record = self.root / "live" / "week.json"
        media = self.root / "live" / "media" / "week"
        media.mkdir(parents=True)
        record.write_text("{}")
        (media / "a.jpg").write_bytes(b"a")
        archive = self.root / "archive"

        moved = archive_store.move_to_archive(record, media, archive)

        self.assertEqual(moved, archive / "week.json")
        self.assertFalse(record.exists() or media.exists())
        self.assertEqual((archive / "media" / "week" / "a.jpg").read_bytes(), b"a")
        self.assertEqual(archive_store.archived_media_dirs(archive, "week"), [archive / "media" / "week"])

    def test_archiving_the_same_name_twice_keeps_both(self):
        archive = self.root / "archive"
        for content in (b"first", b"second"):
            record = self.root / "week.json"
            media = self.root / "media"
            media.mkdir()
            record.write_text(content.decode())
            (media / "a.jpg").write_bytes(content)
            archive_store.move_to_archive(record, media, archive)
        self.assertEqual(len(list(archive.glob("week*.json"))), 2)
        kept = sorted((d / "a.jpg").read_bytes() for d in archive_store.archived_media_dirs(archive, "week"))
        self.assertEqual(kept, [b"first", b"second"])

    def test_nothing_to_archive_is_nothing(self):
        self.assertIsNone(archive_store.move_to_archive(self.root / "none.json", None, self.root / "a"))

    def test_a_snapshot_is_a_copy_in_a_directory_of_its_own(self):
        record = self.root / "week.json"
        record.write_text('{"v": 1}')
        archive = self.root / "archive"
        first = archive_store.snapshot(record, archive)
        second = archive_store.snapshot(record, archive)
        self.assertTrue(record.exists())
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, archive / "snapshots")
        self.assertEqual(list(archive.glob("*.json")), [])


class ClearsKeepEverythingTests(_Disk):
    def test_the_arena_page_clear_archives_its_photos(self):
        tournament = arena.Tournament(tournament_id=WEEK, entry=CHAT, created_at="t", entries=[_entry("1")])
        arena.save_tournament(tournament)
        media = arena.media_path(CHAT, WEEK)
        media.mkdir(parents=True)
        (media / "1_0.jpg").write_bytes(b"photo")

        self.assertTrue(arena.delete_tournament(CHAT, WEEK))

        self.assertIsNone(arena.load_tournament(CHAT, WEEK))
        self.assertEqual(len(list(arena.archive_dir().glob("*.json"))), 1)
        self.assertEqual(sorted(p.name for p in (arena.archive_dir() / "media").rglob("*.jpg")), ["1_0.jpg"])

    def test_a_cleared_week_is_still_counted_once_in_the_vote_statistics(self):
        poll = voting.Poll(poll_id=WEEK, entry=CHAT, created_at="t", entries=[_entry("1")])
        voting.set_approved(poll, ["1"])
        voting.record_vote(poll, 9, ["1"])
        voting.save_poll(poll)
        voting.snapshot_poll(CHAT, WEEK)          # a snapshot must not read as a second week
        voting.archive_all_polls(CHAT)
        self.assertEqual([row["voters"] for row in voting.weekly_vote_stats(CHAT)], [1])


class NarrowCollectTests(_Disk):
    def test_works_a_narrower_collect_removes_are_kept_in_a_snapshot(self):
        poll = voting.Poll(poll_id=WEEK, entry=CHAT, created_at="2026-10-01T00:00:00+00:00",
                           entries=[_entry("1", date(2026, 9, 20)), _entry("2", date(2026, 10, 1))])
        voting.set_approved(poll, ["1", "2"])
        voting.record_vote(poll, 42, ["1"])
        voting.save_poll(poll)

        async def collect_entries(**kwargs):
            return []

        async def resolve(*args, **kwargs):
            return -100

        async def can_manage(api, chat_id, user, entry=None):
            return True

        class _Today(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 5, 15, 0, tzinfo=tz or timezone.utc)

        class Api:
            async def send_message(self, *args, **kwargs):
                return {"message_id": 1}

            async def edit_message_text(self, *args, **kwargs):
                return None

        message = {"message_id": 1, "chat": {"id": DM, "type": "private"}, "from": ADMIN,
                   "text": "/vote собрать 28.09 05.10"}
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_can_manage_chat", can_manage), \
                patch.object(voting, "collect_entries", collect_entries), \
                patch.object(bot_listener, "_current_vote_poll_id", lambda tz: WEEK), \
                patch.object(bot_listener, "datetime", _Today):
            asyncio.run(bot_listener.handle_vote_command(
                Api(), None, SimpleNamespace(webapp_public_url="https://e.com", vote_miniapp_short_name=None),
                timezone.utc, message, CHAT, "bot", set(), log=lambda *_: None,
            ))

        self.assertEqual([e.entry_id for e in voting.load_poll(CHAT, WEEK).entries], ["2"])
        (snapshot,) = (voting.archive_dir() / "snapshots").glob("*.json")
        kept = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual([e["entry_id"] for e in kept["entries"]], ["1", "2"])
        self.assertEqual(kept["votes"], {"42": ["1"]})


class _Message:
    def __init__(self, id, grouped_id=None, photo=True):
        self.id, self.grouped_id, self.photo, self.action = id, grouped_id, photo, None


class _Client:
    """The chat as Telethon shows it: message id -> message, None for a deleted one."""

    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.downloads = []

    async def get_messages(self, entity, ids=None):
        if isinstance(ids, list):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)

    async def download_media(self, message, file=None):
        self.downloads.append(message.id)
        Image.new("RGB", (300, 200), (200, 30, 30)).save(file)


class RestoreFromChatTests(_Disk):
    def test_an_album_comes_back_under_the_names_the_collect_gave_it(self):
        client = _Client([_Message(10, grouped_id=5), _Message(11, grouped_id=5, photo=False),
                          _Message(12, grouped_id=5), _Message(13, grouped_id=9)])
        names = asyncio.run(voting.download_post_photos(client, None, 10, self.root / "m", log=lambda *_: None))
        self.assertEqual(names, ["10_0.jpg", "10_2.jpg"])
        self.assertEqual(client.downloads, [10, 12])   # not the video, not the next post

    def test_a_deleted_post_has_nothing_to_give_back(self):
        names = asyncio.run(voting.download_post_photos(_Client([]), None, 10, self.root / "m", log=lambda *_: None))
        self.assertIsNone(names)

    def test_the_hall_gets_its_missing_photos_back_from_the_chat(self):
        contest = hall_of_fame.Contest(contest_id=WEEK, entry=CHAT, works=[
            hall_of_fame.Work(entry_id="10", place=1, votes=3, author_id=1, author_name="Аня", message_id=10),
            hall_of_fame.Work(entry_id="20", place=2, votes=1, author_id=2, author_name="Боря"),  # post deleted
        ])
        hall_of_fame.save_contest(contest)
        self.assertEqual(hall_of_fame.works_missing_photos(CHAT), [(WEEK, "10", 10), (WEEK, "20", 20)])
        client = _Client([_Message(10)])

        async def resolve(client_, entry):
            return "entity"

        with patch.object(bot_listener, "resolve_chat", resolve):
            restored, gone = asyncio.run(bot_listener._restore_hall_photos(client, CHAT, log=lambda *_: None))

        self.assertEqual((restored, gone), (1, 1))
        work = hall_of_fame.load_contest(CHAT, WEEK).works[0]
        self.assertEqual(work.photos, ["10_0.jpg"])
        with Image.open(hall_of_fame.media_file(CHAT, WEEK, work.thumb)) as cover:
            self.assertEqual(cover.size, (hall_of_fame.THUMB_SIDE, hall_of_fame.THUMB_SIDE))
        self.assertEqual(hall_of_fame.works_missing_photos(CHAT), [(WEEK, "20", 20)])

    def test_without_a_chat_session_nothing_is_attempted(self):
        self.assertEqual(asyncio.run(bot_listener._restore_hall_photos(None, CHAT)), (0, 0))


class StorageTests(unittest.TestCase):
    RAILWAY = {"RAILWAY_ENVIRONMENT": "production"}

    def _env(self, **values):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("RAILWAY_") and k != "DATA_DIR"}
        clean.update(values)
        return patch.dict(os.environ, clean, clear=True)

    def test_an_attached_volume_becomes_the_data_dir_when_none_was_set(self):
        with self._env(RAILWAY_VOLUME_MOUNT_PATH="/data"):
            self.assertEqual(storage.use_railway_volume(), "/data")
            self.assertEqual(os.environ["DATA_DIR"], "/data")

    def test_an_explicit_data_dir_always_wins(self):
        with self._env(RAILWAY_VOLUME_MOUNT_PATH="/data", DATA_DIR="/elsewhere"):
            self.assertIsNone(storage.use_railway_volume())
            self.assertEqual(os.environ["DATA_DIR"], "/elsewhere")

    def test_off_railway_there_is_nothing_to_warn_about(self):
        with self._env():
            self.assertIsNone(storage.persistence_warning())

    def test_on_railway_without_a_volume_the_administrators_are_told(self):
        with self._env(**self.RAILWAY):
            self.assertIn("не подключён Volume", storage.persistence_warning())

    def test_a_data_dir_on_the_volume_is_fine_and_one_beside_it_is_not(self):
        with self._env(RAILWAY_VOLUME_MOUNT_PATH="/data", DATA_DIR="/data/bot", **self.RAILWAY):
            self.assertIsNone(storage.persistence_warning())
        with self._env(RAILWAY_VOLUME_MOUNT_PATH="/data", DATA_DIR="/app", **self.RAILWAY):
            self.assertIn("не на Volume", storage.persistence_warning())

    def test_the_vote_panel_carries_the_warning(self):
        api = SimpleNamespace(sent=[])

        async def send_message(chat_id, text, **kwargs):
            api.sent.append(text)
            return {"message_id": 1}

        api.send_message = send_message

        async def yes(*args, **kwargs):
            return True

        async def resolve(*args, **kwargs):
            return -100

        message = {"message_id": 1, "chat": {"id": DM, "type": "private"}, "from": ADMIN, "text": "/vote"}
        with self._env(**self.RAILWAY), tempfile.TemporaryDirectory() as root, \
                patch("voting._voting_dir", return_value=Path(root)), \
                patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_can_manage_chat", yes):
            asyncio.run(bot_listener.handle_vote_command(
                api, None, SimpleNamespace(webapp_public_url="https://e.com", vote_miniapp_short_name=None),
                timezone.utc, message, CHAT, "bot", set(), log=lambda *_: None,
            ))
        self.assertTrue(api.sent[0].startswith("⚠️"))


class BackupTests(_Disk):
    def _seed(self):
        poll = voting.Poll(poll_id=WEEK, entry=CHAT, created_at="t", entries=[_entry("1")])
        voting.set_approved(poll, ["1"])
        voting.record_vote(poll, 9, ["1"])
        voting.save_poll(poll)
        media = voting.media_path(CHAT, WEEK)
        media.mkdir(parents=True)
        (media / "1_0.jpg").write_bytes(b"photo")
        voting.save_results(poll, poll.tally(), "итоги")
        (voting._voting_dir() / "half.json.tmp").write_text("{")
        hall_of_fame.save_contest(hall_of_fame.Contest(contest_id=WEEK, entry=CHAT))
        return poll

    def test_the_zip_holds_every_record_under_data_dirs_own_paths_and_no_photos(self):
        self._seed()
        path, count = backup.build_backup(bot_listener._history_sources(), bot_listener._backups_dir())
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            poll_name = f"voting/{voting.poll_path(CHAT, WEEK).name}"
            self.assertIn(poll_name, names)
            self.assertEqual(json.loads(archive.read(poll_name))["votes"], {"9": ["1"]})
        self.assertIn(f"voting/results/{voting.results_path(CHAT, WEEK).name}", names)
        self.assertIn(f"hall_of_fame/{hall_of_fame.contest_path(CHAT, WEEK).relative_to(self.root / 'hall_of_fame').as_posix()}", names)
        self.assertFalse(any(n.endswith((".jpg", ".tmp")) for n in names))
        self.assertEqual(count, len(names))
        self.assertEqual(path.parent, self.root / "backups")

    def test_only_the_newest_zips_are_kept_on_the_server(self):
        directory = self.root / "backups"
        directory.mkdir()
        for day in range(15):
            (directory / f"{backup.BACKUP_PREFIX}202601{day + 10:02d}-000000.zip").write_bytes(b"z")
        (directory / "mine.zip").write_bytes(b"not ours")
        backup.build_backup({}, directory)
        self.assertEqual(len(list(directory.glob(f"{backup.BACKUP_PREFIX}*.zip"))), backup.BACKUPS_KEPT)
        self.assertTrue((directory / "mine.zip").exists())

    def _backup_command(self, user, chat_type="private"):
        sent, documents = [], []

        async def send_message(chat_id, text, **kwargs):
            sent.append(text)
            return {"message_id": 1}

        async def send_document_file(chat_id, path, caption=None, **kwargs):
            documents.append((chat_id, Path(path).name, caption))
            return {}

        api = SimpleNamespace(send_message=send_message, send_document_file=send_document_file)

        async def resolve(*args, **kwargs):
            return -100

        async def real_admin(api_, chat_id, user_):
            return user_.get("id") == ADMIN["id"]

        message = {"message_id": 1, "chat": {"id": DM, "type": chat_type}, "from": user, "text": "/backup"}
        with patch.object(bot_listener, "_resolve_chat_id", resolve), \
                patch.object(bot_listener, "_is_chat_admin_or_privileged", real_admin), \
                patch.object(storage, "persistence_warning", lambda: None):
            asyncio.run(bot_listener.handle_backup_command(api, None, message, CHAT, log=lambda *_: None))
        return sent, documents

    def test_a_chat_administrator_gets_the_zip_in_the_dm(self):
        self._seed()
        sent, documents = self._backup_command(ADMIN)
        (chat_id, name, caption) = documents[0]
        self.assertEqual(chat_id, DM)
        self.assertTrue(name.startswith(backup.BACKUP_PREFIX))
        self.assertIn("Фото в неё не входят", caption)
        self.assertEqual(sent, [])

    def test_a_delegate_and_a_group_get_no_zip(self):
        self._seed()
        sent, documents = self._backup_command(DELEGATE)
        self.assertEqual(documents, [])
        self.assertIn("только администраторы", sent[0])
        sent, documents = self._backup_command(ADMIN, chat_type="supergroup")
        self.assertEqual(documents, [])
        self.assertIn("в личке", sent[0])


if __name__ == "__main__":
    unittest.main()
