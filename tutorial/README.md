# How to Use Agent Trust Lab (No Technical Background Needed)

This page explains the project in plain language first, then walks you
through the live website step by step with screenshots. No coding
knowledge required.

**Live site:** [agent-trust-lab.vercel.app](https://agent-trust-lab.vercel.app)

---

## Part 1 — What problem does this actually solve?

Forget the code for a second. Here's the situation in plain English.

Imagine you message a company's support chat:

> "Please refund me ₹500."

You'd assume that request goes straight to the bank. In reality, with
modern AI systems, it often passes through a **chain of AI assistants**,
each one handing the task to the next:

```
 You                Support AI          Refund AI           Bank / Payment
  │                      │                   │                    │
  │   "refund ₹500"      │                   │                    │
  ├─────────────────────▶│                   │                    │
  │                      │   "go refund it"  │                    │
  │                      ├──────────────────▶│                    │
  │                      │                   │    refund(₹500)    │
  │                      │                   ├───────────────────▶│
```

That's fine when everything behaves. But what if, somewhere in that
chain, something sneaky slips in — a fake note, a manipulated message, a
confused AI assistant — and the last AI in the chain ends up trying to do
this instead:

```
 You                Support AI          Refund AI           Bank / Payment
  │                      │                   │                    │
  │   "refund ₹500"      │                   │                    │
  ├─────────────────────▶│                   │      😈 injected   │
  │                      │                   │◀── malicious note ─┤
  │                      │                   │  refund(₹50,000)!! │
  │                      │                   ├───────────────────▶│
  │                      │                   │        ❌           │
```

You asked for ₹500. Somewhere in the middle, the request quietly became
₹50,000. **This is the exact problem this project is built around.**

This project answers three questions about that situation:

1. **Can we stop it from actually happening**, even if an AI assistant in
   the middle gets tricked? *(Yes — explained below as "permission
   slips.")*
2. **Can we prove afterward exactly what happened**, step by step, in a
   way nobody can secretly edit later? *(Yes — a tamper-proof record of
   every step.)*
3. **Can we figure out *which specific piece of information* caused the
   bad decision**, instead of just shrugging and saying "something went
   wrong somewhere"? *(Yes — this is the "auto-detect likely cause"
   button you'll use below.)*

That's the whole project. Everything on the website is just letting you
*watch this happen live*, safely, in a sandbox — no real money, no real
API calls to anyone.

### The "permission slip" idea, in one sentence

Each AI assistant in the chain is given a signed, tamper-proof permission
slip that says exactly what it's allowed to do (e.g. "refund up to ₹500,
nothing more"). Even if that assistant gets tricked into *wanting* to
refund ₹50,000, the system checks its permission slip first — and blocks
it, because the slip doesn't allow that much. Being tricked into wanting
something bad is not the same as being allowed to do it.

---

## Part 2 — Using the website, step by step

### Step 1 — Open the site

Go to [agent-trust-lab.vercel.app](https://agent-trust-lab.vercel.app).
You'll see this:

![Landing page](screenshots/01-landing.png)

On the left: pick a scenario and run it. On the right: once you run
something, you'll see what happened.

### Step 2 — Pick a scenario

Click the dropdown and choose **"Prompt injection"** — this is the exact
₹500-becomes-₹50,000 scenario described above.

![Scenario selected](screenshots/02-scenario-selected.png)

The description under the dropdown tells you in plain English what this
scenario does, before you even run it.

### Step 3 — Click "Run workflow"

This actually runs the whole chain: User → Support AI → Refund AI →
Payment system, exactly like the diagram above. You'll see:

![Trace result](screenshots/03-trace-result.png)

A few things to notice:

- **"denied"** (red badge, top left) — the system caught it. The refund
  attempt for ₹50,000 was blocked.
- **"log integrity OK"** (green badge) — the record of what happened
  hasn't been tampered with.
- **Execution trace** (middle column) — every single step, in order:
  who received what, who decided what, who tried to call the bank.
  Click any row to expand it and see the raw details.
- **Context items** (right column) — the two pieces of information the
  AI assistants actually saw: your real request (marked **trusted**) and
  the sneaky injected note (marked **untrusted**).

### Step 4 — Find out *why* it happened

This is the interesting part. Click **"Auto-detect likely cause."**

![Causal analysis](screenshots/04-causal-analysis.png)

The system doesn't guess — it actually re-runs the scenario multiple
times, each time removing one piece of information, and checks whether
the outcome changes. Here, it correctly points at the malicious note
(`c2`, highlighted red) as the actual cause, and clears your real request
(`c1`, highlighted green) as innocent — because removing your real
request *wouldn't have* changed anything, but removing the malicious note
*would have*.

### Step 5 — Try it yourself: change the evidence, see what happens

You can edit the malicious note's text yourself, right in the browser,
and re-run just that one piece to see the outcome change live.

1. Check the box next to `c2`.
2. Edit its text — replace the sneaky instruction with something harmless.
3. Click **"Replay with overrides."**

![Manual replay](screenshots/05-manual-replay.png)

Notice the outcome flips from `denied @ ₹50,000` to `executed @ ₹500` —
proof that this one piece of text was really what caused the bad
decision, not a coincidence.

---

## Quick glossary (plain English)

| Term on the site | What it actually means |
|---|---|
| Agent | A small AI program that does one job and can hand off to another |
| Trusted / Untrusted context | Trusted = came from the real user. Untrusted = could have been planted by someone malicious |
| Trace | The step-by-step record of everything that happened, in order |
| Replay | Running the same scenario again, safely, without touching anything real |
| Causal analysis | Figuring out *which* piece of evidence actually caused the outcome, by testing — not guessing |
| Authority / delegation token | The "permission slip" each AI assistant is given, saying exactly what it's allowed to do |

---

## Other scenarios worth trying

- **Benign refund** — the normal, nothing-goes-wrong case, for comparison.
- **Compromised sub-agent** — instead of a sneaky note, the Refund AI
  itself is imagined as hacked, trying to grant itself extra permission
  directly. Also blocked, for a different reason (its forged permission
  slip doesn't have a valid signature).
- **Custom** — write your own refund amount and your own injected note,
  in your own words, and see if the system still catches it.

If you want the deeper technical write-up — the actual engineering
reasoning, what's built vs. what's still a future idea — see the main
[README](../README.md).
