# Demo script

Two and a half minutes, screen-recorded. No editing needed.

## Before you hit record

```bash
cd "/Users/monakecil/WorkBuddy AI/Terserah/telegram-personas"

# 1. everything green?
./.venv/bin/python ready.py --only budi,mawar,joko

# 2. start the bots in fast mode (3 turns, ~2 min per round)
./.venv/bin/python main.py --only budi,mawar,joko --fast
```

Fast mode matters. At default settings a round takes five minutes and your
audience watches nothing happen. `--fast` is three turns at medium reasoning.

Also:

- Close other tabs. Your Telegram sidebar shows your personal chats.
- Put the phone/desktop app in a narrow window so message bubbles stay readable.
- Have the terminal visible next to it if you plan to show the log.
- Clear the group of old messages, or scroll so the demo starts clean.

## Shot list

**0:00 – 0:20 · The problem**

Show an empty group chat. Say it, don't type it:

> Ask five people where to eat and you get five shrugs. *Terserah.* Not because
> nobody cares — because nobody wants to be the one who picks. Meanwhile
> everyone has a constraint they keep quiet: an allergy, a budget, a meeting
> that runs till eight.

**0:20 – 0:50 · Private briefing**

Open a DM with `@budi_agent_bot`, send `/start`, tap through the wizard.

> Each person gets their own bot. Budi tells it he's halal, allergic to
> shellfish, and won't spend past 150k. Nobody else sees this — not the other
> agents, not me.

Tap **Looks right**. The bot confirms. That's the whole setup.

**0:50 – 2:10 · The negotiation**

Back in the group:

```
/ask@terserah_host_bot makan malam --area jakbar
```

> Now I ask the group. The host puts options on the table, and every agent
> reacts — from its own briefing only.

Let it run. Point at the disagreement when it appears:

> Watch what happens: Budi's agent rules something out on shellfish, Mawar's
> on dairy, Joko's on peanuts. They're not agreeing — they're negotiating. And
> each one only knows its own person's constraints.

When the shortlist lands:

> Three left, every one of which clears everybody's hard constraints. The
> agents narrowed twenty places to three. I still pick.

Tap one. The decision posts.

**2:10 – 2:30 · What was actually happening**

Cut to the terminal, or show `gm.py`:

```bash
./.venv/bin/python gm.py sessions
./.venv/bin/python gm.py show 1
```

> Behind it: every reply passed a deterministic guard for leaked briefings and
> fabricated allergies, then a reviewer scoped to that one person. A filter in
> code — not a prompt — decided what was even allowed on the table. And the
> whole transcript is recorded, so I can audit any call I disagreed with.

## If something goes wrong on camera

- **Bot silent** → the process died. Restart it, don't debug live.
- **"Nothing works for all N people"** → the pool is thin for that area. That's
  a real, honest answer — say so. It's the product being correct, not broken.
- **A round takes too long** → you're not in `--fast`. Cut and restart.

## Recording on macOS

`Cmd + Shift + 5` → Record Selected Portion → pick the window → Record.
QuickTime also works and gives you a cleaner trim.

## If you want an AI teaser instead

There's a `VideoGen` tool available, but be aware: it generates abstract AI
footage from a text prompt, not a screen recording of your product, and each
5-second clip costs roughly 50–100 credits. It would make a title card, not a
demo. The screenshots you already have are stronger evidence.
