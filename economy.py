"""Coin ledger and shop for the chat's gamification (see GAMIFICATION_PLAN.md).

Before this module, "coins" were not stored anywhere: stats.coins_for_xp derived them as
`xp // 10` on every render, which made them a second display of XP rather than a currency
-- nothing could ever be spent, because there was nothing to subtract from.

The ledger deliberately keeps deriving the EARNED half instead of persisting a running
balance, and stores only what cannot be derived:

    balance = stats.coins_for_xp(xp) + bonus - spent

That falls out of the existing immutable day files rather than duplicating them, so there
is no double source of truth for "how much has this person earned", no periodic crediting
job that can double-credit after a restart, and -- the reason it was chosen here -- no
migration at all: on the first run every member's `spent` is 0, so their opening balance
is exactly the number /stat has been showing them all along. Grandfathering is the
default behavior of the formula, not a one-off script that has to be gotten right once.

XP is very nearly monotonic, but not perfectly: /deletepokras removes a figurine and its
200 XP (20 coins). Someone who had already spent those coins would compute a negative
balance, so balance() clamps at zero -- see its docstring.

`log` is a rolling audit trail, NOT the source of truth. The per-user totals are
authoritative; the log is capped (LOG_LIMIT) so a busy chat can't grow this file forever.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import stats
from app_time import now as app_now

ECONOMY_STORE_VERSION = 1
# Rolling audit trail only -- per-user totals above it are what balances are computed
# from, so trimming this can never change anybody's balance.
LOG_LIMIT = 1_000
FIGURINE_COIN_REWARD = 500

# Member-to-member transfers were removed. `received` is still read by balance() and
# reputation_for() so that any ledger written while they existed keeps computing exactly
# the same numbers; nothing can add to it any more.

# A 30-day rented title, priced so it stays a recurring decision rather than a one-off
# purchase. See the price note on SHOP_ITEMS.
TITLE_DAYS = 30
TITLE_MAX_CHARS = 32


@dataclass(frozen=True)
class ShopItem:
    code: str
    name: str
    price: int
    description: str
    # Minimum hours between two purchases of this item by the same person. Grandfathered
    # balances (the plan's chosen migration) mean the chat's most active members open
    # with four figures on day one, enough to buy the whole catalog at once; a per-item
    # cooldown blunts that without confiscating anything they earned.
    cooldown_hours: int = 0
    # Whether buying it needs an argument (a title string, a target, ...).
    argument_hint: str = ""


# Prices are calibrated against the chat's REAL earn rate, measured off the cached
# transcripts rather than guessed: over a 34-day window the most active members earned
# 60-233 coins/week, the p90 member ~55/week, and the median member ~3/week. So these
# land at "a regular can afford something meaningful every week or two, an inactive
# lurker cannot". The median member being effectively priced out is a real property of
# this curve and is called out in the README -- it is a consequence of coins tracking XP,
# not of the prices.
SHOP_ITEMS = (
    ShopItem(
        "title", "Свой титул", 400,
        f"Свой титул в /stat на {TITLE_DAYS} дней",
        cooldown_hours=0,
        argument_hint="<текст титула>",
    ),
)

# The work critique and the streak freeze were removed from the catalogue. Their
# delivery code (see bot_listener._deliver_shop_item) and the freeze machinery below are
# deliberately LEFT IN PLACE rather than deleted -- re-listing either is adding one
# ShopItem back, the same "disabled, not removed" convention the XP cooldown already
# follows in this codebase.
#
# Consequence worth knowing: transfers used to burn TRANSFER_BURN_PERCENT of every gift
# and were the economy's only always-on sink. With transfers gone and one rentable item
# left, the only thing draining coins is a 400-coin title every 30 days, while an active
# member earns ~1,000 a month. Balances will grow. See the README note.


def find_item(code: str) -> ShopItem | None:
    needle = (code or "").strip().lower()
    return next((item for item in SHOP_ITEMS if item.code == needle), None)


def _economy_path(entry: str) -> Path:
    return stats._stats_dir() / f"{stats._cache_key(entry)}_economy.json"


def _empty() -> dict:
    return {"version": ECONOMY_STORE_VERSION, "users": {}, "log": []}


def _load(entry: str) -> dict:
    path = _economy_path(entry)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("users", {})
    data.setdefault("log", [])
    return data


def _save(entry: str, data: dict) -> None:
    data["version"] = ECONOMY_STORE_VERSION
    if len(data.get("log", [])) > LOG_LIMIT:
        data["log"] = data["log"][-LOG_LIMIT:]
    stats._write_json_atomic(_economy_path(entry), data)


def _record(data: dict, user_id) -> dict:
    """The mutable per-user totals. `burned` is tracked separately from `spent` so a
    "who has burned the most" standing (the plan's anti-hoarding meta-sink) can be built
    later without replaying the capped log."""
    return data["users"].setdefault(
        str(user_id),
        {"spent": 0, "bonus": 0, "received": 0, "burned": 0, "effects": {}},
    )


def _append_log(data: dict, user_id, delta: int, reason: str, ref: str = "") -> None:
    data["log"].append(
        {
            "ts": app_now().isoformat(),
            "user_id": str(user_id),
            "delta": delta,
            "reason": reason,
            "ref": ref,
        }
    )


def balance(entry: str, user_id, xp: int) -> int:
    """Spendable coins for one member.

    Clamped at zero because the earned half is derived from live XP, which can fall when
    an administrator runs /deletepokras (one figurine = 200 XP = 20 coins). Someone who
    had already spent those coins would otherwise land on a negative balance and be
    unable to earn their way out at the same rate they were docked. Clamping means such a
    deletion can, at most, forgive up to 20 already-spent coins -- deliberately preferred
    over showing a member a debt they cannot see the cause of."""
    data = _load(entry)
    return _balance_from(data, user_id, xp)


def _balance_from(data: dict, user_id, xp: int) -> int:
    record = data["users"].get(str(user_id)) or {}
    earned = stats.coins_for_xp(xp)
    return max(
        0,
        earned + record.get("bonus", 0) + record.get("received", 0) - record.get("spent", 0),
    )


def spend(entry: str, user_id, xp: int, amount: int, reason: str, ref: str = "") -> tuple[bool, int]:
    """Debit `amount`. Returns (succeeded, resulting balance); a refused debit leaves the
    ledger untouched and reports the unchanged balance, so callers can render "you need N
    more" without a second read."""
    if amount <= 0:
        return False, balance(entry, user_id, xp)
    data = _load(entry)
    current = _balance_from(data, user_id, xp)
    if current < amount:
        return False, current
    record = _record(data, user_id)
    record["spent"] = record.get("spent", 0) + amount
    _append_log(data, user_id, -amount, reason, ref)
    _save(entry, data)
    return True, current - amount


def refund(entry: str, user_id, xp: int, amount: int, reason: str) -> int:
    """Undo a debit whose effect could not be delivered (a failed LLM call, a missing
    work to critique). Reduces `spent` rather than adding a bonus, so a purchase that
    never happened leaves no trace in lifetime-spend standings."""
    if amount <= 0:
        return balance(entry, user_id, xp)
    data = _load(entry)
    record = _record(data, user_id)
    record["spent"] = max(0, record.get("spent", 0) - amount)
    _append_log(data, user_id, amount, f"refund:{reason}")
    _save(entry, data)
    return _balance_from(data, user_id, xp)


def refund_once(entry: str, user_id, xp: int, amount: int, reason: str) -> bool:
    """Refund one named migration only once, atomically with its economy record."""
    if amount <= 0:
        return False
    data = _load(entry)
    record = _record(data, user_id)
    migrations = _effects(record).setdefault("refunds", {})
    if migrations.get(reason):
        return False
    record["spent"] = max(0, record.get("spent", 0) - amount)
    migrations[reason] = True
    _append_log(data, user_id, amount, f"refund:{reason}")
    _save(entry, data)
    return True


def grant_once(entry: str, user_id, amount: int, reason: str) -> bool:
    """Grant one named migration only once, atomically with its economy record.

    The `bonus` sibling of refund_once, for a payout that must land in full. refund_once
    subtracts from `spent`, which is clamped at zero, so it silently pays out LESS than
    `amount` to anyone who has not spent that much yet -- fine for undoing a specific
    purchase, wrong for "everybody in this group gets N coins"."""
    if amount <= 0:
        return False
    data = _load(entry)
    record = _record(data, user_id)
    migrations = _effects(record).setdefault("grants", {})
    if migrations.get(reason):
        return False
    record["bonus"] = record.get("bonus", 0) + int(amount)
    migrations[reason] = True
    _append_log(data, user_id, int(amount), f"grant:{reason}")
    _save(entry, data)
    return True


def grant(entry: str, user_id, amount: int, reason: str) -> None:
    """Credit coins that did not come from XP (an administrator award, a contest prize)."""
    data = _load(entry)
    record = _record(data, user_id)
    record["bonus"] = record.get("bonus", 0) + int(amount)
    _append_log(data, user_id, int(amount), reason)
    _save(entry, data)


def settle_wager(
    entry: str, user_id, xp: int, stake: int, won: bool, reason: str,
    *, draw: bool = False,
) -> tuple[bool, int]:
    """Settle one coin-only wager in one ledger write.

    A win returns twice the stake (the staked coins plus the same amount in winnings).
    A draw returns the stake unchanged. Keeping debit and payout in the same save means a
    restart can never take a bet without paying a completed win.
    """
    if stake <= 0:
        return False, balance(entry, user_id, xp)
    data = _load(entry)
    current = _balance_from(data, user_id, xp)
    if current < stake:
        return False, current
    record = _record(data, user_id)
    record["spent"] = record.get("spent", 0) + int(stake)
    _append_log(data, user_id, -int(stake), f"wager:{reason}")
    payout = int(stake) * (2 if won else 1 if draw else 0)
    if payout:
        record["bonus"] = record.get("bonus", 0) + payout
        _append_log(data, user_id, payout, f"wager_payout:{reason}")
    _save(entry, data)
    return True, _balance_from(data, user_id, xp)


# --- purchase effects -------------------------------------------------------------
#
# Effects live on the same per-user record as the totals so one atomic write covers both
# the debit and the thing it bought -- a crash between them would otherwise take the
# coins without delivering the effect.


def _effects(record: dict) -> dict:
    return record.setdefault("effects", {})


# --- daily bonus --------------------------------------------------------------------
#
# The one coin faucet that asks for nothing but showing up. It lives here rather than in
# pets.py on purpose: most members do not own a pet, chat activity is their only income,
# and a reward for turning up should not be gated behind an arena they never entered.
#
# The streak table is the whole design. A flat daily payout is worth claiming whenever you
# remember; a rising one is worth NOT breaking, which is the habit actually being bought.
# Day 7 onward stays at the top value -- an unbounded streak would quietly become the
# largest faucet in the game for whoever automated it best.

DAILY_BONUS_EFFECT_KEY = "daily_bonus"
DAILY_BONUS_BY_STREAK = (25, 35, 45, 60, 75, 90, 100)


def daily_bonus_amount(streak: int) -> int:
    """Payout for the Nth consecutive day, counting from 1 and flat after the table ends."""
    index = max(1, int(streak or 1)) - 1
    return DAILY_BONUS_BY_STREAK[min(index, len(DAILY_BONUS_BY_STREAK) - 1)]


def _daily_bonus_effect(data: dict, user_id) -> dict:
    effect = ((data["users"].get(str(user_id)) or {}).get("effects", {})
              .get(DAILY_BONUS_EFFECT_KEY, {}))
    return effect if isinstance(effect, dict) else {}


def _next_streak(effect: dict, today: date) -> int:
    """What today's claim would be worth, in consecutive days.

    Yesterday continues the run; anything older starts over. A `last` in the FUTURE (a
    clock correction, a timezone change) is treated as already claimed by the caller, so
    it never lands here as a silently reset streak.
    """
    try:
        previous = date.fromisoformat(str(effect.get("last")))
    except (TypeError, ValueError):
        return 1
    if previous == today - timedelta(days=1):
        return max(1, int(effect.get("streak", 0) or 0)) + 1
    return 1


def daily_bonus_status(entry: str, user_id, today: date | None = None) -> dict:
    """Read-only view for the button: what is claimable now and what it is worth."""
    day = today or app_now().date()
    effect = _daily_bonus_effect(_load(entry), user_id)
    try:
        previous = date.fromisoformat(str(effect.get("last")))
    except (TypeError, ValueError):
        previous = None
    claimed_today = previous is not None and previous >= day
    streak = max(0, int(effect.get("streak", 0) or 0))
    next_streak = streak if claimed_today else _next_streak(effect, day)
    return {
        "can_claim": not claimed_today,
        "streak": streak if claimed_today else max(0, next_streak - 1),
        "next_streak": next_streak,
        "amount": daily_bonus_amount(next_streak),
        "tomorrow": daily_bonus_amount(next_streak + 1),
        "max_amount": DAILY_BONUS_BY_STREAK[-1],
        "streak_days": len(DAILY_BONUS_BY_STREAK),
        "last": effect.get("last"),
    }


def claim_daily_bonus(entry: str, user_id, today: date | None = None) -> tuple[bool, int, int]:
    """Credit today's bonus once. Returns (claimed, amount, streak).

    The checkpoint and the coins share one ledger write, so a crash cannot pay without
    recording the day -- the same guarantee settle_passive_income relies on below.
    """
    day = today or app_now().date()
    data = _load(entry)
    record = _record(data, user_id)
    effect = _effects(record).setdefault(DAILY_BONUS_EFFECT_KEY, {})
    try:
        previous = date.fromisoformat(str(effect.get("last")))
    except (TypeError, ValueError):
        previous = None
    if previous is not None and previous >= day:
        return False, 0, max(0, int(effect.get("streak", 0) or 0))
    streak = _next_streak(effect, day)
    amount = daily_bonus_amount(streak)
    effect["last"] = day.isoformat()
    effect["streak"] = streak
    record["bonus"] = record.get("bonus", 0) + amount
    _append_log(data, user_id, amount, "daily_bonus")
    _save(entry, data)
    return True, amount, streak


# --- daily chatter prize ------------------------------------------------------------
#
# Paid for YESTERDAY, never for today: a day's stats are only final once it is over, and
# a running total would let somebody watch the standings and pad them before midnight.
#
# Ranked on the XP that day earned, through the very same stats.day_xp_ranking the ЕПХ
# tree's morning digest uses, so the two can never disagree about who moved the needle
# yesterday. That means a long message counts for more than a short one, and a
# #япокрасил post counts heavily -- the same trade the tree already makes, rather than a
# second, competing definition of "active" living in the ledger.

DAILY_CHATTER_PRIZES = (200, 100, 50)


def daily_chatter_prizes(entry: str, day: date, words_per_point: float) -> list[dict]:
    """Pay the top three earners of `day` once. Returns only newly paid rows.

    `words_per_point` is a required argument for the same reason UserStats.xp takes one:
    it is calibrated per chat rather than being a universal constant, and guessing it here
    would silently reprice everybody's day. The caller has the Telethon client needed to
    resolve it (see stats.words_per_point); this module deliberately does not.

    Idempotent through grant_once on a reason that carries the date, so re-running for a
    day already paid is a no-op even if the ranking has since changed (a late-recorded day
    file, a backfill).
    """
    ranked = [
        row for row in stats.day_xp_ranking(entry, day, words_per_point) if row[2] > 0
    ]
    paid = []
    for place, (user_id, user, earned) in enumerate(ranked[:len(DAILY_CHATTER_PRIZES)]):
        amount = DAILY_CHATTER_PRIZES[place]
        if not grant_once(entry, user_id, amount, f"daily_chatter:{day.isoformat()}"):
            continue
        paid.append({
            "user_id": user_id,
            "username": user.username,
            "display_name": user.display_name,
            "xp": earned,
            "messages": user.messages,
            "place": place + 1,
            "amount": amount,
        })
    return paid


# --- farm passive income ----------------------------------------------------------


FARM_PASSIVE_EFFECT_KEY = "pet_farm_income"


def passive_income_status(entry: str, user_id, hourly_rate: int, storage_cap: int,
                          now: datetime | None = None) -> dict:
    """Preview collectable facility gold without changing the ledger.

    A missing checkpoint is intentionally worth zero: pets created before this feature
    begin at their first interaction rather than receiving retrospective income.
    """
    moment = now or app_now()
    data = _load(entry)
    effect = ((data["users"].get(str(user_id)) or {}).get("effects", {})
              .get(FARM_PASSIVE_EFFECT_KEY, {}))
    raw = effect.get("last_hour")
    if hourly_rate <= 0 or storage_cap <= 0 or not raw:
        next_hour = moment + timedelta(hours=1) if hourly_rate > 0 and storage_cap > 0 else moment
        return {"stored": 0, "hours": 0, "last_hour": raw, "next_hour": next_hour}
    try:
        previous = datetime.fromisoformat(raw)
        elapsed = max(0, int((moment - previous).total_seconds() // 3600))
    except (TypeError, ValueError):
        previous = moment
        elapsed = 0
    return {
        "stored": min(storage_cap, elapsed * hourly_rate), "hours": elapsed,
        "last_hour": raw, "next_hour": previous + timedelta(hours=elapsed + 1),
    }


def settle_passive_income(entry: str, user_id, hourly_rate: int, storage_cap: int,
                           now: datetime | None = None) -> dict:
    """Atomically credit completed facility hours and move their checkpoint.

    The checkpoint and ``bonus`` share a ledger write, so restart/retry sees the already
    advanced hour and cannot credit it twice.  Overflow beyond storage is discarded.
    """
    moment = now or app_now()
    data = _load(entry)
    record = _record(data, user_id)
    effect = _effects(record).setdefault(FARM_PASSIVE_EFFECT_KEY, {})
    raw = effect.get("last_hour")
    if not raw:
        effect["last_hour"] = moment.isoformat()
        _save(entry, data)
        return {"credited": 0, "hours": 0, "initialized": True}
    try:
        previous = datetime.fromisoformat(raw)
        elapsed = max(0, int((moment - previous).total_seconds() // 3600))
    except (TypeError, ValueError):
        previous = moment
        elapsed = 0
    cap = max(0, int(storage_cap))
    produced = elapsed * max(0, int(hourly_rate))
    credit = min(cap, produced)
    # Keep an unfinished fraction when there was room; otherwise full storage would
    # leave a fractional tail that effectively grants post-cap production later.
    checkpoint = moment if produced > cap else previous + timedelta(hours=elapsed)
    effect["last_hour"] = checkpoint.isoformat()
    if credit:
        record["bonus"] = record.get("bonus", 0) + credit
        _append_log(data, user_id, credit, "pet:farm_passive_income", ref=str(elapsed))
    _save(entry, data)
    return {"credited": credit, "hours": elapsed, "initialized": False}


def last_purchased_at(entry: str, user_id, code: str) -> datetime | None:
    record = _load(entry)["users"].get(str(user_id)) or {}
    stamp = (record.get("effects") or {}).get("last_purchase", {}).get(code)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def cooldown_remaining(entry: str, user_id, item: ShopItem) -> timedelta | None:
    """How long until `item` can be bought again, or None when it is available now."""
    if not item.cooldown_hours:
        return None
    last = last_purchased_at(entry, user_id, item.code)
    if last is None:
        return None
    ready_at = last + timedelta(hours=item.cooldown_hours)
    remaining = ready_at - app_now()
    return remaining if remaining.total_seconds() > 0 else None


def _mark_purchased(data: dict, user_id, code: str) -> None:
    effects = _effects(_record(data, user_id))
    effects.setdefault("last_purchase", {})[code] = app_now().isoformat()


def set_title(entry: str, user_id, text: str) -> str:
    """Rent a custom /stat title for TITLE_DAYS. A second purchase replaces rather than
    extends, so the price always buys the same thing."""
    data = _load(entry)
    effects = _effects(_record(data, user_id))
    clean = " ".join((text or "").split())[:TITLE_MAX_CHARS]
    effects["title"] = {
        "text": clean,
        "expires_at": (app_now() + timedelta(days=TITLE_DAYS)).isoformat(),
    }
    _mark_purchased(data, user_id, "title")
    _save(entry, data)
    return clean


def active_title(entry: str, user_id) -> str | None:
    """The member's unexpired custom title, or None. Expiry is evaluated on read rather
    than swept by a job, so a lapsed title simply stops rendering."""
    record = _load(entry)["users"].get(str(user_id)) or {}
    title = (record.get("effects") or {}).get("title")
    if not title or not title.get("text"):
        return None
    try:
        expires_at = datetime.fromisoformat(title["expires_at"])
    except (KeyError, ValueError):
        return None
    return title["text"] if expires_at > app_now() else None


def add_streak_freeze(entry: str, user_id) -> int:
    """Bank one streak freeze. Returns how many the member now holds; they are consumed
    automatically by stats._current_streak, not spent explicitly, because the member
    cannot know in advance which day they are going to miss."""
    data = _load(entry)
    effects = _effects(_record(data, user_id))
    effects["freezes"] = int(effects.get("freezes", 0)) + 1
    _mark_purchased(data, user_id, "freeze")
    _save(entry, data)
    return effects["freezes"]


def streak_freezes(entry: str, user_id) -> int:
    record = _load(entry)["users"].get(str(user_id)) or {}
    return int((record.get("effects") or {}).get("freezes", 0))


def consume_streak_freeze(entry: str, user_id, day: date) -> bool:
    """Spend one banked freeze to cover `day`. Idempotent per day: covering the same gap
    twice (two /stat calls before the next message) must not cost two freezes."""
    data = _load(entry)
    effects = _effects(_record(data, user_id))
    used = effects.setdefault("frozen_days", [])
    key = day.isoformat()
    if key in used:
        return True
    if int(effects.get("freezes", 0)) <= 0:
        return False
    effects["freezes"] = int(effects["freezes"]) - 1
    used.append(key)
    _append_log(data, user_id, 0, "streak_freeze_used", key)
    _save(entry, data)
    return True


def frozen_days(entry: str, user_id) -> set:
    record = _load(entry)["users"].get(str(user_id)) or {}
    return set((record.get("effects") or {}).get("frozen_days", []))


# How far back a freeze will reach. A member who has been away for months should come
# back to a broken streak, not silently burn their banked freezes covering a gap they had
# already lost -- and this also bounds the walk below to a fixed number of iterations.
FREEZE_LOOKBACK_DAYS = 60


def apply_streak_freezes(entry: str, user_id, active_day_dates: set, today: date) -> set:
    """Spend banked freezes on the gaps that are actually breaking this streak, and
    return the full set of covered days for stats._current_streak.

    Walks backward exactly the way the streak does, so a freeze is only ever consumed for
    a gap that is genuinely load-bearing -- a member with no gap keeps their freezes, and
    a gap further back than a still-unbroken streak is never paid for. Today is never
    frozen: an unfinished day is not a missed one (see stats._current_streak).
    """
    covered = frozen_days(entry, user_id)
    if streak_freezes(entry, user_id) <= 0 and not covered:
        return covered
    day = today if today.isoformat() in active_day_dates else today - timedelta(days=1)
    for _ in range(FREEZE_LOOKBACK_DAYS):
        key = day.isoformat()
        if key in active_day_dates or key in covered:
            day -= timedelta(days=1)
            continue
        # A real gap. Cover it only while there is still a streak behind it worth saving.
        previous = day - timedelta(days=1)
        if previous.isoformat() not in active_day_dates:
            break
        if not consume_streak_freeze(entry, user_id, day):
            break
        covered.add(key)
        day -= timedelta(days=1)
    return covered


def purchase(entry: str, user_id, xp: int, item: ShopItem) -> tuple[bool, str, int]:
    """Debit for `item` after checking balance and cooldown.

    Returns (succeeded, refusal text, resulting balance). The caller delivers the item's
    actual effect and is responsible for calling refund() if delivery fails -- see
    bot_listener's shop handler."""
    remaining = cooldown_remaining(entry, user_id, item)
    if remaining is not None:
        hours = int(remaining.total_seconds() // 3600)
        minutes = int(remaining.total_seconds() % 3600 // 60)
        wait = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
        return False, f"Ещё рано -- «{item.name}» будет доступна через {wait}.", balance(entry, user_id, xp)
    ok, resulting = spend(entry, user_id, xp, item.price, f"buy:{item.code}")
    if not ok:
        return False, f"Нужно {item.price} монет, у тебя {resulting}.", resulting
    if item.cooldown_hours and item.code != "title":
        data = _load(entry)
        _mark_purchased(data, user_id, item.code)
        _save(entry, data)
    return True, "", resulting


def streak_freeze_lookup(entry: str):
    """The callback stats.resolve_stat_target takes as `frozen_days_for`.

    A factory rather than a plain function because that call site knows the user id but
    not the chat entry, and this module cannot be imported from stats (the dependency
    runs the other way -- see the module docstring)."""

    def lookup(user_id, active_day_dates, today):
        return apply_streak_freezes(entry, user_id, active_day_dates, today)

    return lookup


def reputation_for(entry: str, user_id, user=None) -> int:
    """This member's reputation (see stats.reputation_score).

    Lives here rather than in stats because the coins-received half is ledger data, and
    stats must not import this module (the dependency runs the other way).

    `user` is the UserStats the earned-badge levels are read off. It is optional because
    the ledger knows a user_id but has no way to load stats for it; callers that already
    hold a UserStats (every /stat path) pass it, and one that does not scores the
    peer-granted half alone rather than failing."""
    record = _load(entry)["users"].get(str(user_id)) or {}
    return stats.reputation_score(
        stats.weekly_wins_for_user(entry, user_id),
        len(stats.custom_badges_for_user(entry, user_id)),
        record.get("received", 0),
        stats.medal_levels(user, casino_winnings_for_user(entry, user_id)) if user is not None else 0,
    )


def casino_winnings_for_user(entry: str, user_id) -> int:
    """All-time net casino profit, kept beside the wager records in the shared ledger."""
    try:
        record = _load(entry)["users"].get(str(user_id)) or {}
        casino = _effects(record).get("casino") or {}
        return max(0, int(casino.get("winnings", 0) or 0))
    except (OSError, TypeError, ValueError):
        return 0


def stat_extras(entry: str, user_id, xp: int, user=None) -> dict:
    """Everything /stat needs from the economy, in the keyword shape format_stat takes.

    One helper so both call sites (listener.py and bot_listener.py) stay identical, and
    so a failure in the economy store degrades /stat to its pre-economy output instead of
    breaking the command outright."""
    try:
        return {
            "coins": balance(entry, user_id, xp),
            "reputation": reputation_for(entry, user_id, user),
            "custom_title": active_title(entry, user_id),
            "casino_winnings": casino_winnings_for_user(entry, user_id),
        }
    except (OSError, ValueError):
        return {}


def format_shop(entry: str, user_id, xp: int) -> str:
    """The /shop listing, annotated with what this member can actually afford right now."""
    current = balance(entry, user_id, xp)
    lines = [f"🏪 Магазин\n\n🪙 У тебя: {current:,}".replace(",", ".")]
    for item in SHOP_ITEMS:
        remaining = cooldown_remaining(entry, user_id, item)
        if remaining is not None:
            mark = "⏳"
        elif current >= item.price:
            mark = "✅"
        else:
            mark = "🔒"
        argument = f" {item.argument_hint}" if item.argument_hint else ""
        lines.append(
            f"\n{mark} <b>{item.name}</b> -- {item.price} монет\n"
            f"{item.description}\n"
            f"<code>/buy {item.code}{argument}</code>"
        )
    return "\n".join(lines)
