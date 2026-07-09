"""Small desktop window for chat_summary_tg -- generate one-off summaries and run the
live mention-triggered listener without needing to remember CLI flags.

Run with: python gui.py
"""

import asyncio
import os
import queue
import threading
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import history
import listener as listener_mod
from config import RECOMMENDED_MODELS, build_session, load_config
from errors import ChatSummaryError
from main import parse_date_range, period_label, resolve_tz, safe_filename
from summarizer import summarize_transcript
from telegram_fetch import fetch_range_messages_cached, format_transcript_lines, sender_matches

HISTORY_REFRESH_MS = 5000


class App:
    def __init__(self, root):
        self.root = root
        root.title("Chat Summary TG")
        root.geometry("640x600")
        root.minsize(560, 480)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.busy = False
        self.listener_running = False
        self.listener_client = None
        self.listener_loop = None
        self.last_output_path: Path | None = None

        self._build_ui()
        self.root.after(100, self._poll_log_queue)
        self._refresh_history()
        self.root.after(HISTORY_REFRESH_MS, self._periodic_history_refresh)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------

    def _build_ui(self):
        try:
            default_model = load_config().openai_model
        except ChatSummaryError:
            default_model = RECOMMENDED_MODELS[0]

        model_frame = ttk.Frame(self.root)
        model_frame.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(model_frame, text="Model:").pack(side="left")
        self.model_var = tk.StringVar(value=default_model)
        ttk.Combobox(
            model_frame, textvariable=self.model_var, values=RECOMMENDED_MODELS, width=18
        ).pack(side="left", padx=(4, 8))
        ttk.Label(model_frame, text="(applies to Generate Summary and the Listener)").pack(side="left")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="x", padx=8, pady=8)

        summary_tab = ttk.Frame(notebook)
        listener_tab = ttk.Frame(notebook)
        notebook.add(summary_tab, text="Generate Summary")
        notebook.add(listener_tab, text="Live Listener")

        self.chat_var = tk.StringVar()
        self.date_var = tk.StringVar(value="today")
        self.user_var = tk.StringVar()
        self.tz_var = tk.StringVar()

        self._labeled_entry(summary_tab, "Chat (@username, ID, or title):", self.chat_var, 0)
        self._labeled_entry(
            summary_tab,
            "Date (today / yesterday / YYYY-MM-DD / YYYY-MM-DD:YYYY-MM-DD / last7days):",
            self.date_var,
            1,
        )
        self._labeled_entry(summary_tab, "User filter (optional, @username):", self.user_var, 2)
        self._labeled_entry(summary_tab, "Timezone (optional, e.g. Europe/Istanbul):", self.tz_var, 3)
        summary_tab.columnconfigure(1, weight=1)

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            summary_tab,
            text="Force refresh (ignore cached transcript, re-fetch from Telegram)",
            variable=self.force_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        btn_frame = ttk.Frame(summary_tab)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=8, sticky="w")
        self.generate_btn = ttk.Button(btn_frame, text="Generate Summary", command=self._on_generate)
        self.generate_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(
            btn_frame, text="Open Output Folder", command=self._open_output_folder, state="disabled"
        )
        self.open_folder_btn.pack(side="left", padx=8)

        self.generate_status = ttk.Label(summary_tab, text="Status: idle")
        self.generate_status.grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        info = ttk.Label(
            listener_tab,
            text=(
                "Watches chats you're in for @mentions/replies containing a trigger keyword\n"
                "(default 'summary') and replies with a themed digest, as you.\n"
                "Set LISTENER_ALLOWED_CHATS in .env before using this for real -- otherwise it\n"
                "answers anyone who mentions you in any chat, spending your OpenAI budget."
            ),
            justify="left",
        )
        info.pack(anchor="w", padx=8, pady=8)

        listener_btn_frame = ttk.Frame(listener_tab)
        listener_btn_frame.pack(anchor="w", padx=8)
        self.start_listener_btn = ttk.Button(listener_btn_frame, text="Start Listener", command=self._on_start_listener)
        self.start_listener_btn.pack(side="left")
        self.stop_listener_btn = ttk.Button(
            listener_btn_frame, text="Stop Listener", command=self._on_stop_listener, state="disabled"
        )
        self.stop_listener_btn.pack(side="left", padx=8)
        self.listener_status = ttk.Label(listener_tab, text="Status: stopped")
        self.listener_status.pack(anchor="w", padx=8, pady=4)

        history_tab = ttk.Frame(notebook)
        notebook.add(history_tab, text="History")

        ttk.Label(
            history_tab,
            text="Every question the listener answered -- who asked, in which chat, and when.",
            justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        columns = ("time", "chat", "from", "question")
        self.history_tree = ttk.Treeview(history_tab, columns=columns, show="headings", height=12)
        for col, label, width in [
            ("time", "Time", 130),
            ("chat", "Chat", 140),
            ("from", "From", 110),
            ("question", "Question", 220),
        ]:
            self.history_tree.heading(col, text=label)
            self.history_tree.column(col, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.history_tree.bind("<Double-1>", lambda e: self._open_history_answer())

        history_btn_frame = ttk.Frame(history_tab)
        history_btn_frame.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(history_btn_frame, text="Open Answer", command=self._open_history_answer).pack(side="left")
        ttk.Button(history_btn_frame, text="Refresh", command=self._refresh_history).pack(side="left", padx=8)

        ttk.Label(self.root, text="Log:").pack(anchor="w", padx=8)
        self.log_text = scrolledtext.ScrolledText(self.root, height=20, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _labeled_entry(self, parent, label, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(parent, textvariable=var, width=40).grid(row=row, column=1, sticky="ew", padx=8, pady=4)

    # ---------- logging (thread-safe: workers push, main thread drains) ----------

    def log(self, message: str):
        self.log_queue.put(str(message))

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    # ---------- login dialogs (must run on the main thread) ----------

    def ask(self, title, prompt, show=None):
        result = {}
        done = threading.Event()

        def _show():
            result["value"] = simpledialog.askstring(title, prompt, show=show, parent=self.root)
            done.set()

        self.root.after(0, _show)
        done.wait()
        return result.get("value")

    async def ensure_login(self, client: TelegramClient):
        await client.connect()
        if await client.is_user_authorized():
            return
        self.log("Not logged in yet -- check for a login dialog box.")
        phone = self.ask("Telegram Login", "Phone number (with country code, e.g. +15551234567):")
        if not phone:
            raise ChatSummaryError("Login cancelled: no phone number entered.")
        await client.send_code_request(phone)
        code = self.ask("Telegram Login", "Login code Telegram just sent you:")
        if not code:
            raise ChatSummaryError("Login cancelled: no code entered.")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pwd = self.ask("Telegram Login", "Two-factor password:", show="*")
            if not pwd:
                raise ChatSummaryError("Login cancelled: no 2FA password entered.")
            await client.sign_in(password=pwd)
        self.log("Logged in and session saved.")

    # ---------- one-off summary ----------

    def _on_generate(self):
        if self.busy:
            return
        if self.listener_running:
            messagebox.showwarning("Busy", "Stop the listener first -- both use the same Telegram session.")
            return

        chat = self.chat_var.get().strip()
        if not chat:
            messagebox.showerror("Missing chat", "Enter a chat username, ID, or title.")
            return

        date_str = self.date_var.get().strip() or "today"
        tz_str = self.tz_var.get().strip()

        # Validate eagerly, on the main thread, so typos show up immediately as a
        # dialog instead of only surfacing after a background round trip.
        try:
            parse_date_range(date_str)
            resolve_tz(tz_str or None)
            load_config()
        except ChatSummaryError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        self.busy = True
        self.generate_btn.configure(state="disabled")
        self.generate_status.configure(text=f"Status: generating... ({chat}, {date_str})")
        self.log(f"=== Generation started: chat='{chat}' date='{date_str}' user='{self.user_var.get().strip() or '(none)'}' ===")
        threading.Thread(
            target=self._run_generate_thread,
            args=(chat, date_str, self.user_var.get(), tz_str, self.force_var.get()),
            daemon=True,
        ).start()

    def _run_generate_thread(self, chat, date_str, user, tz_name, force):
        try:
            out_path = asyncio.run(self._generate_summary(chat, date_str, user, tz_name, force))
        except ChatSummaryError as e:
            # Capture the message as a plain string before scheduling: `except ... as e`
            # unbinds `e` when the block exits, which would leave the deferred lambda
            # below referencing a name that no longer exists by the time it runs.
            status_text = f"Status: FAILED -- {e}"
            self.log(f"=== Generation FAILED: {e} ===")
            self.root.after(0, lambda: self.generate_status.configure(text=status_text))
        except Exception:
            tb = traceback.format_exc()
            self.log("=== Generation FAILED (unexpected error) ===\n" + tb)
            self.root.after(0, lambda: self.generate_status.configure(text="Status: FAILED -- see log for details"))
        else:
            self.log(f"=== Generation SUCCEEDED: saved to {out_path} ===")
            self.root.after(0, lambda: self.generate_status.configure(text=f"Status: done -- saved to {out_path.name}"))
        finally:
            self.busy = False
            self.root.after(0, lambda: self.generate_btn.configure(state="normal"))

    async def _generate_summary(self, chat, date_str, user, tz_name, force=False):
        cfg = load_config()
        cfg.openai_model = self.model_var.get().strip() or cfg.openai_model
        tz = resolve_tz(tz_name.strip() or None)
        start_day, end_day = parse_date_range(date_str.strip() or "today")
        label = period_label(start_day, end_day)
        user = user.strip() or None

        client = TelegramClient(build_session(cfg), cfg.api_id, cfg.api_hash)
        try:
            await self.ensure_login(client)
            self.log(f"Fetching messages from '{chat}' for {label} ({tz})...")
            chat_title, messages = await fetch_range_messages_cached(
                client, chat, start_day, end_day, tz, log=self.log, force_refresh=force
            )
        finally:
            await client.disconnect()

        if user:
            matched = sum(1 for m in messages if sender_matches(m, user))
            self.log(f"Fetched {len(messages)} messages from '{chat_title}' ({matched} from {user}).")
        else:
            self.log(f"Fetched {len(messages)} messages from '{chat_title}'.")

        lines = format_transcript_lines(messages, include_date=(start_day != end_day))

        self.log(f"Summarizing with {cfg.openai_model}...")
        summary_md = summarize_transcript(
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            chat_title=chat_title,
            period_label=label,
            lines=lines,
            focus_user=user,
            style="file",
        )

        participants = len({m.sender_name for m in messages})
        generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
        title_suffix = f" — {user}" if user else ""
        header = (
            f"# {chat_title}{title_suffix} — Summary for {label}\n\n"
            f"*Generated {generated_at}. {len(messages)} messages from {participants} participants.*\n\n"
        )
        full_md = header + summary_md + "\n"

        out_dir = Path("output")
        out_dir.mkdir(parents=True, exist_ok=True)
        user_suffix = f"_{safe_filename(user)}" if user else ""
        date_part = start_day.isoformat() if start_day == end_day else f"{start_day.isoformat()}_{end_day.isoformat()}"
        out_path = out_dir / f"{safe_filename(chat_title)}{user_suffix}_{date_part}.md"
        out_path.write_text(full_md, encoding="utf-8")

        self.last_output_path = out_path
        self.log(f"Saved to {out_path}")
        self.root.after(0, lambda: self.open_folder_btn.configure(state="normal"))
        return out_path

    def _open_output_folder(self):
        folder = self.last_output_path.parent if self.last_output_path else Path("output")
        os.startfile(folder)

    # ---------- history ----------

    def _refresh_history(self):
        entries = history.load_all()
        self.history_tree.delete(*self.history_tree.get_children())
        for entry in reversed(entries):  # newest first
            ts = entry.timestamp[:16].replace("T", " ")
            preview = entry.question if len(entry.question) <= 60 else entry.question[:57] + "..."
            self.history_tree.insert(
                "", "end", iid=entry.answer_path, values=(ts, entry.chat_title, entry.requester, preview)
            )

    def _periodic_history_refresh(self):
        self._refresh_history()
        self.root.after(HISTORY_REFRESH_MS, self._periodic_history_refresh)

    def _open_history_answer(self):
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showinfo("No selection", "Select a row first.")
            return
        os.startfile(selected[0])

    # ---------- listener ----------

    def _on_start_listener(self):
        if self.listener_running:
            return
        if self.busy:
            messagebox.showwarning("Busy", "Wait for the current summary run to finish first.")
            return

        try:
            load_config()
        except ChatSummaryError as e:
            messagebox.showerror("Invalid config", str(e))
            return

        self.listener_running = True
        self.start_listener_btn.configure(state="disabled")
        self.stop_listener_btn.configure(state="normal")
        self.listener_status.configure(text="Status: starting...")
        threading.Thread(target=self._run_listener_thread, daemon=True).start()

    def _run_listener_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.listener_loop = loop
        try:
            loop.run_until_complete(self._listener_main())
        except ChatSummaryError as e:
            self.log(f"Listener error: {e}")
        except Exception:
            self.log("Unexpected listener error:\n" + traceback.format_exc())
        finally:
            loop.close()
            self.listener_loop = None
            self.listener_client = None
            self.listener_running = False
            self.root.after(0, self._on_listener_stopped_ui)

    async def _listener_main(self):
        cfg = load_config()
        cfg.openai_model = self.model_var.get().strip() or cfg.openai_model
        tz = resolve_tz(None)
        client = TelegramClient(build_session(cfg), cfg.api_id, cfg.api_hash)
        self.listener_client = client
        await self.ensure_login(client)
        me = await client.get_me()
        if not me.username:
            await client.disconnect()
            raise ChatSummaryError(
                "Your Telegram account has no @username set -- people can't @mention you."
            )
        self.root.after(0, lambda: self.listener_status.configure(text=f"Status: running as @{me.username}"))
        await listener_mod.run_listener(client, cfg, tz, log=self.log)

    def _on_stop_listener(self):
        if self.listener_client and self.listener_loop:
            self.log("Stopping listener...")
            asyncio.run_coroutine_threadsafe(self.listener_client.disconnect(), self.listener_loop)

    def _on_listener_stopped_ui(self):
        self.start_listener_btn.configure(state="normal")
        self.stop_listener_btn.configure(state="disabled")
        self.listener_status.configure(text="Status: stopped")

    def _on_close(self):
        if self.listener_client and self.listener_loop:
            asyncio.run_coroutine_threadsafe(self.listener_client.disconnect(), self.listener_loop)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
