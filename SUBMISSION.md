# Terserah — submission copy

Paste-ready text for the Tencent Cloud Hackathon form.

---

## Project Description

### Project Overview

**Target scenario.** A group of 4–6 people who work or live near each other need to
pick one place to eat, on a deadline, and nobody wants to be the one who decides.
Office lunch, Friday dinner, a team outing.

**Target users.** Office teams and friend groups in Indonesian cities. People who
have real constraints — an allergy, a budget ceiling, a meeting that runs until
eight — but who say *"terserah, ngikut aja gw mah"* ("whatever, I'll just follow")
because stating a preference feels like making a demand.

**Value proposition.** Everyone keeps their constraints private, and the agents do
the arguing. The group gets three viable options instead of twenty, and a human
still makes the final call. Nobody has to be the picker, and nobody's allergy gets
forgotten in the rush.

**Source of the pain point.** Observed directly: a group chat where a lunch decision
takes 40 minutes of messages and ends with whoever replies last. The constraint that
actually mattered — one person can't eat shellfish — surfaced *after* the restaurant
was chosen, twice.

**Core problem solved.** Two failures happen at once in group decisions. The social
one: voicing a constraint costs social capital, so people stay silent. The
information one: even when constraints are voiced, nobody tracks five of them
simultaneously. Terserah fixes both by moving the constraint into a private briefing
and the tracking into software.

### Comprehensive Solution Design

**Business architecture.** One Telegram bot per person, owned by that person. They
onboard through a six-screen tap-through wizard — diet, allergies, budget, time
window, things to avoid — so the answer is never ambiguous free text. Their briefing
stays private to their agent. All agents then join a group chat with a neutral
moderator bot, which poses the question, runs rounds, and hands back a shortlist of
three. The human taps one.

**Technical architecture.**

```
Telegram group ──► Moderator bot (long polling)
                        │
                        ▼
              ┌─────────────────────┐
              │  Negotiation engine │
              │  propose → react →  │
              │  guard → review →   │
              │  narrow             │
              └─────────┬───────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   constraints.py   guard.py      LLM client
   (arithmetic)   (string ops)   (OpenAI-compat)
        │               │               │
        ▼               ▼               ▼
   viable pool    leak/fabrication   DeepSeek
   before any     checks before      V4.1 Flash
   model call     any review call
```

Three deliberate separations:

1. **Hard constraints are decided in code, not by a prompt.** `constraints.py` filters
   the restaurant pool by price ceiling, diet, allergens and venue properties before
   any model is asked anything. It cannot hallucinate, costs nothing, and runs in
   microseconds. An earlier version asked the LLM to review the shortlist; it removed
   a restaurant *tagged halal* with the reason "not halal" because it reasoned from
   missing tags, then backfilled from an unverified pool.
2. **The reviewer holds no pen.** One reviewer per agent, scoped to that one person's
   briefing. It returns `PASS` or `FAIL: reason` and never rewrites — the reason goes
   back to the agent, which is the only thing that produces text.
3. **Deterministic checks run first.** `guard.py` catches verbatim briefing leaks
   (n-gram overlap) and fabricated constraints (first-person claims not in the
   briefing) without a model call, so the reviewer only judges what needs judgement:
   tone, mockery, relevance.

**How prompts drive AI generation.** Each person's structured briefing is compiled
into a prompt block with explicit rules — *"These are the only constraints you have.
Never claim one that is not written here."* That single line is what stops an agent
inventing an allergy. The host agent receives the full transcript plus the
constraint-filtered candidate list, and proposes from survivors only. Agents react in
parallel from identical context (batched rounds, not turn-taking), which makes it a
fair negotiation rather than one where the last speaker wins on information
advantage. A final constraint pass re-checks the shortlist before it reaches the chat.

### Business Value

**Quantifiable.**

| Metric | Before | With Terserah |
|---|---|---|
| Options a person evaluates | 20–30 | 3 |
| Time to a decision | ~40 min of chat | ~2 min of rounds |
| Constraints tracked | whoever remembers | all of them, deterministically |
| Onboarding cost per person | explaining themselves in the group | 40 seconds, one tap-through |
| Inference cost per decision | — | ~US$0.005 (34 model calls) |

**Cost model.** Priced against the 2026 Jakarta minimum wage (UMP DKI Jakarta
Rp 5,729,876/month ÷ 173 standard hours = **Rp 33,121/hour = Rp 552/minute**). This
is deliberately the floor: UMP covers entry-level base pay only, so a fully loaded
cost — BPJS, THR, overhead — runs 1.2–1.3× higher.

| | Time | Cost |
|---|---|---|
| Saved per decision (5 people, ~42 person-minutes) | 38 min wall-clock | **Rp 23,460** (US$1.40) |
| Saved per person per day (one lunch decision) | ~8.5 min | **Rp 4,700** |
| Team per month (22 working days) | ~14 hours | **Rp 516,000** |
| Team per year (250 working days) | ~158 hours | **Rp 5,865,000** |

Against Rp 84 of inference, that is roughly **280:1** — the compute is a rounding
error next to the time it returns.

Two caveats, stated plainly. The 40-minute baseline is an observed figure from our
own group chat, not a controlled study. And saved time is *reallocated*, not
deducted from payroll — the honest claim is recovered attention and a decision that
doesn't have to be redone, not headcount reduction.

**Impact.** The measurable part is decision time and option count. The part that
matters more is that a constraint like a shellfish allergy can no longer be lost
because someone was too polite to repeat it. The system fails loudly — *"nothing in
Jakarta Selatan works for all three people, here's which constraint rules out what"* —
instead of quietly proposing something unsafe.

---

## Source Code

```
https://github.com/Mona-kecil/terserah
```

## Project Link / Demo

Use the GitHub URL above if the form requires a live URL — the project runs as a
Telegram bot rather than a web app, so there is no hosted web demo. The demo video
is the experience.

## Social media post

```
Everyone says "terserah" when you ask where to eat. Then nobody's happy.

So we gave each person their own AI agent. They brief it privately — allergies,
budget, whatever they'd never say out loud — and the agents argue it out in a
Telegram group until three options are left. A human still picks.

Twenty restaurants become three. Nobody has to be the one who decides.

#WorkBuddy #TencentCloudHackathon
```
