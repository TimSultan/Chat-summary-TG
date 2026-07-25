# Gamification redesign — analysis & proposal

**Drafted:** 2026-07-25
**Status:** proposal only. Nothing here is implemented.
**Scope:** covers the XP / coin / level / badge system in `stats.py` and its `/stat`,
`/top`, `/badge`, `/weekwinner` surfaces in `bot_listener.py`.

Baseline it was written against: `stats.py:83-151` (XP constants, `XP_LEVELS`, badge
tiers), `stats.py:213` (`coins_for_xp`), `README.md:256-367` (documented behavior).

---

## 1. Analysis: what's structurally wrong right now

| # | Finding | Why it matters |
|---|---|---|
| 1 | **Coins aren't real** — `coins_for_xp()` (`stats.py:213`) is `xp // 10`, derived, not stored | Coins carry *zero* information beyond XP. The same number is displayed twice. Nothing can be spent until there's an actual balance ledger. This is the blocker for everything in §2. |
| 2 | **Zero sinks** | Healthy bot economies remove 20–30% of earned currency. This one removes 0%. Balances grow forever → the number stops meaning anything within a few months. |
| 3 | **Levels are AND-gated on XP *and* figurines** (`stats.py:91-99`) | A chatty non-painter is frozen at 🩶 forever. A prolific painter who lurks is also frozen. Both tracks stall each other — the most common member has *no* moving progress bar. |
| 4 | **7 levels across 50k XP** | Far too coarse. A member sits on one level for months. Progression research is unanimous: frequent small steps early, big named tiers later. |
| 5 | **`/stat` deliberately hides next-level requirements** (`README.md:275`) | Kills the goal-gradient effect — visible near-goal progress is the strongest single motivator in leveling systems. Worth revisiting; compromise proposed in §3. |
| 6 | **Everything is all-time and permanent; nothing resets** | New members face an unclimbable wall against 2-year veterans. `/top week` partly helps, but level / coins / badges are all cumulative. |
| 7 | **Pure PBL (points/badges/leaderboards)** | The documented "PBL trap" — spikes then burns out, because it delivers no autonomy, no meaningful choice, no relatedness. |
| 8 | **No cooldown or daily XP cap** | Word-based scoring stops `"ок"` farming, but not long-message farming. Standard practice is a 30–60s cooldown plus a daily ceiling. |
| 9 | **Rewards quantity, never usefulness** | Nothing rewards answering a newbie's question — the behavior that actually keeps a hobby chat alive. |
| 10 | **Overjustification risk on `#япокрасил`** | Painting is intrinsically motivated. A fixed 200 XP price tag per mini can crowd out intrinsic enjoyment — rewarding a hobby is the classic case where extrinsic rewards backfire. |

---

## 2. What coins can be spent on

**Rules first:** coins must never buy XP, levels, or rank (pay-to-win destroys
leaderboard trust), and every item should cost nothing real.

### A. Identity & cosmetics (cheap to build, high demand)
1. **Custom title** in `/stat` — rented for 30 days, so it's a *recurring* sink, not one-time
2. **Custom badge emoji/frame** — buy the right to design one badge for yourself
3. **Decorated name in `/top`** — brackets, prefix symbol, position highlight
4. **Витрина** — pin 3 favorite works to the top of `/stat` instead of newest-first
5. **Custom level-up announcement text** — those are already posted (`README.md:290`)

### B. Attention & spotlight (highest perceived value)
6. **Работа дня/недели** — pay to have the bot re-post your mini into the chat with a highlight
7. **Featured slot in the weekly digest** — limited to 1–2 per week, so price floats with demand
8. **Buy a pin** — bot pins your message for 24h (needs bot admin rights)

### C. Bot services (the LLM is already wired up)
9. **AI critique of your mini** — the highest-value sellable item; genuinely useful in a painting chat
10. **Buy a roast** — of yourself, or gift one to a friend (`roast.py`)
11. **Custom joke on demand** (`joke.py`)
12. **Extra `/summary` quota** — converts real LLM cost into a sink
13. **Personal monthly stats report** — DM'd, richer than `/stat`

### D. Social & gifting (turns coins into a real economy)
14. **Transfer coins** to another member — enables organic tipping
15. **`/спасибо @user`** — moves 5 coins from you to them; the *thanks* becomes costly and therefore meaningful
16. **Buy a badge to award someone else** — expensive; peer recognition that isn't admin-gated
17. **Sponsor the weekly prize pool** — with public credit

### E. Progression utility (keep it off the XP track)
18. **Streak freeze** — protects a streak for one missed day. Duolingo's most-purchased item ever. Streaks are already tracked.
19. **Double-coin weekend** — personal, 48h, expensive
20. **Weekly contest entry buy-in** → winner takes the pot (a real sink with a house cut)
21. **Raffle tickets** — recurring; a house edge burns 20–30% automatically

### F. The meta-sink
22. **Меценат leaderboard** — rank by coins *burned*, not held. Makes spending itself a status game; the most effective anti-hoarding mechanic there is. Add a 🎖 badge tier for lifetime spend.

### G. Real-world (only with a budget)
23. Raffle for paints/brushes, partner-shop discount codes, a commission slot from a top painter in the chat.

**Implementation note:** items 1–22 all need one thing first — a real `coins_balance` +
`coins_spent` ledger with an append-only transaction log, replacing the derived
`coins_for_xp()`. Build that before anything else in this section.

---

## 3. A better leveling system

### Split one ladder into three tracks
The AND-gate is the core problem. Replace it with three independent tracks so **every
member always has one bar moving**:

| Track | Source | Character |
|---|---|---|
| 🗣 **Уровень чата** | XP from messages / replies / active days | Fast, always moving, ~40 levels |
| 🎨 **Ранг художника** | Figurines + peer votes + contest results | Slow, prestigious, ~10 ranks, the real hobby ladder |
| 🤝 **Репутация** | *Only* from other members (tips, thanks, contest wins) | Non-farmable, the anti-grind track |

The current 7 names (Ученик грунта → Легенда покраса) map cleanly onto the **painter
rank** — that's what they were always describing. The chat level gets its own naming.

### Fix the curve
- **~40 chat levels**, not 7. Roughly `xp_for(n) = 100 × n^1.6` → level 2 in a day, level 10 in a month, level 40 as a multi-year flex.
- **Named tiers every 5 levels** so prestige names survive while the numbers move often.
- **Show a progress bar.** Compromise on the hide-the-target rule: show `▓▓▓▓▓░░░░░ 52%` without revealing the numeric threshold. Progress stays visible, mystery stays intact.

### Add seasons (this fixes the newcomer wall)
- **3-month seasons.** Seasonal XP resets; all-time XP and painter rank never do.
- Public **seasonal leaderboard** = fresh drama and a real chance for new members; permanent **all-time level** = veterans keep everything.
- This is the proven layering: private daily progression + public seasonal rank. Neither alone retains well.
- Season end: top-3 get a permanent dated badge (`🏆 Сезон 3 — 2 место`). These accumulate into a visible history.

### Add prestige past max
At chat level 40, opt into a reset for a permanent ⭐ marker (⭐×2, ⭐×3). Costs nothing,
gives the most engaged members a reason to continue instead of hitting a dead end.

### Anti-abuse (add alongside)
- 45s XP cooldown per user
- Daily XP ceiling (~150–200)
- No XP for near-duplicate messages
- Cap XP from any single conversation thread

---

## 4. What levels unlock

**Telegram constraint that shapes this:** unlike Discord, a normal Telegram group has no
role-gated channels. Every unlock must be **bot-mediated**. That's an advantage — bot
features are fully controllable.

| Level | Unlock | Type |
|---|---|---|
| 2 | Coin earning switches on; `/stat` for yourself | Anti-spam gate |
| 3 | `/joke`, `/roast` on yourself | Feature |
| 5 | Custom title slot (rentable in shop); can nominate others for the weekly contest | Cosmetic + social |
| 7 | `/спасибо` tips carry double weight; **vote in the weekly contest** | Influence |
| 10 | AI critique of your minis; higher `/summary` quota | Feature |
| 12 | Shop tier 2 unlocks (spotlight items) | Economy |
| 15 | **Propose next week's challenge theme** | Autonomy |
| 20 | Permanent slot in a pinned Зал славы; can **create** a custom badge (today admin-only, `README.md:327`) | Status + autonomy |
| 25 | Seat on the совет that picks contest themes and judges | Purpose |
| 30 | Can award one badge per month to anyone | Peer recognition |
| 40 | Prestige available; permanent ⭐ | Prestige |

**Design principle from the research:** cosmetic unlocks are the weakest motivator.
**Permission and influence unlocks are the strongest** — "you can now decide what the chat
does this week" beats any colored name. Front-load features early (cheap wins keep
newcomers), back-load influence.

⚠️ **Keep moderation powers admin-only.** Level 25 should pick contest themes, not delete
messages or hand out `/weekwinner`. Level ≠ trust.

---

## 5. Mechanics worth adding beyond coins / levels / unlocks

23. **Daily/weekly quests** — "post 1 work", "reply to 3 people", "help a newcomer". Rotate; use variable rewards so they don't become a chore.
24. **Team seasons** — split the chat into 2–3 teams for a month with a collective goal. Relatedness is the strongest retention factor in the research, and there is currently none.
25. **Separate peer-recognition currency** — every member gets 5 non-purchasable "respect" tokens per week that can only be *given away*, expiring weekly (the Bonusly model). Fixes finding #9 directly.
26. **Newcomer onboarding streak** — three easy wins in the first 24h (first message, first reply, first work). First-session wins dominate long-term retention.
27. **Combo/momentum bonus** — a conversation that draws 5+ people replying within 10 min gives everyone a small bonus. Rewards conversation, not monologue.
28. **Anniversary badges** — 1 year in chat, etc. Free, and well-liked.

### Ethical guardrails
- **Leaderboard opt-out.** Not everyone wants to be ranked.
- **Reconsider the procrastinator digest** (`stats.py:774`) — publicly naming inactive members is a shame mechanic. Effective short-term, corrosive long-term. A DM nudge achieves the same thing.
- **On the overjustification risk (finding #10):** keep `#япокрасил` rewards *variable and social* rather than a flat 200 XP price tag — reward the sharing, the reactions, and contest recognition more than the act of painting itself. Fixed per-unit payment for a hobby is exactly the shape that crowds out intrinsic motivation.

---

## 6. Suggested build order

1. **Real coin ledger** — replace derived `coins_for_xp()`. Unblocks everything.
2. **Cooldown + daily cap** — before the economy has real value, not after.
3. **Split the three tracks; ungate levels from figurines.** Biggest single engagement win.
4. **Shop v1** — 5 items only: custom title, streak freeze, AI critique, coin transfer, roast.
5. **Seasons + seasonal leaderboard.**
6. **Level unlocks**, starting with the influence ones (theme proposal, contest voting).
7. Peer-recognition tokens, quests, teams.

**Known migration risk:** coins are currently derived rather than stored, and the last
observed level is persisted per chat to drive one-time promotion announcements
(`LEVEL_STATE_VERSION` in `stats.py`). Splitting one level track into three changes what
"promotion" means, so the persisted level state needs a migration path — otherwise the
rollout re-announces promotions for every existing member.

---

## Sources

- [Discord Server Economy System — BuildMyDiscord](https://buildmydiscord.com/en/blog/discord-server-economy-system-create-engaging-virtual-currency-and-rewards-in-20)
- [Leveling Systems & League Rank — Yu-kai Chou](https://yukaichou.com/advanced-gamification/leveling-system-gt85-and-league-rank-gt101/)
- [Motivation Traps in Reward-Based Gamification — Yu-kai Chou](https://yukaichou.com/gamification-study/motivation-traps-rewardbased-gamification-campaigns/)
- [7 Gameful Design Secrets for Lasting Engagement — Gamification Hub](https://www.gamificationhub.org/what-are-some-best-practices-for-implementing-gameful-design-in-a-way-that-is-both-effective-and-sustainable-over-timeeot-id/)
- [Overjustification effect — Wikipedia](https://en.wikipedia.org/wiki/Overjustification_effect)
- [Gamification and intrinsic motivation meta-analysis — Springer](https://link.springer.com/article/10.1007/s11423-023-10337-7)
- [Level Curves: The Art of Designing In-Game Progression](https://www.designthegame.com/learning/courses/course/fundamentals-level-curve-design/level-curves-art-designing-game-progression)
- [Gamification on Discord: Engaging Community Members](https://rewardtheworld.net/gamification-on-discord-engaging-community-members/)
- [MEE6 Economy plugin docs](https://wiki.mee6.xyz/en/plugins/economy)
