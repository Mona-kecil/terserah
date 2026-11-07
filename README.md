# Terserah

*Terserah* — Indonesian for "whatever."

Ask five people where to eat and you get five shrugs. Not because they don't
care, but because nobody wants to be the one who picks. Meanwhile everyone has
a constraint they keep to themselves: an allergy, a budget ceiling, a meeting
that runs until eight.

Terserah gives each person an agent. They brief it in private. The agents argue
in the group, a reviewer checks every word before it lands, and the system
narrows twenty restaurants down to three. A human still picks — just from three.

## The flow

```
You: /ask where should we eat tonight?

Terserah (system)   Turn 1 of 3 — on the table:
                    1. Warung Bu Tini — Padang, Kemang, ~45k [halal, budget, open_late]
                    2. Kafe Teduh — Western, Kemang, ~110k [vegetarian, quiet, outdoor]

Budi's agent        Kafe Teduh works for me. I'm vegetarian, so Warung Bu Tini
                    is risky unless they have a real meat-free dish.
Citra's agent       Either is fine but Kafe Teduh is quieter. I can't hear
                    myself think in a warung at 7pm.
Dewi's agent        Both under my 150k ceiling. Kafe Teduh, then.
...                          (each reply checked by that person's own reviewer)

Terserah            Turn 2 of 3 — on the table: ...
Terserah            Turn 3 of 3 — on the table: ...

Terserah            The agents are done. Three left:
                    1. Sate Pak Kumis (60k)
                    2. Gado-Gado Ibu Sri (35k)
                    3. Kafe Teduh (110k)
                    [ Tap one to decide ]
```

**Turn 1 is a random draw** from the pool, so the opening isn't biased by
anyone's phrasing. Later turns are chosen with the objections in hand. After
`max_turns`, the system agent ranks the top three and stops. It never picks for
you.

**Agents think in parallel, speak in order.** Each turn fires all five reaction
calls at once, then runs all five guard/review passes concurrently, then posts
the results one at a time.

Sequential posting is about *ordering*, not rate limits. These are five separate
bot tokens, and Telegram's throughput quota is per bot — each sends one message,
so nothing here is close to a limit. But concurrent sends land in a
non-deterministic order, and the transcript the agents argue from has to match
what people actually saw in the chat.

With `reasoning_effort: high`, a five-agent three-turn session takes about 100
seconds; nearly all of it is the model thinking, not orchestration.

There is no consensus vote. The loop runs `max_turns` (or stops early when no
viable option is left), then narrows. The agents never declare agreement.

## Four checks, and only one needs a model

The guiding rule: **anything decidable by arithmetic or string comparison is
decided in code.** A model is only asked what genuinely requires judgement.

**1. The host only proposes what can work.** Before any agent is asked anything,
the pool is filtered against every briefing — price against the lowest ceiling,
`pork` against halal, `seafood` against a shellfish allergy, `loud` against noise
avoidance. The system agent picks from the survivors only.

This was not always true, and it mattered. Turn 1 used to sample the whole pool,
so a group with a 60k ceiling and a shellfish allergy would spend a full turn
having five agents explain that they couldn't go to a 250k seafood restaurant.
Now every proposal is at least possible, and the debate is about preference
rather than impossibility. Measured effect: a session went from 3:48 to 2:32.

**2. The guard catches the mechanical failures.** `guard.py` runs before the
reviewer and needs no model:

- **verbatim leak** — a run of five consecutive words copied from the briefing
- **fabricated constraint** — a first-person claim (`"I'm allergic to…"`,
  `"I can't eat…"`) naming something the briefing never declared, with
  implications respected so a halal person saying "no pork" is fine

A hit skips the review call entirely and goes straight to a rewrite.

**3. The reviewer handles what is left.** One reviewer per agent, scoped to one
briefing. It judges tone, mockery, jabs, guilt-tripping, and relevance — the
things that need a model. It returns `PASS` or `FAIL: reason` and nothing else.

**4. The final shortlist is filtered again.** Same arithmetic as step 1, applied
to the three finalists. Belt and braces, because the cost of being wrong about an
allergy is a person getting sick, not an embarrassing suggestion.

### Why the reviewer holds no pen

It returns a verdict, never text. On a fail, the reason goes back to the *agent*,
which rewrites its own message. A reviewer that rewrote would smooth five voices
into one, and you'd lose the thing you're paying for.

### The rule that keeps the filter honest

**Exclude on evidence, never on absence.** A place that doesn't declare `halal`
might be fine; a place tagged `pork` definitely isn't. An earlier LLM-based check
got this exactly backwards — it removed a restaurant *tagged halal* with the
reason "not halal", because it reasoned from missing tags, then backfilled from an
unverified pool and put a 200k seafood place back into a 60k group.

Semantic inference is left to the agents, who turn out to be good at it. In a
live run, Budi's agent worked out that satay means peanut sauce and ruled it out
against his allergy — something no tag in the pool encodes.

## Privacy, and the god view

**Between players: private.** Each agent's prompt contains only its own briefing
plus the public transcript. No code path puts one person's briefing into
another's prompt.

**From you, the game master: nothing.** You run the server, so you see the
board. That's the role.

## Which API key

**Use DeepSeek.** `deepseek-flash` at roughly $0.15 per million input tokens and
$0.60 per million output. A full five-agent, three-turn session is about 34
calls — call it **half a US cent**. Even a hundred sessions is pocket change.

It also handles Bahasa Indonesia well, which matters when your friends brief
their agents in it.

Two caveats worth knowing: DeepSeek charges double during peak hours
(01:00–04:00 and 06:00–10:00 UTC, Mon–Fri, which is 08:00–11:00 and 13:00–17:00
WIB), and rates change — check the pricing page before you scale. Everything is
OpenAI-compatible, so switching providers is a two-line edit in `.env`.

Alternatives: **OpenRouter** if you want one key across many models,
**OpenAI** if you already have credits, or **Ollama** locally for free — slower
and weaker at Indonesian, but zero cost and nothing leaves your machine.

## Setup

**1. Get the key.** [platform.deepseek.com](https://platform.deepseek.com) →
API Keys → create. Copy it once; it isn't shown again.

**2. Create the bots.** Six: one moderator plus one per person. Message
[@BotFather](https://t.me/BotFather), `/newbot`, six times. Or automate:

```bash
./.venv/bin/pip install -r requirements-provision.txt
./.venv/bin/python provision.py --name "Budi's agent" --username terserah_budi_bot --dry-run
```

Read the warnings at the top of `provision.py` first — it drives your personal
account.

**3. Configure.**

```bash
cp .env.example .env
```

Six tokens plus `LLM_API_KEY`. Edit `config/personas.yaml` if your names differ,
and `data/restaurants.yaml` for your actual pool.

**4. Verify.**

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python main.py --check
```

Six `OK` lines and you're wired.

## First: does the key work?

Before anything else, prove the endpoint answers. One cheap request, plain-English
diagnosis:

```bash
./.venv/bin/python probe.py
```

```
Probing (one request, a few tokens)

  https://api.deepseek.com  model=deepseek-flash
    OK 1.2s  reply='OK'
```

If the base URL is a guess, let it try variants:

```bash
./.venv/bin/python probe.py --try https://api.sumopod.com,https://api.sumopod.com/v1
```

It prints the one that worked, ready to paste into `.env`. Wrong key, wrong path,
wrong model name, and unreachable host each get a different message, so you are
never guessing which of the three is broken.

## Testing — before real people are involved

This is the part worth doing properly. You can run the entire flow with no
Telegram and no API key:

```bash
./.venv/bin/python simulate.py --fake-llm
```

That prints a full three-turn conversation using a deterministic stand-in model.
It proves the plumbing: parallel reactions, the review gate, the shortlist, the
buttons. It costs nothing.

Then test with your real key, still offline:

```bash
./.venv/bin/python simulate.py
```

Same flow, real model, no group chat. This is where you tune prompts — you'll
see whether agents actually respect the briefings, or whether the reviewer is
firing too often.

```bash
./.venv/bin/python simulate.py --show-prompts     # see exactly what's sent
./.venv/bin/python simulate.py --turn 1           # shorter run, cheaper
./.venv/bin/python simulate.py --inject-bad       # prove the reviewer fires
./.venv/bin/python simulate.py --wizard           # preview the onboarding screens
```

`--wizard` needs no bot and no key — it prints the six onboarding screens exactly
as each person will see them, then shows the briefing their agent receives.

The summary line tells you what the gate did:

```
reviewed: 15  rejected: 2  rewritten: 2  dropped: 0
constraint removals: 1
```

If `rejected` is high, your briefings or persona prompts are pushing agents
toward jabs. If `constraint removals` is high, the system agent is proposing
badly and you should tighten the pool tags.

**Only then** make the group: add all six bots, hand each person the link to
their own bot, and run `/ask` for real.

## What each person does

They open their own bot and tap `/start`. Six screens, all buttons, no typing:

```
What can you eat?           Halal | Vegetarian | Vegan | No pork | No beef | No seafood
Any allergies?              Shellfish | Peanuts | Gluten | Dairy | Eggs
Budget per person?          Under 30k | 30-60k | 60-100k | 100-150k | 150-250k | No limit
When are you usually free?  Lunch | Afternoon | Evening | After 20:00 | Flexible
Anything to avoid?          Loud | Queues | Smoking | Outdoor | Long walks
Does this look right?       [ Looks right ]  [ Start over ]
```

Forty seconds. Then `/note <text>` if they want to add one soft preference,
`/show` to read it back, `/start` to redo it.

**Why buttons and not a text box.** Free text is where this product leaks. *"I'm
easy"* or *"not too expensive"* gives an agent nothing to defend, so it invents
something — and a fabricated allergy is indistinguishable from a real one to
everyone at the table. Every field here comes from a fixed vocabulary, so the
agent always knows exactly what is hard, what is soft, and what was never said.

The briefing an agent receives looks like this:

```
HARD CONSTRAINTS - never violate, never soften:
- Diet: Halal, Vegetarian
- Allergies (absolute): Shellfish
- Budget ceiling: Rp 80.000 per person
- Available: evening, 17-20
- Must avoid: Loud places

Rules about this list:
- These are the only constraints you have. Never claim one that is not
  written here.
- If an option breaks one, say so plainly and decline it.
- If it is not on this list, it is not a constraint at all.
```

Its reviewer gets the same block, which is what makes the review checkable
rather than a matter of taste: *is this claim in the list, or isn't it?*

Legacy free-text briefings still load, but they are downgraded to a soft note —
they can inform a preference, never assert a constraint.

## Game master console

`gm.py` reads the same SQLite files the bots write, so it works while a round is
live.

```bash
python gm.py briefings        # every briefing, who's still blank
python gm.py briefing budi    # one person's full text
python gm.py sessions         # recent sessions, status, turn counts
python gm.py show 3           # transcript by turn, blocked messages marked
python gm.py watch            # follow a session as it happens
```

Run `briefings` before each round. An unbriefed agent still argues — it just has
nothing to protect, and will happily agree to something its person would refuse.

## Configuration

```yaml
delegates:
  moderator_token: "${TELEGRAM_TOKEN_MAIN}"
  moderator_name: "Terserah"

  max_turns: 3            # turns before narrowing to three
  options_per_turn: 2     # 3 x 2 = 6 candidates considered
  max_words: 55           # per agent per turn
  post_delay: 0.4

  review: true            # content gate on every reply
  review_retries: 1       # rewrites before dropping
  constraint_check: true  # verify the shortlist against hard constraints

  restaurants: "data/restaurants.yaml"
```

Cost is roughly `turns × (agents × 2 + 2)` calls — the `× 2` is the reviewer.
Five agents over three turns is about 34 calls.

## Demo

`DEMO.md` has a two-and-a-half minute screen-recording script with a
shot list and the commands to run before recording.

## Limits

- The pool is curated by hand. Real live data needs a Places API; the interface
  in `restaurants.py` is small enough to swap.
- Reviewers fail *open* on infrastructure errors — a dead reviewer shouldn't
  silence the table. It's logged, so check `gm.py` if replies look off.
- Agents can't verify facts. They argue from their briefing and the pool.
- Telegram's quota is per bot token: roughly 30 messages/second overall and one
  per second to a given chat. Five agents sending one message each is nowhere
  near either. The real limit on session length is model latency, not Telegram.
- `main.py` must stay running. A sleeping laptop means a dead group.
