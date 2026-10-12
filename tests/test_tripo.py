"""/tripo: photos in the DM, a 3D model back, through image generator web's Tripo endpoint.

The web app is stood in for by a local aiohttp server that answers /api/model3d the way
api/model3d.js does, so the bot's real HTTP client, polling and delivery all run. What is
pinned, in order of what costs the most when wrong:

- the request is the web node's Ultra-geometry one, with the chosen polygon budget and
  every other option off, and it is submitted exactly once per press;
- the model comes back into the DM, zipped or as a link when Telegram would refuse it;
- only the owner grants credits; everybody else spends one per generation, and gets it
  back exactly once when no model came of it -- whether that is known at once, after a
  timeout, or after a restart; a credit is never lost between the press and the outcome;
- /tripolog counts every generation and checks that each balance adds up;
- photos land in the right views, an album in order.
"""

import asyncio
import base64
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_listener
import tripo
from tests.async_case import AsyncTestCase

DM = 555
OWNER = {"id": 42, "username": "sultan_kembayev", "first_name": "Султан"}
STRANGER = {"id": 77, "username": "someone", "first_name": "Кто-то"}
TASK_ID = "0f4e2a3c-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
TZ = ZoneInfo("Europe/Moscow")


def _store_in(test_case) -> None:
    """Credits go to a directory of the test's own."""
    folder = tempfile.TemporaryDirectory()
    test_case.addCleanup(folder.cleanup)
    patcher = patch("stats._stats_dir", return_value=Path(folder.name))
    patcher.start()
    test_case.addCleanup(patcher.stop)


def _png(size=(1600, 1200), mode="RGB", color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, "PNG")
    return buffer.getvalue()


def _webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (90, 90, 90, 255)).save(buffer, "WEBP")
    return buffer.getvalue()


def _photo_message(message_id: int, file_id: str, sender=OWNER, caption=None) -> dict:
    message = {
        "message_id": message_id,
        "chat": {"id": DM, "type": "private"},
        "from": sender,
        "photo": [
            {"file_id": f"{file_id}-small", "width": 90, "height": 90},
            {"file_id": file_id, "width": 1280, "height": 960},
        ],
    }
    if caption:
        message["caption"] = caption
    return message


def _command(sender=OWNER, chat_type="private", text="/tripo") -> dict:
    return {
        "message_id": 1,
        "chat": {"id": DM, "type": chat_type},
        "from": sender,
        "text": text,
    }


class FakeApi:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []
        self.answers = []
        self.markups = []
        self.documents = []
        self.photos = []
        self.downloads = []
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                           parse_mode="Markdown", disable_notification=False):
        message_id = self._id()
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                          "message_id": message_id, "parse_mode": parse_mode})
        return {"message_id": message_id}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.edits.append({"message_id": message_id, "text": text, "reply_markup": reply_markup})

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append(message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    async def download_file(self, file_id):
        self.downloads.append(file_id)
        return _png()

    async def send_document_file(self, chat_id, path, caption=None, **kwargs):
        path = Path(path)
        self.documents.append({"name": path.name, "data": path.read_bytes(), "caption": caption})
        return {"message_id": self._id()}

    async def send_photo_file(self, chat_id, path, caption=None, **kwargs):
        self.photos.append({"name": Path(path).name, "caption": caption})
        return {"message_id": self._id()}

    # helpers

    def panels(self) -> list[dict]:
        return [m for m in self.sent if (m["reply_markup"] or {}).get("inline_keyboard")
                and m["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "tripo:go"]


class FakeWeb:
    """image generator web's /api/model3d, plus Tripo's file CDN."""

    def __init__(self):
        self.bodies = []
        self.submit_errors = []
        self.status_errors = []
        self.status_replies = []
        self.model = b"glTF" + bytes(4000)
        self.expire_once = set()
        self.file_hits = []
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.router.add_post("/api/model3d", self._model3d)
        app.router.add_get("/files/{name}", self._file)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()

    async def close(self):
        await self.server.close()

    @property
    def base_url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    def url(self, name: str) -> str:
        return str(self.server.make_url(f"/files/{name}"))

    def finished(self, model="model.glb", render="render.webp") -> dict:
        files = [{"Type": "GLB", "Url": self.url(model)}]
        if render:
            files.append({"Type": "IMAGE", "Url": self.url(render)})
        return {"done": True, "status": "success", "progress": 100, "files": files,
                "errorMessage": ""}

    def submits(self) -> list[dict]:
        return [b for b in self.bodies if b["action"] == "submit"]

    async def _model3d(self, request):
        body = await request.json()
        self.bodies.append(body)
        if body.get("provider") != "tripo":
            return web.json_response({"error": "wrong provider"}, status=400)
        if body["action"] == "submit":
            if self.submit_errors:
                status, payload = self.submit_errors.pop(0)
                return web.json_response(payload, status=status)
            return web.json_response({"taskId": TASK_ID, "diagnostics": []})
        if body["action"] == "status":
            if self.status_errors:
                status, payload = self.status_errors.pop(0)
                return web.json_response(payload, status=status)
            reply = self.status_replies.pop(0) if len(self.status_replies) > 1 else self.status_replies[0]
            return web.json_response({**reply, "diagnostics": []})
        return web.json_response({"error": "unknown action"}, status=400)

    async def _file(self, request):
        name = request.match_info["name"]
        self.file_hits.append(name)
        if name in self.expire_once:
            self.expire_once.discard(name)
            return web.Response(status=403, text="expired")
        if name.startswith("render"):
            return web.Response(body=_webp(), content_type="image/webp")
        return web.Response(body=self.model, content_type="model/gltf-binary")


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class SettingsTests(unittest.TestCase):
    def test_the_six_polygon_budgets(self):
        self.assertEqual(
            [tripo.format_faces(f) for f in tripo.FACE_LIMITS],
            ["100k", "200k", "500k", "1M", "1.5M", "2M"],
        )

    def test_the_panel_offers_every_budget_and_marks_the_chosen_one(self):
        flow = tripo.new_flow(1, DM, 0)
        flow["faces"] = 1_500_000
        rows = tripo.panel_keyboard(flow)["inline_keyboard"]
        budgets = [b for row in rows[1:3] for b in row]
        self.assertEqual(
            [b["callback_data"] for b in budgets],
            [f"tripo:faces:{f}" for f in tripo.FACE_LIMITS],
        )
        self.assertEqual([b["text"] for b in budgets if "•" in b["text"]], ["• 1.5M •"])

    def test_the_request_is_ultra_geometry_with_everything_else_off(self):
        payload = tripo.submit_payload({"front": "F", "back": "B"}, 2_000_000)
        self.assertEqual(payload["provider"], "tripo")
        self.assertEqual(payload["mode"], "image")
        self.assertEqual(payload["modelVersion"], tripo.MODEL_VERSION)
        self.assertEqual(payload["geometryQuality"], "detailed")
        self.assertEqual(payload["faceLimit"], 2_000_000)
        for option in ("texture", "pbr", "quad", "generateParts"):
            self.assertIs(payload[option], False, option)
        self.assertEqual(payload["mainImageBase64"], "F")
        self.assertEqual(payload["viewImages"], {"back": "B"})
        self.assertNotIn("tripoKey", payload)

    def test_callback_data_round_trips(self):
        self.assertEqual(tripo.parse_callback("tripo:faces:500000"), ("faces", "500000"))
        self.assertEqual(tripo.parse_callback("tripo:go"), ("go", None))
        self.assertEqual(tripo.parse_callback(f"tripo:task:{TASK_ID}"), ("task", TASK_ID))
        self.assertIsNone(tripo.parse_callback("viaclean:toggle"))

    def test_task_buttons_fit_telegram_and_an_overlong_id_drops_the_button(self):
        data = tripo.task_keyboard(TASK_ID)["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        self.assertIsNone(tripo.task_keyboard("x" * 80))

    def test_endpoint_accepts_a_base_url_or_the_endpoint_itself(self):
        for value in ("https://gen.example.app", "https://gen.example.app/", "https://gen.example.app/api/model3d"):
            with patch.dict(os.environ, {"TRIPO_WEB_URL": value}):
                self.assertEqual(tripo.web_endpoint(), "https://gen.example.app/api/model3d")
        with patch.dict(os.environ, {"TRIPO_WEB_URL": " "}):
            self.assertIsNone(tripo.web_endpoint())


class PhotoTests(unittest.TestCase):
    def test_an_album_fills_the_views_in_order_and_then_replaces_the_last(self):
        flow = tripo.new_flow(1, DM, 0)
        placed = [tripo.accept_photo(flow, f"p{i}") for i in range(5)]
        self.assertEqual(placed, ["front", "left", "back", "right", "right"])
        self.assertEqual(flow["views"]["right"], "p4")

    def test_a_chosen_view_takes_the_next_photo_then_the_first_empty_one_follows(self):
        flow = tripo.new_flow(1, DM, 0)
        flow["target"] = "back"
        self.assertEqual(tripo.accept_photo(flow, "b"), "back")
        self.assertEqual(flow["target"], "front")

    def test_pictures_are_recognised_as_photos_or_image_files(self):
        self.assertEqual(tripo.image_from_message(_photo_message(1, "big")), ("big", None))
        image_doc = {"document": {"file_id": "doc", "mime_type": "image/png", "file_size": 1000}}
        self.assertEqual(tripo.image_from_message(image_doc), ("doc", None))
        self.assertEqual(tripo.image_from_message({"document": {"mime_type": "application/pdf"}}), (None, None))
        self.assertEqual(tripo.image_from_message({"text": "hi"}), (None, None))
        huge = {"document": {"file_id": "x", "mime_type": "image/jpeg", "file_size": 30 * 1024 * 1024}}
        file_id, problem = tripo.image_from_message(huge)
        self.assertIsNone(file_id)
        self.assertIn("20 МБ", problem)

    def test_images_are_encoded_like_the_web_node_does(self):
        encoded = tripo.prepare_image(_png((1600, 1200)))
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (800, 600))

    def test_transparency_becomes_white_not_black(self):
        encoded = tripo.prepare_image(_png((100, 100), mode="RGBA", color=(0, 0, 0, 0)))
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertGreater(min(image.convert("L").getextrema()), 240)

    def test_a_phone_photo_is_turned_upright(self):
        source = Image.new("RGB", (400, 200), (10, 10, 10))
        exif = source.getexif()
        exif[0x0112] = 6  # rotate 90° clockwise to display
        buffer = io.BytesIO()
        source.save(buffer, "JPEG", exif=exif)
        encoded = tripo.prepare_image(buffer.getvalue())
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.size, (200, 400))


class CreditStoreTests(unittest.TestCase):
    def setUp(self):
        _store_in(self)

    def start(self, who=STRANGER, charged=True):
        return tripo.start_job(who, DM, charged, 1_000_000, {"front": "f"})

    def test_a_generation_needs_a_credit_and_one_with_no_model_gives_it_back_once(self):
        self.assertIsNone(self.start())
        tripo.change_credits(str(STRANGER["id"]), 1)
        job = self.start()
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertIsNone(self.start())
        self.assertEqual(tripo.settle_job(job, delivered=False)["credits"], 1)
        # Settled is settled: the check button after a restart must not refund it again.
        self.assertIsNone(tripo.settle_job(job, delivered=False))
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)

    def test_a_delivered_generation_keeps_its_credit_spent(self):
        tripo.change_credits(str(STRANGER["id"]), 1)
        job = self.start()
        self.assertEqual(tripo.settle_job(job, delivered=True)["refunded"], False)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertEqual(tripo._read_credits()["jobs"], {})

    def test_a_parked_generation_holds_its_credit_until_it_is_settled(self):
        tripo.change_credits(str(STRANGER["id"]), 1)
        job = self.start()
        tripo.set_job_task(job, TASK_ID)
        tripo.park_job(job, "Tripo не закончил за 20 минут")
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertEqual(tripo.job_for_task(TASK_ID)[1]["state"], "unresolved")
        tripo.settle_job(job, delivered=False)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)

    def test_a_pending_grant_is_claimed_once_by_username(self):
        tripo.change_credits("@Painter", 2)
        tripo.change_credits("painter", 1)
        self.assertEqual(tripo.remember_user({"id": 9, "username": "PAINTER"})["credits"], 3)
        self.assertEqual(tripo.remember_user({"id": 9, "username": "painter"})["credits"], 3)

    def test_every_change_is_logged(self):
        tripo.change_credits(str(STRANGER["id"]), 2, by="Султан")
        job = self.start()
        tripo.set_job_task(job, TASK_ID)
        tripo.settle_job(job, delivered=False)
        log = tripo._read_credits()["log"]
        self.assertEqual([(e["change"], e["why"]) for e in log], [(2, "grant"), (-1, "generation"), (1, "refund")])
        self.assertEqual(log[0]["by"], "Султан")
        self.assertEqual((log[1]["job"], log[2]["job"], log[2]["task"]), (job, job, TASK_ID))

    def test_taking_away_more_than_there_is_logs_and_counts_what_was_taken(self):
        tripo.change_credits("7", 2)
        result = tripo.change_credits("7", -5)
        self.assertEqual((result["applied"], result["credits"]), (-2, 0))
        store = tripo._read_credits()
        self.assertEqual((store["log"][-1]["change"], store["log"][-1]["why"]), (-2, "revoke"))
        self.assertEqual(store["users"]["7"]["revoked"], 2)
        self.assertIsNone(tripo._unbalanced(store["users"]["7"]))
        # Nothing to take: nothing logged.
        self.assertEqual(tripo.change_credits("7", -1)["applied"], 0)
        self.assertEqual(len(tripo._read_credits()["log"]), 2)

    def test_a_pending_grant_taken_away_logs_what_was_waiting(self):
        tripo.change_credits("@later", 1)
        self.assertEqual(tripo.change_credits("@later", -3)["applied"], -1)
        self.assertEqual(tripo._read_credits()["log"][-1]["change"], -1)

    def test_a_record_from_before_revocations_were_counted_reads_balanced(self):
        tripo._credits_path().parent.mkdir(parents=True, exist_ok=True)
        tripo._credits_path().write_text(json.dumps({"users": {
            "7": {"credits": 2, "used": 1, "granted": 5},
        }}), encoding="utf-8")
        record = tripo._read_credits()["users"]["7"]
        self.assertEqual(record["revoked"], 2)
        self.assertIsNone(tripo._unbalanced(record))

    def test_every_settled_generation_is_a_usage_line_the_owners_too(self):
        tripo.change_credits(str(STRANGER["id"]), 1)
        tripo.settle_job(self.start(), delivered=True)
        tripo.settle_job(self.start(OWNER, charged=False), delivered=False, error="bad image")
        rows = tripo.read_usage()
        self.assertEqual([(r["user"], r["outcome"], r["refunded"]) for r in rows],
                         [("77", "delivered", False), ("42", "failed", False)])
        self.assertEqual((rows[1]["error"], rows[1]["faces"], rows[1]["views"]), ("bad image", 1_000_000, ["front"]))
        self.assertFalse(tripo.is_known(OWNER["id"]))

    def test_a_damaged_store_reads_as_empty_rather_than_crashing(self):
        tripo._credits_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(tripo.balance(7), 0)

    def test_a_damaged_usage_line_is_skipped(self):
        tripo.settle_job(self.start(OWNER, charged=False), delivered=True)
        with tripo._usage_path().open("a", encoding="utf-8") as out:
            out.write('{"cut off')
        self.assertEqual(len(tripo.read_usage()), 1)

    def test_parsing_a_change(self):
        self.assertEqual(tripo.parse_credit_change("@a 5"), ("@a", 5))
        self.assertEqual(tripo.parse_credit_change("123 -2"), ("123", -2))
        for bad in ("", "@a", "@a x", "@a 0", "@a 5000", "@ 3"):
            with self.subTest(bad=bad):
                self.assertIsInstance(tripo.parse_credit_change(bad), str)

    def test_after_a_restart_a_job_that_never_reached_tripo_is_refunded(self):
        tripo.change_credits(str(STRANGER["id"]), 1)
        cut_off = self.start()
        sent = self.start(OWNER, charged=False)
        tripo.set_job_task(sent, TASK_ID)
        parked = self.start(OWNER, charged=False)
        tripo.set_job_task(parked, "other-task")
        tripo.park_job(parked, "timeout")
        writes = []
        original = tripo._write_credits
        with patch.object(tripo, "_write_credits", side_effect=lambda s: (writes.append(1), original(s))):
            recovered = tripo.recover_jobs()
        self.assertEqual(len(writes), 1)
        self.assertEqual([(s["user"], s["refunded"], s["credits"]) for s in recovered["settled"]],
                         [("77", True, 1)])
        self.assertEqual([(j["id"], j["task"]) for j in recovered["resume"]], [(sent, TASK_ID)])
        jobs = tripo._read_credits()["jobs"]
        self.assertNotIn(cut_off, jobs)
        self.assertEqual((jobs[sent]["state"], jobs[parked]["error"]), ("unresolved", "timeout"))
        self.assertEqual(tripo.read_usage()[-1]["outcome"], "failed")
        # Once: a second start finds nothing left to do.
        self.assertEqual(tripo.recover_jobs(), {"settled": [], "resume": []})

    def test_a_banned_or_expired_task_counts_as_finished(self):
        for status in ("banned", "expired", "cancelled", "failed"):
            self.assertTrue(tripo.task_finished({"done": False, "status": status}), status)
        self.assertFalse(tripo.task_finished({"done": False, "status": "running"}))
        self.assertTrue(tripo.task_finished({"done": True, "status": "success"}))


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        _store_in(self)
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def settled(self, who, delivered, minutes_ago, took=180, charged=True, faces=1_000_000, error=""):
        job = tripo.start_job(who, DM, charged, faces, {"front": "f", "back": "b"})
        store = tripo._read_credits()
        started = self.now - timedelta(minutes=minutes_ago)
        store["jobs"][job]["started"] = started.isoformat(timespec="seconds")
        tripo._write_credits(store)
        with patch.object(tripo, "_now_iso", return_value=(started + timedelta(seconds=took)).isoformat()):
            tripo.settle_job(job, delivered=delivered, error=error)

    def test_an_empty_report_says_so_and_still_checks_balances(self):
        text = tripo.usage_report(TZ, now=self.now)
        self.assertIn("Генераций пока не было.", text)
        self.assertIn("Сверка: балансы сходятся", text)

    def test_the_overview_counts_outcomes_periods_people_and_credits(self):
        tripo.remember_user(STRANGER)
        tripo.change_credits("@someone", 3)
        self.settled(STRANGER, True, minutes_ago=30, took=200)
        self.settled(STRANGER, False, minutes_ago=60 * 24 * 10, error="bad <image>")
        self.settled(OWNER, True, minutes_ago=10, took=100, charged=False, faces=2_000_000)
        with patch.object(tripo, "_now_iso", return_value=self.now.isoformat()):
            live = tripo.start_job(OWNER, DM, False, 500_000, {"front": "f"})
        tripo.set_job_task(live, TASK_ID)
        tripo.park_job(live, "Tripo не закончил за 20 минут")

        text = tripo.usage_report(TZ, now=self.now)
        self.assertIn("Генерации: <b>4</b> · сегодня 3 · 7 дней 3 · 30 дней 4", text)
        self.assertIn("Модель получена: 2 · не вышло: 1 (кредит возвращён 1) · ждут проверки: 1", text)
        self.assertIn("Среднее время до модели: 2:30", text)
        self.assertIn("Полигоны: 500k ×1 · 1M ×2 · 2M ×1", text)
        self.assertIn("Начислено 3 · списано владельцем 0 · потрачено 1 · в работе 0 · на балансах <b>2</b>", text)
        self.assertIn("Сверка: балансы сходятся", text)
        self.assertIn("@someone (Кто-то) — 2, моделей 1, баланс 2", text)
        self.assertIn("@sultan_kembayev (Султан) — 2, моделей 1, без кредитов", text)
        self.assertIn(f"ждёт проверки с 15:00: Tripo не закончил за 20 минут · задача <code>{TASK_ID[:8]}</code>", text)
        self.assertIn("не вышло: bad &lt;image&gt;, кредит возвращён", text)
        self.assertIn("02.10", text)  # older than today: dated

    def test_a_balance_that_does_not_add_up_is_flagged(self):
        tripo.change_credits("7", 2)
        store = tripo._read_credits()
        store["users"]["7"]["credits"] = 5
        tripo._write_credits(store)
        text = tripo.usage_report(TZ, now=self.now)
        self.assertIn("⚠️ Не сходится", text)
        self.assertIn("id 7 — баланс 5, должно быть 2", text)

    def test_one_person_with_their_credit_history(self):
        tripo.remember_user(STRANGER)
        tripo.change_credits("@someone", 2, by="Султан")
        self.settled(STRANGER, False, minutes_ago=5, error="bad image")
        self.settled(STRANGER, True, minutes_ago=2)
        text = tripo.usage_report(TZ, "@SomeOne", now=self.now)
        self.assertIn("<b>Tripo: @someone (Кто-то)</b>", text)
        self.assertIn("Баланс: <b>1</b> · начислено 2 · списано владельцем 0 · потрачено 1 · в работе 0", text)
        self.assertIn("Сверка: сходится.", text)
        self.assertIn("Генерации: 2 · моделей 1 · не вышло 1 (кредит возвращён 1)", text)
        self.assertIn("+2 начислено (Султан)", text)
        self.assertIn("+1 возврат", text)
        self.assertEqual(text.count("−1 генерация"), 2)
        self.assertEqual(tripo.usage_report(TZ, str(STRANGER["id"]), now=self.now), text)

    def test_the_owner_found_by_username_without_a_credit_record(self):
        self.settled(OWNER, True, minutes_ago=1, charged=False)
        text = tripo.usage_report(TZ, "@sultan_kembayev", now=self.now)
        self.assertIn("Генерирует без кредитов.", text)
        self.assertIn("Генерации: 1 · моделей 1", text)

    def test_somebody_unknown_is_said_so(self):
        self.assertIn("Не нашёл @nobody", tripo.usage_report(TZ, "@nobody", now=self.now))

    def test_a_long_history_still_fits_one_message(self):
        for i in range(40):
            # Telegram's longest: a 32-character username, 64-character names.
            who = {"id": 1000 + i, "username": f"u{i:02d}" + "x" * 29, "first_name": "Я" * 64,
                   "last_name": "Ж" * 64}
            tripo.settle_job(tripo.start_job(who, DM, False, 1_000_000, {"front": "f"}),
                             delivered=False, error="x" * 300)
        text = tripo.usage_report(TZ, now=self.now)
        self.assertLessEqual(len(text), 4096)
        self.assertTrue(text.endswith("…"))


# ---------------------------------------------------------------------------
# The bot, end to end against a stand-in web app
# ---------------------------------------------------------------------------


class TripoBotTests(AsyncTestCase):
    async def asyncSetUp(self):
        self.web = FakeWeb()
        await self.web.start()
        self.addAsyncCleanup(self.web.close)
        for name, value in (
            ("PANEL_REFRESH_DELAY_SECONDS", 0),
            ("POLL_INTERVAL_SECONDS", 0),
            ("SUBMIT_BACKOFF_SECONDS", (0, 0)),
        ):
            patcher = patch.object(tripo, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"TRIPO_WEB_URL": self.web.base_url, "TRIPO_API_KEY": "tsk_test"})
        env.start()
        self.addCleanup(env.stop)
        _store_in(self)
        self.api = FakeApi()
        self.flows = {}
        self.tasks = set()

    async def settle(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks))

    async def open_panel(self, message=None, who=OWNER):
        await bot_listener.handle_tripo_command(
            self.api, message or _command(who), self.flows, self.tasks,
        )
        await self.settle()
        return self.flows[who["id"]]

    async def send_photo(self, message_id, file_id, sender=OWNER):
        taken = await bot_listener.maybe_handle_tripo_photo(
            self.api, _photo_message(message_id, file_id, sender=sender), self.flows, self.tasks,
        )
        await self.settle()
        return taken

    async def press(self, data, presser=OWNER, message_id=None):
        flow = self.flows.get(presser["id"])
        callback = {
            "id": f"cb{len(self.api.answers)}",
            "from": presser,
            "data": data,
            "message": {
                "message_id": message_id if message_id is not None else (flow or {}).get("panel_id"),
                "chat": {"id": DM, "type": "private"},
            },
        }
        await bot_listener.handle_tripo_callback(self.api, callback, self.flows, self.tasks)
        await self.settle()

    # --- access

    async def test_somebody_without_credits_is_refused_and_nothing_opens(self):
        await bot_listener.handle_tripo_command(self.api, _command(STRANGER), self.flows, self.tasks)
        self.assertEqual(self.flows, {})
        self.assertEqual(self.api.sent[-1]["text"], bot_listener.TRIPO_NO_CREDITS)
        # Remembered, so the owner can now grant to their @username and reach them.
        self.assertTrue(tripo.is_known(STRANGER["id"]))

    async def test_somebody_with_a_credit_gets_the_panel_with_their_balance(self):
        tripo.remember_user(STRANGER)
        tripo.change_credits("@someone", 2)
        flow = await self.open_panel(who=STRANGER)
        self.assertFalse(flow["unlimited"])
        self.assertIn("Кредиты: <b>2</b>", self.api.panels()[-1]["text"])
        go = self.api.panels()[-1]["reply_markup"]["inline_keyboard"][-1][0]["text"]
        self.assertEqual(go, "Отправить на генерацию · 1 кредит")

    async def test_the_owner_needs_no_credits_and_sees_none(self):
        flow = await self.open_panel()
        self.assertTrue(flow["unlimited"])
        self.assertNotIn("Кредиты", self.api.panels()[-1]["text"])

    async def test_without_the_web_address_it_says_what_to_set(self):
        with patch.dict(os.environ, {"TRIPO_WEB_URL": ""}):
            await bot_listener.handle_tripo_command(self.api, _command(), self.flows, self.tasks)
        self.assertEqual(self.api.sent[-1]["text"], bot_listener.TRIPO_NOT_CONFIGURED)
        self.assertEqual(self.flows, {})

    async def test_a_stranger_cannot_press_somebody_elses_panel(self):
        await self.open_panel()
        await self.press("tripo:go", presser=STRANGER, message_id=self.flows[OWNER["id"]]["panel_id"])
        self.assertIn("устарела", self.api.answers[-1])
        self.assertEqual(self.web.bodies, [])

    async def test_a_stranger_cannot_make_the_bot_fetch_a_task(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.press(f"tripo:task:{TASK_ID}", presser=STRANGER, message_id=999)
        self.assertEqual(self.web.bodies, [])
        self.assertEqual(self.api.documents, [])

    # --- credits

    async def stranger_ready(self, credits: int):
        """A non-owner with `credits`, their panel open and a front photo in it."""
        tripo.remember_user(STRANGER)
        tripo.change_credits(str(STRANGER["id"]), credits)
        flow = await self.open_panel(who=STRANGER)
        await self.send_photo(10, "front-id", sender=STRANGER)
        return flow

    async def test_a_generation_costs_one_credit(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.stranger_ready(2)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertIn("Кредиты: <b>1</b>", self.api.panels()[-1]["text"])

    async def test_the_owner_is_never_charged(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.press("tripo:go")
        self.assertEqual(len(self.api.documents), 1)
        self.assertFalse(tripo.is_known(OWNER["id"]))

    async def test_without_a_credit_left_nothing_is_sent(self):
        flow = await self.stranger_ready(1)
        tripo.change_credits(str(STRANGER["id"]), -1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertIn("Кредиты закончились", self.api.answers[-1])
        self.assertEqual(self.web.bodies, [])
        self.assertFalse(flow["running"])

    async def test_a_generation_that_never_started_gives_the_credit_back(self):
        self.web.submit_errors = [(400, {"error": "Tripo input must be a JPEG or PNG image."})]
        await self.stranger_ready(1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertIn("Кредит возвращён, осталось 1.", [e for e in self.api.edits if "Не получилось" in e["text"]][-1]["text"])

    async def test_a_failed_tripo_task_gives_the_credit_back(self):
        self.web.status_replies = [{"done": True, "status": "failed", "progress": 0, "files": []}]
        await self.stranger_ready(1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)

    async def test_a_timed_out_task_keeps_the_credit_because_tripo_may_still_finish(self):
        self.web.status_replies = [{"done": False, "status": "running", "progress": 50, "files": []}]
        await self.stranger_ready(1)
        with patch.object(tripo, "POLL_LIMIT", 2):
            await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        last = [e for e in self.api.edits if "Не получилось" in e["text"]][-1]
        self.assertEqual(last["reply_markup"], tripo.task_keyboard(TASK_ID))
        self.assertIn("Кредит пока держится", last["text"])
        self.assertEqual(self.job()["state"], "unresolved")

    async def test_a_photo_telegram_will_not_hand_over_gives_the_credit_back(self):
        async def broken(file_id):
            raise bot_listener.ChatSummaryError("file is gone")

        await self.stranger_ready(1)
        self.api.download_file = broken
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertEqual(self.web.bodies, [])

    # --- credits: nothing lost between the press and the outcome

    def job(self) -> dict | None:
        jobs = tripo._read_credits()["jobs"]
        return next(iter(jobs.values()), None)

    async def time_out(self):
        """A stranger's generation that polling gave up on, its credit held."""
        self.web.status_replies = [{"done": False, "status": "running", "progress": 50, "files": []}]
        await self.stranger_ready(1)
        with patch.object(tripo, "POLL_LIMIT", 2):
            await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)

    async def check(self, presser=STRANGER):
        await self.press(f"tripo:task:{TASK_ID}", presser=presser, message_id=999)

    async def test_a_delivered_generation_is_settled_and_logged(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.stranger_ready(2)
        await self.press("tripo:go", presser=STRANGER)
        self.assertIsNone(self.job())
        row = tripo.read_usage()[-1]
        self.assertEqual((row["outcome"], row["task"], row["refunded"], row["user"], row["faces"]),
                         ("delivered", TASK_ID, False, "77", tripo.DEFAULT_FACE_LIMIT))

    async def test_a_press_whose_answer_times_out_still_runs_what_it_paid_for(self):
        self.web.status_replies = [self.web.finished(render=None)]
        flow = await self.stranger_ready(1)

        async def answer_times_out(callback_query_id, text=None):
            raise asyncio.TimeoutError

        self.api.answer_callback_query = answer_times_out
        with self.assertRaises(asyncio.TimeoutError):
            await bot_listener.handle_tripo_callback(self.api, {
                "id": "a", "from": STRANGER, "data": "tripo:go",
                "message": {"message_id": flow["panel_id"], "chat": {"id": DM}},
            }, self.flows, self.tasks)
        await self.settle()
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertIsNone(self.job())

    async def test_a_finished_task_without_a_model_gives_the_credit_back(self):
        self.web.status_replies = [{"done": True, "status": "success", "progress": 100, "files": []}]
        await self.stranger_ready(1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        failure = [e for e in self.api.edits if "Не получилось" in e["text"]][-1]
        self.assertIn("Кредит возвращён, осталось 1.", failure["text"])

    async def test_a_banned_task_is_refunded_without_waiting_out_the_poll(self):
        self.web.status_replies = [{"done": False, "status": "banned", "progress": 0, "files": []}]
        await self.stranger_ready(1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertEqual(len([b for b in self.web.bodies if b["action"] == "status"]), 1)

    async def test_a_timed_out_task_that_then_fails_gives_the_credit_back_once(self):
        await self.time_out()
        self.web.status_replies = [{"done": True, "status": "failed", "progress": 0, "files": [],
                                    "errorMessage": "mesh failed"}]
        await self.check()
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertEqual(self.api.sent[-1]["text"], "Задача не удалась: mesh failed. Кредит возвращён, осталось 1.")
        await self.check()
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertEqual(self.api.sent[-1]["text"], "Задача не удалась: mesh failed.")

    async def test_a_timed_out_task_that_finishes_is_delivered_by_the_check_and_stays_paid(self):
        await self.time_out()
        self.web.status_replies = [self.web.finished(render=None)]
        await self.check()
        self.assertEqual(len(self.api.documents), 1)
        # The job remembers the budget, so the file is named as the run would have.
        self.assertEqual(self.api.documents[0]["name"], f"tripo_1M_{TASK_ID[:8]}.glb")
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertIsNone(self.job())
        self.assertEqual(tripo.read_usage()[-1]["outcome"], "delivered")

    async def test_a_check_while_tripo_is_still_working_settles_nothing(self):
        await self.time_out()
        await self.check()
        self.assertEqual(self.api.sent[-1]["text"], "Tripo ещё генерирует: 50%.")
        self.assertEqual(self.job()["state"], "unresolved")

    async def test_a_refused_status_read_holds_the_credit_for_the_check(self):
        self.web.status_errors = [(404, {"error": "task not found"})]
        await self.stranger_ready(1)
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertEqual(self.job()["state"], "unresolved")
        failure = [e for e in self.api.edits if "Не получилось" in e["text"]][-1]
        self.assertEqual(failure["reply_markup"], tripo.task_keyboard(TASK_ID))

    async def test_a_model_telegram_refuses_holds_the_credit_until_the_check_delivers_it(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.stranger_ready(1)
        send = self.api.send_document_file

        async def refused(chat_id, path, caption=None, **kwargs):
            raise bot_listener.ChatSummaryError("Request Entity Too Large")

        self.api.send_document_file = refused
        await self.press("tripo:go", presser=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertEqual(self.job()["state"], "unresolved")
        self.api.send_document_file = send
        await self.check()
        self.assertEqual(len(self.api.documents), 1)
        self.assertIsNone(self.job())
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)

    # --- after a restart, before anybody presses anything

    def interrupted(self, charged=True, task_id=TASK_ID) -> str:
        """A generation the previous process was in the middle of."""
        if charged:
            tripo.remember_user(STRANGER)
            tripo.change_credits(str(STRANGER["id"]), 1)
        job = tripo.start_job(STRANGER if charged else OWNER, DM, charged, 1_500_000,
                              {"front": "f", "left": "l"})
        if task_id:
            tripo.set_job_task(job, task_id)
        return job

    async def recover(self):
        await bot_listener._tripo_recover(self.api, self.tasks)
        await self.settle()

    async def test_a_restart_before_tripo_returns_the_credit_and_says_so(self):
        self.interrupted(task_id=None)
        await self.recover()
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertIn("Кредит возвращён, осталось 1.", self.api.sent[-1]["text"])
        self.assertEqual(self.web.bodies, [])

    async def test_a_restart_mid_generation_is_picked_up_and_delivered(self):
        self.interrupted()
        self.web.status_replies = [
            {"done": False, "status": "running", "progress": 70, "files": []},
            self.web.finished(render=None),
        ]
        await self.recover()
        self.assertEqual(self.web.submits(), [])
        self.assertIn("модель придёт сюда", self.api.sent[0]["text"])
        self.assertEqual(self.api.documents[0]["name"], f"tripo_1.5M_{TASK_ID[:8]}.glb")
        self.assertIn("виды: спереди, слева", self.api.documents[0]["caption"])
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertIsNone(self.job())

    async def test_a_restart_mid_generation_that_fails_returns_the_credit(self):
        self.interrupted()
        self.web.status_replies = [{"done": True, "status": "failed", "progress": 0, "files": []}]
        await self.recover()
        self.assertEqual(tripo.balance(STRANGER["id"]), 1)
        self.assertIn("Кредит возвращён, осталось 1.", self.api.sent[-1]["text"])

    async def test_a_check_press_while_a_restart_resumes_the_task_does_not_fetch_it_twice(self):
        self.interrupted(charged=False)
        self.web.status_replies = [self.web.finished(render=None)]
        release = asyncio.Event()
        original = tripo.TripoWebClient.status

        async def held(client, task_id):
            await release.wait()
            return await original(client, task_id)

        with patch.object(tripo.TripoWebClient, "status", held):
            await bot_listener._tripo_recover(self.api, self.tasks)
            # Not self.check: that waits for the background work, which is what is held.
            await bot_listener.handle_tripo_callback(self.api, {
                "id": "c", "from": OWNER, "data": f"tripo:task:{TASK_ID}",
                "message": {"message_id": 999, "chat": {"id": DM}},
            }, self.flows, self.tasks)
            self.assertEqual(self.api.answers[-1], "Уже проверяю.")
            release.set()
            await self.settle()
        self.assertEqual(len(self.api.documents), 1)

    def test_the_listener_recovers_at_startup(self):
        source = inspect.getsource(bot_listener.run_bot_listener)
        self.assertIn("_tripo_spawn(_tripo_recover(api, background_tasks, log=log), background_tasks)", source)

    # --- /tripocredits

    async def credits_command(self, text, sender=OWNER):
        await bot_listener.handle_tripo_credits_command(self.api, _command(sender, text=text), text)

    async def test_only_the_owner_grants(self):
        await self.credits_command("/tripocredits @someone 5", sender=STRANGER)
        self.assertEqual(self.api.sent[-1]["text"], bot_listener.TRIPO_OWNER_ONLY)
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)

    async def test_a_grant_to_somebody_unknown_waits_for_their_first_tripo(self):
        await self.credits_command("/tripocredits @Someone 3")
        self.assertIn("дождутся", self.api.sent[-1]["text"])
        flow = await self.open_panel(who=STRANGER)
        self.assertEqual(tripo.balance(STRANGER["id"]), 3)
        self.assertFalse(flow["unlimited"])

    async def test_a_grant_to_somebody_known_tells_them(self):
        tripo.remember_user(STRANGER)
        await self.credits_command("/tripocredits @someone 4")
        note = [m for m in self.api.sent if m["chat_id"] == STRANGER["id"]][-1]
        self.assertIn("Баланс: 4", note["text"])
        self.assertIn("Уведомил", self.api.sent[-1]["text"])

    async def test_taking_away_never_goes_below_zero_and_says_nothing_to_them(self):
        tripo.remember_user(STRANGER)
        tripo.change_credits("@someone", 2)
        await self.credits_command(f"/tripocredits {STRANGER['id']} -5")
        self.assertEqual(tripo.balance(STRANGER["id"]), 0)
        self.assertFalse([m for m in self.api.sent if m["chat_id"] == STRANGER["id"]])
        self.assertTrue(self.api.sent[-1]["text"].startswith("Списано 2."))
        await self.credits_command(f"/tripocredits {STRANGER['id']} -1")
        self.assertEqual(self.api.sent[-1]["text"], "Списывать нечего: у @someone 0.")

    async def test_the_list_shows_balances_and_what_is_waiting(self):
        tripo.remember_user(STRANGER)
        tripo.change_credits("@someone", 2)
        tripo.change_credits("@later", 1)
        await self.credits_command("/tripocredits")
        text = self.api.sent[-1]["text"]
        self.assertIn("@someone (Кто-то) — <b>2</b>", text)
        self.assertIn("@later — <b>1</b>", text)

    async def test_a_malformed_grant_explains_itself(self):
        for text in ("/tripocredits @someone", "/tripocredits @someone lots", "/tripocredits @someone 0"):
            with self.subTest(text=text):
                await self.credits_command(text)
                self.assertTrue(self.api.sent[-1]["text"])
        self.assertFalse(tripo.is_known(STRANGER["id"]))

    # --- /tripolog

    async def log_command(self, text="/tripolog", sender=OWNER):
        await bot_listener.handle_tripo_log_command(self.api, _command(sender, text=text), text, TZ)

    async def test_only_the_owner_reads_the_log(self):
        await self.log_command(sender=STRANGER)
        self.assertEqual(self.api.sent[-1]["text"], bot_listener.TRIPO_LOG_OWNER_ONLY)

    async def test_the_log_counts_a_generation_just_made(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.stranger_ready(2)
        await self.press("tripo:go", presser=STRANGER)
        await self.log_command()
        report = self.api.sent[-1]
        self.assertEqual(report["parse_mode"], "HTML")
        self.assertIn("Генерации: <b>1</b> · сегодня 1", report["text"])
        self.assertIn("Модель получена: 1 · не вышло: 0", report["text"])
        self.assertIn("Сверка: балансы сходятся", report["text"])
        await self.log_command("/tripolog @someone")
        self.assertIn("Баланс: <b>1</b> · начислено 2", self.api.sent[-1]["text"])

    # --- the panel

    async def test_photos_fill_views_and_the_panel_moves_below_them(self):
        flow = await self.open_panel()
        first_panel = flow["panel_id"]
        self.assertTrue(await self.send_photo(10, "front-id"))
        self.assertTrue(await self.send_photo(11, "left-id"))
        self.assertEqual(flow["views"], {"front": "front-id", "left": "left-id"})
        self.assertIn(first_panel, self.api.deleted)
        self.assertEqual(self.api.panels()[-1]["message_id"], flow["panel_id"])
        self.assertIn("спереди ✓ · слева ✓ · сзади —", self.api.panels()[-1]["text"])

    async def test_an_album_posts_one_panel_not_one_per_photo(self):
        await self.open_panel()
        with patch.object(tripo, "PANEL_REFRESH_DELAY_SECONDS", 0.05):
            for i, file_id in enumerate(("a", "b", "c", "d")):
                await bot_listener.maybe_handle_tripo_photo(
                    self.api, _photo_message(10 + i, file_id), self.flows, self.tasks,
                )
            await self.settle()
        self.assertEqual(len(self.api.panels()), 2)  # the opening one and one after the album
        self.assertEqual(set(self.flows[OWNER["id"]]["views"]), set(tripo.VIEWS))

    async def test_a_photo_with_no_panel_open_is_left_for_others(self):
        self.assertFalse(await self.send_photo(10, "x"))

    async def test_a_photo_captioned_with_the_command_is_the_front_view(self):
        flow = await self.open_panel(_photo_message(1, "captioned", caption="/tripo"))
        self.assertEqual(flow["views"], {"front": "captioned"})
        self.assertEqual(flow["target"], "left")

    async def test_buttons_choose_the_view_and_the_budget(self):
        flow = await self.open_panel()
        await self.press("tripo:view:back")
        await self.press("tripo:faces:200000")
        self.assertEqual((flow["target"], flow["faces"]), ("back", 200_000))
        await self.press("tripo:faces:123")  # not on the panel: ignored
        self.assertEqual(flow["faces"], 200_000)
        self.assertIn("<b>200k</b>", self.api.edits[-1]["text"])

    async def test_an_old_panel_is_refused(self):
        await self.open_panel()
        await self.press("tripo:faces:200000", message_id=1)
        self.assertIn("устарела", self.api.answers[-1])
        self.assertEqual(self.flows[OWNER["id"]]["faces"], tripo.DEFAULT_FACE_LIMIT)

    async def test_generation_needs_a_front_view(self):
        await self.open_panel()
        await self.press("tripo:go")
        self.assertEqual(self.api.answers[-1], "Сначала пришли фото спереди.")
        self.assertEqual(self.web.bodies, [])

    async def test_close_forgets_the_panel(self):
        await self.open_panel()
        await self.press("tripo:cancel")
        self.assertEqual(self.flows, {})
        self.assertEqual(self.api.edits[-1]["text"], "Tripo закрыт.")

    # --- generating

    async def test_one_press_generates_once_and_sends_the_model_back(self):
        self.web.status_replies = [
            {"done": False, "status": "running", "progress": 40, "files": []},
            self.web.finished(),
        ]
        flow = await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.send_photo(11, "left-id")
        await self.press("tripo:view:right")
        await self.send_photo(12, "right-id")
        await self.press("tripo:faces:1500000")
        await self.press("tripo:go")

        submits = self.web.submits()
        self.assertEqual(len(submits), 1)
        body = submits[0]
        self.assertEqual(body["faceLimit"], 1_500_000)
        self.assertEqual(body["geometryQuality"], "detailed")
        self.assertEqual(body["tripoKey"], "tsk_test")
        self.assertEqual(set(body["viewImages"]), {"left", "right"})
        for option in ("texture", "pbr", "quad", "generateParts"):
            self.assertIs(body[option], False)
        self.assertEqual(sorted(self.api.downloads), ["front-id", "left-id", "right-id"])
        # Every status read carries the key too, and polling stopped once Tripo was done.
        statuses = [b for b in self.web.bodies if b["action"] == "status"]
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(b["tripoKey"] == "tsk_test" and b["taskId"] == TASK_ID for b in statuses))

        self.assertEqual(len(self.api.documents), 1)
        document = self.api.documents[0]
        self.assertEqual(document["data"], self.web.model)
        self.assertEqual(document["name"], f"tripo_1.5M_{TASK_ID[:8]}.glb")
        self.assertIn("1.5M", document["caption"])
        self.assertEqual(len(self.api.photos), 1)  # Tripo's render, above the model

        self.assertFalse(flow["running"])
        self.assertIn("\nГотово.\n", self.api.edits[-1]["text"])
        self.assertEqual(self.api.edits[-1]["reply_markup"], {"inline_keyboard": []})
        # A fresh panel follows, photos kept, ready for another budget.
        self.assertEqual(self.api.panels()[-1]["message_id"], flow["panel_id"])
        self.assertEqual(len(flow["views"]), 3)

    async def test_a_double_tap_does_not_pay_twice(self):
        release = asyncio.Event()
        self.web.status_replies = [self.web.finished()]
        original = tripo.TripoWebClient.status

        async def held_status(client, task_id):
            await release.wait()
            return await original(client, task_id)

        flow = await self.open_panel()
        await self.send_photo(10, "front-id")
        panel = flow["panel_id"]
        with patch.object(tripo.TripoWebClient, "status", held_status):
            await bot_listener.handle_tripo_callback(self.api, {
                "id": "a", "from": OWNER, "data": "tripo:go",
                "message": {"message_id": panel, "chat": {"id": DM}},
            }, self.flows, self.tasks)
            flow["panel_id"] = panel  # a second panel pressed while the first run is going
            await bot_listener.handle_tripo_callback(self.api, {
                "id": "b", "from": OWNER, "data": "tripo:go",
                "message": {"message_id": panel, "chat": {"id": DM}},
            }, self.flows, self.tasks)
            self.assertEqual(self.api.answers[-1], "Генерация уже идёт.")
            for _ in range(50):
                if self.web.submits():
                    break
                await asyncio.sleep(0.01)
            release.set()
            await self.settle()
        self.assertEqual(len(self.web.submits()), 1)
        self.assertEqual(len(self.api.documents), 1)

    async def test_a_busy_tripo_is_waited_out(self):
        self.web.submit_errors = [(429, {"error": "concurrency full"})]
        self.web.status_replies = [self.web.finished(render=None)]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.press("tripo:go")
        self.assertEqual(len(self.web.submits()), 2)
        self.assertEqual(len(self.api.documents), 1)

    async def test_a_tripo_failure_is_reported_with_the_check_button(self):
        self.web.status_replies = [{"done": True, "status": "failed", "progress": 0, "files": [],
                                    "errorMessage": "bad <image>"}]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.press("tripo:go")
        failure = [e for e in self.api.edits if "Не получилось" in e["text"]][-1]
        self.assertIn("bad &lt;image&gt;", failure["text"])
        self.assertEqual(failure["reply_markup"], tripo.task_keyboard(TASK_ID))
        self.assertEqual(self.api.documents, [])

    async def test_an_upload_refusal_from_the_web_app_is_shown_not_retried(self):
        self.web.submit_errors = [(400, {"error": "Tripo input must be a JPEG or PNG image."})]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.press("tripo:go")
        self.assertEqual(len(self.web.submits()), 1)
        self.assertIn("JPEG or PNG", [e for e in self.api.edits if "Не получилось" in e["text"]][-1]["text"])

    async def test_an_expired_link_is_signed_afresh_once(self):
        self.web.status_replies = [self.web.finished(render=None)]
        self.web.expire_once = {"model.glb"}
        await self.open_panel()
        await self.send_photo(10, "front-id")
        await self.press("tripo:go")
        self.assertEqual(self.web.file_hits.count("model.glb"), 2)
        self.assertEqual(len(self.api.documents), 1)

    async def test_a_model_over_telegrams_limit_is_zipped(self):
        self.web.model = b"glTF" + bytes(200_000)  # zeros: deflates to almost nothing
        self.web.status_replies = [self.web.finished(render=None)]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        with patch.object(tripo, "TELEGRAM_UPLOAD_LIMIT", 100_000):
            await self.press("tripo:go")
        document = self.api.documents[0]
        self.assertTrue(document["name"].endswith(".glb.zip"))
        with zipfile.ZipFile(io.BytesIO(document["data"])) as archive:
            self.assertEqual(archive.read(archive.namelist()[0]), self.web.model)

    async def test_a_model_too_big_even_zipped_goes_out_as_a_link(self):
        self.web.model = os.urandom(50_000)  # random bytes do not deflate
        self.web.status_replies = [self.web.finished(render=None)]
        await self.open_panel()
        await self.send_photo(10, "front-id")
        with patch.object(tripo, "TELEGRAM_UPLOAD_LIMIT", 10_000):
            await self.press("tripo:go")
        self.assertEqual(self.api.documents, [])
        link = [m for m in self.api.sent if "Скачать GLB" in m["text"]][-1]
        self.assertIn(self.web.url("model.glb"), link["text"])
        self.assertEqual(link["reply_markup"], tripo.link_keyboard(TASK_ID))

    # --- after a restart

    async def test_the_check_button_recovers_a_model_after_a_restart(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.press(f"tripo:task:{TASK_ID}", message_id=999)  # no flows: a fresh process
        self.assertEqual(self.api.answers[-1], "Проверяю…")
        self.assertEqual(self.web.submits(), [])
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(self.api.documents[0]["name"], f"tripo_{TASK_ID[:8]}.glb")

    async def test_the_check_button_only_reports_progress_while_this_process_is_polling(self):
        self.flows[OWNER["id"]] = {**tripo.new_flow(OWNER["id"], DM, 0),
                                   "running": True, "task_id": TASK_ID, "progress": 63}
        await self.press(f"tripo:task:{TASK_ID}", message_id=999)
        self.assertEqual(self.api.answers[-1], "Ещё генерируется: 63%")
        self.assertEqual(self.web.bodies, [])

    async def test_new_link_sends_a_link_without_downloading(self):
        self.web.status_replies = [self.web.finished(render=None)]
        await self.press(f"tripo:link:{TASK_ID}", message_id=999)
        self.assertEqual(self.web.file_hits, [])
        self.assertIn(self.web.url("model.glb"), self.api.sent[-1]["text"])


class DispatchTests(AsyncTestCase):
    """The router sends /tripo, its photos and its buttons to the handlers above."""

    async def test_the_command_and_its_buttons_are_routed(self):
        api = FakeApi()
        flows = {}
        with patch.dict(os.environ, {"TRIPO_WEB_URL": "https://gen.example.app"}), \
                patch.object(bot_listener, "handle_tripo_command") as command, \
                patch.object(bot_listener, "handle_tripo_callback") as button:
            command.return_value = None
            button.return_value = None
            cfg = type("Cfg", (), {"listener_allowed_chats": [], "stats_enabled": False})()
            await bot_listener._dispatch_update(
                {"message": _command()}, api, None, cfg, None, "bot", 1, set(),
                asyncio.Queue(), set(), None, {}, {}, {}, {}, tripo_flows=flows,
            )
            await bot_listener._dispatch_update(
                {"callback_query": {"id": "x", "data": "tripo:go", "from": OWNER,
                                    "message": {"message_id": 5, "chat": {"id": DM}}}},
                api, None, cfg, None, "bot", 1, set(),
                asyncio.Queue(), set(), None, {}, {}, {}, {}, tripo_flows=flows,
            )
        command.assert_called_once()
        button.assert_called_once()
        self.assertIs(command.call_args.args[2], flows)

    async def test_the_credits_command_is_routed_and_not_taken_for_tripo(self):
        api = FakeApi()
        with patch.object(bot_listener, "handle_tripo_credits_command") as credits, \
                patch.object(bot_listener, "handle_tripo_command") as command:
            cfg = type("Cfg", (), {"listener_allowed_chats": [], "stats_enabled": False})()
            await bot_listener._dispatch_update(
                {"message": _command(text="/tripocredits @someone 2")}, api, None, cfg, None, "bot", 1,
                set(), asyncio.Queue(), set(), None, {}, {}, {}, {}, tripo_flows={},
            )
        credits.assert_called_once()
        command.assert_not_called()

    async def test_the_log_command_is_routed_and_not_taken_for_tripo(self):
        api = FakeApi()
        with patch.object(bot_listener, "handle_tripo_log_command") as report, \
                patch.object(bot_listener, "handle_tripo_command") as command:
            cfg = type("Cfg", (), {"listener_allowed_chats": [], "stats_enabled": False})()
            for chat_type in ("private", "supergroup"):
                await bot_listener._dispatch_update(
                    {"message": _command(text="/tripolog @someone", chat_type=chat_type)}, api, None, cfg,
                    TZ, "bot", 1, set(), asyncio.Queue(), set(), None, {}, {}, {}, {}, tripo_flows={},
                )
        report.assert_called_once()
        self.assertEqual(report.call_args.args[2], "/tripolog @someone")
        self.assertIs(report.call_args.args[3], TZ)
        command.assert_not_called()

    async def test_the_command_is_ignored_in_a_group(self):
        api = FakeApi()
        with patch.object(bot_listener, "handle_tripo_command") as command:
            cfg = type("Cfg", (), {"listener_allowed_chats": [], "stats_enabled": False})()
            await bot_listener._dispatch_update(
                {"message": _command(chat_type="supergroup")}, api, None, cfg, None, "bot", 1,
                set(), asyncio.Queue(), set(), None, {}, {}, {}, {}, tripo_flows={},
            )
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
