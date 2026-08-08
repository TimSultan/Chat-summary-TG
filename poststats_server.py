"""Standalone, self-owned deployment for the /poststats page.

This is intentionally separate from listener.py.  The first visitor to a fresh Railway
deployment completes Telegram's normal phone/code login in the browser.  The resulting
session belongs to that deployment's owner and is stored only on its mounted DATA_DIR;
it is never mixed with another owner's Telegram account or group list.

Telegram's Bot API cannot retrieve the historical view/forward/reaction/comment metrics
that /poststats displays, so this uses the owner's Telegram account rather than a
BotFather bot token.  No OpenAI key is involved.
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
CONFIG_PATH = DATA_DIR / "poststats_owner.json"
PORT = int(os.getenv("PORT", "8080"))


class ClientProxy:
    """Stable object passed to post_stats_web while its owner logs in at runtime."""

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
    client_proxy: ClientProxy
    runtime_cfg: SimpleNamespace
    pending_login: PendingLogin | None
    owner_ready: bool
    setup_code: str


_RUNTIME_KEY = web.AppKey("poststats_runtime", Runtime)


def _load_owner() -> dict | None:
    try:
        owner = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        print(f"[poststats] cannot read {CONFIG_PATH}: {e}")
        return None
    required = ("api_id", "api_hash", "session_string", "access_token")
    if not all(owner.get(key) for key in required):
        print("[poststats] owner configuration is incomplete; opening setup page")
        return None
    return owner


def _save_owner(owner: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".poststats-", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(owner, handle, ensure_ascii=False)
        os.replace(temp_name, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


async def _connect_owner(owner: dict) -> TelegramClient:
    client = TelegramClient(StringSession(owner["session_string"]), int(owner["api_id"]), owner["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("saved Telegram session is no longer authorized")
    me = await client.get_me()
    print(f"[poststats] connected as @{getattr(me, 'username', None) or me.id}")
    return client


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up Post Stats</title>
<style>body{max-width:520px;margin:48px auto;padding:0 18px;font:15px/1.5 system-ui,sans-serif;color:#161616}h1{margin-bottom:4px}p{color:#555}label{display:block;margin:14px 0 5px;font-weight:600}input{box-sizing:border-box;width:100%;padding:10px;border:1px solid #bbb;border-radius:6px;font:inherit}button{margin-top:18px;padding:10px 16px;border:0;border-radius:6px;background:#1677ff;color:white;font:inherit;font-weight:600;cursor:pointer}.error{color:#b42318;min-height:24px;margin-top:14px}.hint{font-size:13px}.hidden{display:none}</style>
</head><body><h1>Set up Post Stats</h1>
<p>This connects <strong>this Railway deployment</strong> to your Telegram account. It is required because Telegram does not give historical post analytics to BotFather bots.</p>
<p class="hint">Create an API application at <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a>. These values stay in this deployment. No OpenAI key is needed.</p>
<form id="startForm"><label>Setup code<input id="setupCode" required autocomplete="off"></label><label>Telegram API ID<input id="apiId" inputmode="numeric" required></label><label>Telegram API hash<input id="apiHash" required autocomplete="off"></label><label>Telegram phone number<input id="phone" placeholder="+441234567890" required autocomplete="tel"></label><button>Send login code</button></form>
<form id="codeForm" class="hidden"><label>Telegram login code<input id="code" inputmode="numeric" autocomplete="one-time-code" required></label><button>Finish sign-in</button></form>
<form id="passwordForm" class="hidden"><label>Two-step verification password<input id="password" type="password" autocomplete="current-password" required></label><button>Finish sign-in</button></form>
<div id="error" class="error" role="alert"></div>
<script>const $=id=>document.getElementById(id),err=m=>$('error').textContent=m||'',headers=()=>({'Content-Type':'application/json','X-PostStats-Setup-Code':$('setupCode').value.trim()});async function post(url,data){let r=await fetch(url,{method:'POST',headers:headers(),body:JSON.stringify(data)}),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Request failed');return j}$('startForm').onsubmit=async e=>{e.preventDefault();err('');try{await post('/poststats/setup/send-code',{api_id:$('apiId').value,api_hash:$('apiHash').value,phone:$('phone').value});$('startForm').classList.add('hidden');$('codeForm').classList.remove('hidden');$('code').focus()}catch(x){err(x.message)}};$('codeForm').onsubmit=async e=>{e.preventDefault();err('');try{let r=await post('/poststats/setup/verify-code',{code:$('code').value});if(r.password_required){$('codeForm').classList.add('hidden');$('passwordForm').classList.remove('hidden');$('password').focus()}else location.href=r.redirect_url}catch(x){err(x.message)}};$('passwordForm').onsubmit=async e=>{e.preventDefault();err('');try{let r=await post('/poststats/setup/verify-password',{password:$('password').value});location.href=r.redirect_url}catch(x){err(x.message)}};</script></body></html>"""


async def handle_setup(request: web.Request) -> web.Response:
    if request.app[_RUNTIME_KEY].owner_ready:
        raise web.HTTPFound("/poststats")
    return web.Response(text=SETUP_PAGE, content_type="text/html")


async def _drop_pending(app: web.Application) -> None:
    runtime = app[_RUNTIME_KEY]
    pending = runtime.pending_login
    runtime.pending_login = None
    if pending is not None:
        await pending.client.disconnect()


def _setup_authorized(request: web.Request) -> bool:
    supplied = request.headers.get("X-PostStats-Setup-Code", "")
    return hmac.compare_digest(supplied, request.app[_RUNTIME_KEY].setup_code)


async def handle_send_code(request: web.Request) -> web.Response:
    if request.app[_RUNTIME_KEY].owner_ready:
        return _json_error("Post Stats is already configured.", 409)
    if not _setup_authorized(request):
        return _json_error("Incorrect setup code.", 403)
    try:
        body = await request.json()
        api_id = int(str(body.get("api_id", "")).strip())
        api_hash = str(body.get("api_hash", "")).strip()
        phone = str(body.get("phone", "")).strip()
    except (ValueError, TypeError, json.JSONDecodeError):
        return _json_error("Enter a numeric API ID, API hash, and phone number.")
    if api_id <= 0 or not api_hash or not phone:
        return _json_error("Enter a numeric API ID, API hash, and phone number.")

    await _drop_pending(request.app)
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        return _json_error(f"Telegram could not send a login code: {e}", 502)
    request.app[_RUNTIME_KEY].pending_login = PendingLogin(
        client, api_id, api_hash, phone, sent.phone_code_hash
    )
    return web.json_response({"ok": True})


async def _finish_login(request: web.Request, password: str | None = None) -> web.Response:
    if not _setup_authorized(request):
        return _json_error("Incorrect setup code.", 403)
    runtime = request.app[_RUNTIME_KEY]
    pending = runtime.pending_login
    if pending is None:
        return _json_error("Start again and request a fresh Telegram login code.", 409)
    try:
        if password is None:
            body = await request.json()
            code = str(body.get("code", "")).strip()
            if not code:
                return _json_error("Enter the login code.")
            await pending.client.sign_in(pending.phone, code, phone_code_hash=pending.phone_code_hash)
        else:
            if not password:
                return _json_error("Enter the two-step verification password.")
            await pending.client.sign_in(password=password)
    except SessionPasswordNeededError:
        return web.json_response({"password_required": True})
    except Exception as e:
        return _json_error(f"Telegram sign-in failed: {e}", 401)

    owner = {
        "api_id": pending.api_id,
        "api_hash": pending.api_hash,
        "session_string": pending.client.session.save(),
        "access_token": secrets.token_urlsafe(32),
    }
    try:
        _save_owner(owner)
    except OSError as e:
        return _json_error(f"Could not save configuration: {e}", 500)

    runtime.runtime_cfg.post_stats_access_token = owner["access_token"]
    runtime.client_proxy.client = pending.client
    runtime.owner_ready = True
    runtime.pending_login = None
    print("[poststats] owner setup completed")
    # post_stats_page immediately stores this in localStorage then removes it from the URL.
    return web.json_response({"redirect_url": f"/poststats?token={owner['access_token']}"})


async def handle_verify_code(request: web.Request) -> web.Response:
    return await _finish_login(request)


async def handle_verify_password(request: web.Request) -> web.Response:
    try:
        password = str((await request.json()).get("password", ""))
    except (TypeError, json.JSONDecodeError):
        password = ""
    return await _finish_login(request, password=password)


async def handle_poststats_page(request: web.Request) -> web.Response:
    if not request.app[_RUNTIME_KEY].owner_ready:
        raise web.HTTPFound("/poststats/setup")
    return await post_stats_web.handle_page(request)


async def cleanup(app: web.Application) -> None:
    await _drop_pending(app)
    client = app[_RUNTIME_KEY].client_proxy.client
    if client is not None:
        await client.disconnect()


async def create_app() -> web.Application:
    app = web.Application()
    proxy = ClientProxy()
    runtime_cfg = SimpleNamespace(post_stats_access_token=None, post_stats_scoped_tokens={})
    runtime = Runtime(proxy, runtime_cfg, None, False, secrets.token_urlsafe(24))
    app[_RUNTIME_KEY] = runtime

    owner = _load_owner()
    if owner:
        try:
            proxy.client = await _connect_owner(owner)
            runtime_cfg.post_stats_access_token = owner["access_token"]
            runtime.owner_ready = True
        except Exception as e:
            print(f"[poststats] saved login is unavailable; opening setup page: {e}")
    if not runtime.owner_ready:
        print(f"[poststats] setup code (shown only in Railway logs): {runtime.setup_code}")

    app.add_routes([
        web.get("/", handle_setup),
        web.get("/poststats/setup", handle_setup),
        web.post("/poststats/setup/send-code", handle_send_code),
        web.post("/poststats/setup/verify-code", handle_verify_code),
        web.post("/poststats/setup/verify-password", handle_verify_password),
    ])
    post_stats_web.attach(app, proxy, runtime_cfg, page_handler=handle_poststats_page, log=print)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    print(f"[poststats] starting standalone server on port {PORT}")
    web.run_app(create_app(), port=PORT)


if __name__ == "__main__":
    main()
