"""/tripo -- turn one photo, or several views of one object, into a 3D model.

The bot is only the remote control. Generation goes through image generator web's
/api/model3d (provider "tripo"), the endpoint its Tripo node calls, and the request is
the one that node sends with these settings: model v3.1, Ultra geometry, and everything
else off -- no texture, PBR, quad mesh, parts or rig. The only choice left to make in
Telegram is the polygon budget.

Configured from the environment, read when used so a missing value costs this command
and nothing else:

    TRIPO_WEB_URL   where image generator web is deployed (https://….vercel.app)
    TRIPO_API_KEY   Tripo secret (tsk_…). Optional when that deployment has its own
                    TRIPO_API_KEY.

Every generation spends real Tripo credits, so everybody but the owner pays for one with a
bot credit (see "credits" below), and only the owner grants those.
"""

import asyncio
import base64
import html
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import stats

COMMAND = "/tripo"
CALLBACK_PREFIX = "tripo"

MODEL_VERSION = "v3.1-20260211"
FACE_LIMITS = (100_000, 200_000, 500_000, 1_000_000, 1_500_000, 2_000_000)
DEFAULT_FACE_LIMIT = 1_000_000

# Tripo's multiview order. Front is required; the others are optional.
VIEWS = ("front", "left", "back", "right")
VIEW_LABELS = {"front": "Спереди", "left": "Слева", "back": "Сзади", "right": "Справа"}

# The web app's compressImageForApi: longest side 800px, JPEG at 0.72, transparency on white.
IMAGE_MAX_PX = 800
IMAGE_JPEG_QUALITY = 72
# Bot API getFile refuses anything larger, so such a file is turned away when it arrives.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
# A model is streamed to disk; this only stops a runaway response from filling it.
MODEL_DOWNLOAD_LIMIT = 500 * 1024 * 1024

# An open panel keeps taking photos for this long after it was last touched.
FLOW_TTL_SECONDS = 2 * 60 * 60
# Photos in an album arrive as separate messages; the panel is moved below them once,
# after the last one, instead of once per photo.
PANEL_REFRESH_DELAY_SECONDS = 1.5

# The web node polls every 4s. Ultra geometry at 2M faces can take several minutes.
POLL_INTERVAL_SECONDS = 4
POLL_LIMIT = 300
# Tripo's advice for 429 (rate limit or account concurrency full): back off from 1s.
SUBMIT_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32, 32, 32)

IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class TripoWebError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TripoTaskFailed(TripoWebError):
    """Tripo ran the task and reported it failed: no model came of it."""


# --------------------------------------------------------------------------- settings


def web_endpoint() -> str | None:
    """/api/model3d on the configured deployment. A full endpoint URL is accepted too."""
    base = (os.getenv("TRIPO_WEB_URL") or "").strip().rstrip("/")
    if not base:
        return None
    return base if base.endswith("/api/model3d") else f"{base}/api/model3d"


def api_key() -> str:
    return (os.getenv("TRIPO_API_KEY") or "").strip()


def format_faces(faces: int) -> str:
    """100k, 500k, 1M, 1.5M."""
    if faces >= 1_000_000:
        millions = faces / 1_000_000
        return f"{millions:g}M"
    return f"{faces // 1000}k"


# --------------------------------------------------------------------------- the panel


def new_flow(user_id: int, chat_id: int, now: float, unlimited: bool = False) -> dict:
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        # The owner generates without credits; everybody else pays one per generation.
        "unlimited": unlimited,
        "panel_id": None,
        "views": {},
        "target": "front",
        "faces": DEFAULT_FACE_LIMIT,
        "touched": now,
        "running": False,
        "task_id": None,
        "progress": 0,
        "photo_seq": 0,
    }


def is_expired(flow: dict, now: float) -> bool:
    return not flow["running"] and now - flow["touched"] > FLOW_TTL_SECONDS


def view_names(views) -> str:
    return ", ".join(VIEW_LABELS[v].lower() for v in VIEWS if v in views)


def accept_photo(flow: dict, file_id: str) -> str:
    """Put a photo into the selected view and select the next empty one.

    That is what lets an album of four photos fill front, left, back and right in order.
    With every view filled the selection stays put, so the next photo replaces this one.
    Returns the view the photo went into.
    """
    view = flow["target"]
    flow["views"][view] = file_id
    empty = [v for v in VIEWS if v not in flow["views"]]
    if empty:
        flow["target"] = empty[0]
    return view


def callback_data(action: str, argument: str | int | None = None) -> str:
    parts = [CALLBACK_PREFIX, action]
    if argument is not None:
        parts.append(str(argument))
    return ":".join(parts)


def parse_callback(data: str) -> tuple[str, str | None] | None:
    parts = (data or "").split(":", 2)
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX or not parts[1]:
        return None
    return parts[1], (parts[2] if len(parts) > 2 else None)


def panel_text(flow: dict, credits: int | None = None) -> str:
    """`credits` is the person's balance, or None for the owner, who needs none."""
    views = flow["views"]
    view_line = " · ".join(
        f"{VIEW_LABELS[v].lower()} {'✓' if v in views else '—'}" for v in VIEWS
    )
    lines = [
        "<b>Tripo 3D</b>",
        "",
        f"Виды: {view_line}",
        f"Полигоны: <b>{format_faces(flow['faces'])}</b>",
        "Геометрия Ultra. Текстура, PBR, quad, части и риг выключены.",
    ]
    if credits is not None:
        lines.append(f"Кредиты: <b>{credits}</b> · одна генерация — один кредит.")
    lines.append("")
    if "front" not in views:
        lines.append(
            "Пришли фото: первое станет видом спереди. Альбом из нескольких фото "
            "разложится по порядку: спереди, слева, сзади, справа."
        )
    else:
        target = VIEW_LABELS[flow["target"]].lower()
        verb = "заменит вид" if flow["target"] in views else "станет видом"
        lines.append(f"Следующее фото {verb} «{target}». Кнопкой вида можно выбрать другой.")
    if flow["running"]:
        lines += ["", "Предыдущая генерация ещё идёт."]
    return "\n".join(lines)


def panel_keyboard(flow: dict, credits: int | None = None) -> dict:
    views = flow["views"]
    go = "Отправить на генерацию" if credits is None else "Отправить на генерацию · 1 кредит"

    def view_button(view: str) -> dict:
        label = VIEW_LABELS[view]
        if view in views:
            label += " ✓"
        if view == flow["target"]:
            label = f"» {label}"
        return {"text": label, "callback_data": callback_data("view", view)}

    def faces_button(faces: int) -> dict:
        label = format_faces(faces)
        if faces == flow["faces"]:
            label = f"• {label} •"
        return {"text": label, "callback_data": callback_data("faces", faces)}

    return {"inline_keyboard": [
        [view_button(v) for v in VIEWS],
        [faces_button(f) for f in FACE_LIMITS[:3]],
        [faces_button(f) for f in FACE_LIMITS[3:]],
        [
            {"text": "Убрать фото", "callback_data": callback_data("clear")},
            {"text": "Закрыть", "callback_data": callback_data("cancel")},
        ],
        [{"text": go, "callback_data": callback_data("go")}],
    ]}


def progress_text(views, faces: int, task_id: str | None, line: str) -> str:
    lines = [
        "<b>Tripo 3D</b>",
        "",
        f"Виды: {view_names(views)} · полигоны {format_faces(faces)}",
        line,
    ]
    if task_id:
        lines.append(f"Задача: <code>{html.escape(task_id)}</code>")
    return "\n".join(lines)


def _task_button(text: str, action: str, task_id: str) -> dict | None:
    data = callback_data(action, task_id)
    # Telegram refuses a whole keyboard over one callback longer than 64 bytes. Tripo's
    # ids are UUIDs and fit; anything else goes without the button rather than the message.
    if len(data.encode("utf-8")) > 64:
        return None
    return {"inline_keyboard": [[{"text": text, "callback_data": data}]]}


def task_keyboard(task_id: str) -> dict | None:
    """The one button a running generation keeps: after a restart it is the only way back
    to a model Tripo has already been paid for."""
    return _task_button("Проверить результат", "task", task_id)


def link_keyboard(task_id: str) -> dict | None:
    return _task_button("Новая ссылка", "link", task_id)


# --------------------------------------------------------------------------- credits
#
# One credit buys one generation. Only the owner grants them (/tripocredits) and the owner
# needs none. Kept per person rather than per chat: /tripo lives in a DM.
#
# A grant can name somebody by @username before the bot has ever seen them; it waits under
# that name in "pending" until they open /tripo, which is when the bot learns their id.
#
# Same concurrency story as via_cleaner's store: every read-modify-write is one synchronous
# function with no await inside, on a single event loop, so two writers never interleave.

CREDITS_STORE_VERSION = 1
CREDIT_LOG_LIMIT = 500
CREDIT_CHANGE_LIMIT = 1000
CREDITS_COMMAND = "/tripocredits"


def _credits_path() -> Path:
    return stats._stats_dir() / "tripo_credits.json"


def _blank_credits() -> dict:
    return {"version": CREDITS_STORE_VERSION, "users": {}, "pending": {}, "log": []}


def _read_credits() -> dict:
    path = _credits_path()
    if not path.exists():
        return _blank_credits()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _blank_credits()
    if not isinstance(data, dict):
        return _blank_credits()
    store = _blank_credits()
    for key in ("users", "pending"):
        if isinstance(data.get(key), dict):
            store[key] = data[key]
    if isinstance(data.get("log"), list):
        store["log"] = data["log"]
    return store


def _write_credits(store: dict) -> None:
    path = _credits_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Moved into place, so a deploy mid-write cannot leave truncated JSON -- which would
    # read back as nobody having any credits.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _log(store: dict, who: str, change: int, why: str, by: str = "", task_id: str | None = None) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "who": who,
             "change": change, "why": why}
    if by:
        entry["by"] = by
    if task_id:
        entry["task"] = task_id
    store["log"] = (store["log"] + [entry])[-CREDIT_LOG_LIMIT:]


def _clean_username(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _record(store: dict, user_id) -> dict:
    return store["users"].setdefault(str(user_id), {"credits": 0, "used": 0, "granted": 0})


def remember_user(user: dict) -> dict:
    """Note who this is, and hand them whatever was granted to their @username before the
    bot knew their id. Returns their record."""
    store = _read_credits()
    key = str(user["id"])
    changed = key not in store["users"]
    record = _record(store, key)
    username = _clean_username(user.get("username"))
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    if record.get("username") != username or record.get("name") != name:
        record["username"], record["name"] = username, name
        changed = True
    waiting = store["pending"].pop(username, 0) if username else 0
    if waiting:
        record["credits"] += waiting
        record["granted"] += waiting
        _log(store, key, waiting, "claim")
        changed = True
    if changed:
        _write_credits(store)
    return dict(record)


def balance(user_id) -> int:
    return int((_read_credits()["users"].get(str(user_id)) or {}).get("credits", 0))


def is_known(user_id) -> bool:
    return str(user_id) in _read_credits()["users"]


def spend_credit(user_id) -> bool:
    """Take one credit for a generation. False, and nothing taken, when there is none."""
    store = _read_credits()
    record = store["users"].get(str(user_id))
    if not record or record.get("credits", 0) < 1:
        return False
    record["credits"] -= 1
    record["used"] = record.get("used", 0) + 1
    _log(store, str(user_id), -1, "generation")
    _write_credits(store)
    return True


def refund_credit(user_id, task_id: str | None = None) -> int:
    """Give back the credit of a generation that produced no model. Returns the balance."""
    store = _read_credits()
    record = _record(store, user_id)
    record["credits"] += 1
    record["used"] = max(0, record.get("used", 0) - 1)
    _log(store, str(user_id), 1, "refund", task_id=task_id)
    _write_credits(store)
    return record["credits"]


def parse_credit_change(argument: str) -> tuple[str, int] | str:
    """Parse "@user 5", "@user -2" or "123456 +3" into (target, change), or return what is
    wrong with it."""
    parts = argument.split()
    if len(parts) != 2:
        return f"Формат: {CREDITS_COMMAND} @username 5 (или -2, чтобы списать)."
    target, raw = parts
    try:
        change = int(raw)
    except ValueError:
        return f"«{raw}» — не число. Формат: {CREDITS_COMMAND} @username 5"
    if change == 0 or abs(change) > CREDIT_CHANGE_LIMIT:
        return f"Количество — от 1 до {CREDIT_CHANGE_LIMIT}, со знаком минус, чтобы списать."
    if not _clean_username(target):
        return "Кому? Укажи @username или числовой id."
    return target, change


def change_credits(target: str, change: int, by: str = "") -> dict:
    """Grant (or take away, with a negative change) credits by @username or numeric id.

    Returns {"user_id": str | None, "label": str, "credits": int, "pending": bool}: a
    username the bot has never seen keeps the credits waiting for them (pending), with no
    id to notify.
    """
    store = _read_credits()
    needle = _clean_username(target)
    key = needle if needle.isdigit() else next(
        (k for k, r in store["users"].items() if r.get("username") == needle), None,
    )
    if key is None:
        credits = max(0, int(store["pending"].get(needle, 0)) + change)
        if credits:
            store["pending"][needle] = credits
        else:
            store["pending"].pop(needle, None)
        _log(store, f"@{needle}", change, "grant", by=by)
        _write_credits(store)
        return {"user_id": None, "label": f"@{needle}", "credits": credits, "pending": True}
    record = _record(store, key)
    record["credits"] = max(0, record["credits"] + change)
    if change > 0:
        record["granted"] = record.get("granted", 0) + change
    _log(store, key, change, "grant", by=by)
    _write_credits(store)
    label = f"@{record['username']}" if record.get("username") else (record.get("name") or f"id {key}")
    return {"user_id": key, "label": label, "credits": record["credits"], "pending": False}


def credits_overview() -> str:
    """The owner's list: every balance, then what is still waiting for a username."""
    store = _read_credits()
    lines = ["<b>Кредиты Tripo</b>", ""]
    people = sorted(
        store["users"].items(), key=lambda kv: (-kv[1].get("credits", 0), -kv[1].get("used", 0)),
    )
    for key, record in people:
        who = f"@{record['username']}" if record.get("username") else f"id {key}"
        if record.get("name"):
            who += f" ({record['name']})"
        lines.append(
            f"{html.escape(who)} — <b>{record.get('credits', 0)}</b>, потрачено {record.get('used', 0)}"
        )
    if not people:
        lines.append("Пока ни у кого нет кредитов.")
    if store["pending"]:
        lines += ["", "Ждут, пока человек откроет /tripo:"]
        lines += [
            f"@{html.escape(name)} — <b>{credits}</b>" for name, credits in sorted(store["pending"].items())
        ]
    lines += [
        "",
        f"Начислить: {CREDITS_COMMAND} @username 5",
        f"Списать: {CREDITS_COMMAND} @username -2",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- input


def image_from_message(message: dict) -> tuple[str | None, str | None]:
    """(file_id, None) for a usable picture, (None, reason) for an unusable one, and
    (None, None) when the message carries no picture at all."""
    photos = message.get("photo") or []
    if photos:
        return photos[-1].get("file_id"), None
    document = message.get("document") or {}
    if not document:
        return None, None
    if (document.get("mime_type") or "").lower() not in IMAGE_MIME_TYPES:
        return None, None
    if (document.get("file_size") or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        return None, "Файл больше 20 МБ: Telegram не отдаёт боту такие файлы. Пришли его как фото."
    return document.get("file_id"), None


def prepare_image(data: bytes) -> str:
    """Telegram picture → base64 JPEG, exactly as the web node encodes its inputs."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX), Image.LANCZOS)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=IMAGE_JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def prepare_images(images: dict[str, bytes]) -> dict[str, str]:
    return {view: prepare_image(data) for view, data in images.items()}


def submit_payload(encoded: dict[str, str], faces: int) -> dict:
    """The body the web Tripo node posts for Image → 3D with Ultra geometry."""
    return {
        "provider": "tripo",
        "action": "submit",
        "mode": "image",
        "prompt": "",
        "negativePrompt": "",
        "modelVersion": MODEL_VERSION,
        "geometryQuality": "detailed",
        "faceLimit": faces,
        "texture": False,
        "pbr": False,
        "textureQuality": "standard",
        "quad": False,
        "generateParts": False,
        "mainImageBase64": encoded["front"],
        "viewImages": {v: encoded[v] for v in VIEWS[1:] if v in encoded},
    }


# --------------------------------------------------------------------------- output


def model_file(files: list[dict]) -> dict | None:
    models = [f for f in files if (f.get("Type") or "").upper() not in ("IMAGE", "PNG", "JPG", "WEBP")]
    return next((f for f in models if f.get("Type", "").upper() == "GLB"), models[0] if models else None)


def preview_file(files: list[dict]) -> dict | None:
    return next(
        (f for f in files if (f.get("Type") or "").upper() in ("IMAGE", "PNG", "JPG", "WEBP")), None
    )


def model_name(task_id: str, faces: int | None, extension: str) -> str:
    budget = f"_{format_faces(faces)}" if faces else ""
    return f"tripo{budget}_{task_id[:8]}.{extension.lower()}"


def zip_model(path: Path) -> Path:
    """A big GLB deflates by a useful fraction, often enough to fit Telegram's 50 MB."""
    target = path.with_suffix(path.suffix + ".zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(path, arcname=path.name)
    return target


def preview_to_jpeg(data: bytes, path: Path) -> Path:
    """Tripo's render is WebP; a JPEG is what Telegram takes as a photo without fuss."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        flat.save(path, "JPEG", quality=90)
    return path


# --------------------------------------------------------------------------- the web app


class TripoWebClient:
    """image generator web's /api/model3d, spoken to the way its browser client does."""

    def __init__(self, session: aiohttp.ClientSession, endpoint: str, key: str = ""):
        self._session = session
        self._endpoint = endpoint
        self._key = key

    async def _post(self, body: dict, timeout: float) -> dict:
        if self._key:
            body = {**body, "tripoKey": self._key}
        try:
            async with self._session.post(
                self._endpoint, json=body, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TripoWebError(f"image generator web не отвечает: {e or type(e).__name__}") from e
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"error": text.strip()[:300] or f"HTTP {status}"}
        if not isinstance(data, dict):
            data = {"error": str(data)[:300]}
        if status >= 400:
            raise TripoWebError(data.get("error") or f"HTTP {status}", status)
        return data

    async def submit(self, payload: dict, sleep=asyncio.sleep, on_wait=None) -> str:
        """Start a task and return its id, waiting out Tripo's 429s the way the web does."""
        for attempt in range(len(SUBMIT_BACKOFF_SECONDS) + 1):
            try:
                data = await self._post(payload, timeout=90)
            except TripoWebError as e:
                if e.status != 429 or attempt >= len(SUBMIT_BACKOFF_SECONDS):
                    raise
                if on_wait is not None:
                    await on_wait()
                await sleep(SUBMIT_BACKOFF_SECONDS[attempt])
                continue
            task_id = data.get("taskId")
            if not task_id:
                raise TripoWebError("Tripo не вернул номер задачи.")
            return task_id

    async def status(self, task_id: str) -> dict:
        return await self._post({"provider": "tripo", "action": "status", "taskId": task_id}, timeout=30)

    async def download(self, url: str, path: Path, limit: int = MODEL_DOWNLOAD_LIMIT) -> int:
        """Stream a Tripo file to disk. Returns its size."""
        size = 0
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status >= 400:
                    raise TripoWebError(f"Tripo не отдал файл (HTTP {resp.status})", resp.status)
                out = await asyncio.to_thread(open, path, "wb")
                try:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > limit:
                            raise TripoWebError("Файл модели слишком большой.")
                        await asyncio.to_thread(out.write, chunk)
                finally:
                    await asyncio.to_thread(out.close)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TripoWebError(f"Не удалось скачать файл: {e or type(e).__name__}") from e
        return size
