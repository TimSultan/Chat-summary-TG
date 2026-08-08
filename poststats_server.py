"""Standalone owner-owned launcher for historical Telegram post statistics.

Run this file only through Dockerfile.poststats.  It deliberately does not import or
start listener.py, bot_listener.py, or any summary/OpenAI functionality.

The first owner opens /setup, signs into *their own* Telegram account with Telegram's
normal phone/code flow, and this service stores the resulting session under DATA_DIR.
The existing /poststats UI then reads only through that owner's Telegram client.
"""

import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

import post_stats_web


DATA_DIR = Path(os.getenv("DATA_DIR", "."))
OWNER_PATH = DATA_DIR / "poststats_owner.json"
PORT = int(os.getenv("PORT", "8080"))


class ClientProxy:
    """Keeps post_stats_web's client reference stable across browser setup."""

    client: TelegramClient | None = None

    def __getattr__(self, name):
        if self.client is None:
            raise RuntimeError("Post Stats has not been connected to Telegram yet.")
        return getattr(self.client, name)


@dataclass
class PendingLogin:
    client: TelegramClient
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str


@dataclass
class Runtime:
    proxy: ClientProxy
    config: SimpleNamespace
    setup_code: str
    owner_ready: bool = False
    pending: PendingLogin | None = None


RUNTIME_KEY = web.AppKey("poststats_runtime", Runtime)


def _read_owner() -> dict | None:
    try:
        owner = json.loads(OWNER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        print(f"[poststats] unreadable owner configuration: {e}")
        return None
    if not all(owner.get(name) for name in ("api_id", "api_hash", "session_string", "access_token")):
        print("[poststats] incomplete owner configuration; setup is required")
        return None
    return owner


def _write_owner(owner: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".poststats-", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(owner, file, ensure_ascii=False)
        os.replace(temporary, OWNER_PATH)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


async def _connect_saved_owner(owner: dict) -> TelegramClient:
    client = TelegramClient(StringSession(owner["session_string"]), int(owner["api_id"]), owner["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("the saved Telegram session is no longer authorized")
    me = await client.get_me()
    print(f"[poststats] connected as @{getattr(me, 'username', None) or me.id}")
    return client


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


SETUP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up Post Stats</title><style>body{max-width:520px;margin:48px auto;padding:0 18px;font:15px/1.5 system-ui,sans-serif;color:#161616}h1{margin-bottom:4px}p{color:#555}label{display:block;margin:14px 0 5px;font-weight:600}input{box-sizing:border-box;width:100%;padding:10px;border:1px solid #bbb;border-radius:6px;font:inherit}button{margin-top:18px;padding:10px 16px;border:0;border-radius:6px;background:#1677ff;color:#fff;font:inherit;font-weight:600;cursor:pointer}.error{color:#b42318;min-height:24px;margin-top:14px}.hint{font-size:13px}.hidden{display:none}</style>
</head><body><h1>Set up Post Stats</h1><p>This deployment reads the historical post statistics available to <strong>your own Telegram account</strong>. It does not use the summary bot, OpenAI, or another person's Telegram account.</p><p class="hint">Create an API application at <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a>. Get the one-time setup code from this deployment's Railway logs.</p>
<form id="start"><label>Setup code<input id="setupCode" required autocomplete="off"></label><label>Telegram API ID<input id="apiId" inputmode="numeric" required></label><label>Telegram API hash<input id="apiHash" required autocomplete="off"></label><label>Phone number<input id="phone" placeholder="+441234567890" autocomplete="tel" required></label><button>Send Telegram code</button></form><form id="codeForm" class="hidden"><label>Telegram login code<input id="code" inputmode="numeric" autocomplete="one-time-code" required></label><button>Finish sign-in</button></form><form id="passwordForm" class="hidden"><label>Two-step verification password<input id="password" type="password" autocomplete="current-password" required></label><button>Finish sign-in</button></form><div id="error" class="error" role="alert"></div>
<script>const $=id=>document.getElementById(id),err=m=>$('error').textContent=m||'',headers=()=>({'Content-Type':'application/json','X-PostStats-Setup-Code':$('setupCode').value.trim()});async function post(url,data){let r=await fetch(url,{method:'POST',headers:headers(),body:JSON.stringify(data)}),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Request failed');return j}$('start').onsubmit=async e=>{e.preventDefault();err('');try{await post('/setup/send-code',{api_id:$('apiId').value,api_hash:$('apiHash').value,phone:$('phone').value});$('start').classList.add('hidden');$('codeForm').classList.remove('hidden');$('code').focus()}catch(x){err(x.message)}};$('codeForm').onsubmit=async e=>{e.preventDefault();err('');try{let r=await post('/setup/verify-code',{code:$('code').value});if(r.password_required){$('codeForm').classList.add('hidden');$('passwordForm').classList.remove('hidden');$('password').focus()}else location.href=r.redirect_url}catch(x){err(x.message)}};$('passwordForm').onsubmit=async e=>{e.preventDefault();err('');try{let r=await post('/setup/verify-password',{password:$('password').value});location.href=r.redirect_url}catch(x){err(x.message)}};</script></body></html>"""


def _setup_is_authorized(request: web.Request) -> bool:
    return hmac.compare_digest(
        request.headers.get("X-PostStats-Setup-Code", ""), request.app[RUNTIME_KEY].setup_code
    )


async def _discard_pending(runtime: Runtime) -> None:
    pending, runtime.pending = runtime.pending, None
    if pending is not None:
        await pending.client.disconnect()


async def handle_setup(request: web.Request) -> web.Response:
    if request.app[RUNTIME_KEY].owner_ready:
        raise web.HTTPFound("/poststats")
    return web.Response(text=SETUP_HTML, content_type="text/html")


async def handle_send_code(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if runtime.owner_ready:
        return _error("Post Stats is already configured.", 409)
    if not _setup_is_authorized(request):
        return _error("Incorrect setup code.", 403)
    try:
        body = await request.json()
        api_id = int(str(body.get("api_id", "")).strip())
        api_hash = str(body.get("api_hash", "")).strip()
        phone = str(body.get("phone", "")).strip()
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("Enter a numeric API ID, API hash, and phone number.")
    if api_id <= 0 or not api_hash or not phone:
        return _error("Enter a numeric API ID, API hash, and phone number.")

    await _discard_pending(runtime)
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        return _error(f"Telegram could not send a login code: {e}", 502)
    runtime.pending = PendingLogin(client, api_id, api_hash, phone, sent.phone_code_hash)
    return web.json_response({"ok": True})


async def _complete_login(request: web.Request, password: str | None = None) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if not _setup_is_authorized(request):
        return _error("Incorrect setup code.", 403)
    if runtime.pending is None:
        return _error("Start again and request a fresh Telegram login code.", 409)
    try:
        if password is None:
            code = str((await request.json()).get("code", "")).strip()
            if not code:
                return _error("Enter the login code.")
            await runtime.pending.client.sign_in(
                runtime.pending.phone, code, phone_code_hash=runtime.pending.phone_code_hash
            )
        else:
            if not password:
                return _error("Enter the two-step verification password.")
            await runtime.pending.client.sign_in(password=password)
    except SessionPasswordNeededError:
        return web.json_response({"password_required": True})
    except Exception as e:
        return _error(f"Telegram sign-in failed: {e}", 401)

    owner = {
        "api_id": runtime.pending.api_id,
        "api_hash": runtime.pending.api_hash,
        "session_string": runtime.pending.client.session.save(),
        "access_token": secrets.token_urlsafe(32),
    }
    try:
        _write_owner(owner)
    except OSError as e:
        return _error(f"Could not save the owner configuration: {e}", 500)
    runtime.config.post_stats_access_token = owner["access_token"]
    runtime.proxy.client = runtime.pending.client
    runtime.owner_ready = True
    runtime.pending = None
    print("[poststats] owner setup completed")
    # The existing page immediately puts this token in localStorage and removes it from the URL.
    return web.json_response({"redirect_url": f"/poststats?token={owner['access_token']}"})


async def handle_verify_code(request: web.Request) -> web.Response:
    return await _complete_login(request)


async def handle_verify_password(request: web.Request) -> web.Response:
    try:
        password = str((await request.json()).get("password", ""))
    except (TypeError, json.JSONDecodeError):
        password = ""
    return await _complete_login(request, password=password)


async def cleanup(app: web.Application) -> None:
    runtime = app[RUNTIME_KEY]
    await _discard_pending(runtime)
    if runtime.proxy.client is not None:
        await runtime.proxy.client.disconnect()


async def create_app() -> web.Application:
    app = web.Application()
    runtime = Runtime(
        proxy=ClientProxy(),
        config=SimpleNamespace(post_stats_access_token=None, post_stats_scoped_tokens={}),
        setup_code=secrets.token_urlsafe(24),
    )
    app[RUNTIME_KEY] = runtime

    saved_owner = _read_owner()
    if saved_owner:
        try:
            runtime.proxy.client = await _connect_saved_owner(saved_owner)
            runtime.config.post_stats_access_token = saved_owner["access_token"]
            runtime.owner_ready = True
        except Exception as e:
            print(f"[poststats] saved login is unavailable; setup is required: {e}")
    if not runtime.owner_ready:
        print(f"[poststats] setup code (shown only in Railway logs): {runtime.setup_code}")

    app.add_routes([
        web.get("/", handle_setup),
        web.get("/setup", handle_setup),
        web.post("/setup/send-code", handle_send_code),
        web.post("/setup/verify-code", handle_verify_code),
        web.post("/setup/verify-password", handle_verify_password),
    ])
    # Existing /poststats UI and API are reused unchanged.
    post_stats_web.attach(app, runtime.proxy, runtime.config, log=print)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    print(f"[poststats] standalone server listening on {PORT}")
    web.run_app(create_app(), port=PORT)


if __name__ == "__main__":
    main()
