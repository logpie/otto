# Otto design review — Slack-style team IM walkthrough

_Reviewer: Claude Sonnet 4.6 · Date: 2026-05-04_

This review walks through how a real user would use the Otto redesign
(research.md + plan.md + otto-wireframes.md) to build a mid-complexity
SaaS product: a Slack-style team IM webapp. The product includes
workspaces, channels, DMs, threads, mentions, file uploads, emoji
reactions, presence indicators, search, notifications, mobile-responsive
UI, and slash-command integrations. Multi-user, multi-tenant, real-time,
stateful.

---

## 1. Multi-tenancy decomposition

The research.md compile stage rule is:

> "If two Features have overlapping `owned_paths`, they're merged into one Group."

Workspace isolation in a Slack clone is a cross-cutting concern, not a
file. `workspace_id` appears in every query against every table — users,
channels, messages, memberships, reactions. The file `models.py` (or
equivalent `db/schema.sql`, `lib/workspace.py`) is touched by Auth,
Channels, DMs, Threads, Reactions, Search, and Notifications.

**The problem:** Compile's file-overlap rule will pull nearly every
Feature into the same Group (the one that owns `models.py`), or the
compile agent will produce a spec where every Group declares a dependency
on a "Workspace" Group that was never explicitly asked for. Neither
outcome maps cleanly to user intent.

What's missing is a concept of **shared infrastructure Features** — a
workspace tenancy layer isn't a product Feature the user asked for; it's
the data model that all Features share. The current spec schema has no
slot for "foundational layer that all Groups depend on but no single
Group owns." The only mechanism is `dependencies[]`, which serializes
merge order but doesn't address *file ownership* when the foundation file
is owned by Group A but also written by Group B during its build.

Concretely: if Group `channels` owns `models.py` because channel schema
is in there, and Group `auth` also writes to `models.py` to add the
`User` model, the merge queue detects a conflict on landing. Land Group A
first, Group B rebases — but the file they each wrote was never split.
The merge queue's "rebase + rerun checks + atomic land" sequence handles
this *after the fact*, but the spec never declared that `models.py` is a
shared foundation. This is an ownership ambiguity that the current model
doesn't resolve at compile time; it shows up as merge conflicts at land
time instead.

**Specific paragraph in research.md that's incomplete:** Section 3,
"How Features and Groups relate," steps 1–3, never distinguishes
foundational files (touched by all Groups) from feature-owned files
(touched by one Group). The rule "overlapping owned_paths → same Group"
collapses the whole product into one Group if `models.py` is shared. The
only escape hatch is the `dependencies[]` field, but that's a merge
*ordering* field, not a file ownership field.

**Concrete suggestion:** Compile should emit a `Foundation` Group
automatically for Slack-class products — a Group containing the
workspace/tenant data model, auth primitives, and DB schema. All other
Groups declare `dependencies: ["foundation"]` and `owned_paths` excludes
the shared schema file. The compile prompt needs explicit instruction on
this pattern for multi-tenant products.

---

## 2. Real-time state

Presence indicators, typing indicators, and live message delivery require
a persistent server-side event bus — typically a WebSocket hub, an
SSE broadcast layer, or a pub-sub process. This infrastructure doesn't
belong to any single Feature. It belongs to nobody and everybody.

The current design has no concept of **shared runtime infrastructure**
that lives alongside the product but is not a Feature. The `owned_paths`
model assumes files map to Features. A WebSocket hub (`ws_server.py`,
`redis_pubsub.py`, or similar) is a cross-cutting runtime component, not
a Feature file.

**How would Otto plan this today?** The compile agent would either:
(a) assign WebSocket infrastructure to the `presence` Group, meaning
Channels and DMs can't use it without depending on `presence`, which is
backwards; or (b) create a `realtime-infrastructure` Group with no
Features, which is a Group with zero verdicts — invisible in the Proof
and with no audit target.

Option (b) reveals the deeper issue: the Group model is a *dispatch unit
+ verdict carrier*. A Group must have at least one Feature to earn a
verdict. But a WebSocket hub has no user-visible Feature — it's an
internal capability that makes three other Features possible.

**The design has no coherent answer for shared infrastructure that
doesn't belong to any single Feature.** The closest escape hatch is
adding "WebSocket infrastructure" as an explicit Feature in a
`realtime-foundation` Group with a `StateInvariant` check ("WebSocket
server starts, accepts connection, broadcasts test message"). That's
technically valid — it produces a verdict — but it puts internal
infrastructure on the user-facing Feature list, cluttering the Proof with
plumbing the user didn't ask for.

A better model: introduce **infrastructure blocks** as a first-class
spec concept, distinct from Features. Infrastructure blocks have `owned_paths`
and `checks` but no user-facing verdict. The Proof surfaces them as
"foundation components" separate from the Feature list.

---

## 3. The "1 Feature per Group default" rule under load

Research.md states:

> "Default rule: one Feature per Group, unless file-ownership forces
> grouping."

A v1 Slack clone has at least 30 user-visible Features when written out:
workspace creation, workspace invitation, workspace switching, channel
creation, channel membership, channel messages (CRUD), DMs, DM threads,
message threads, message editing, message deletion, emoji reactions,
file uploads, file preview, mention extraction, mention notifications,
desktop notification delivery, push notification opt-in, presence
indicators, typing indicators, user search, message search, channel
search, slash-command parsing, webhook outbound, mobile-responsive
layout, avatar upload, user settings, notification preferences, unread
count badges.

Under the default rule, this produces roughly 20–30 Groups, most with
one Feature each. That means 20–30 parallel agents, 20–30 branches, and
a merge queue with 20–30 entries.

**What happens to the merge queue?**

Research.md's merge queue is described as "eligibility-gated FIFO."
With 30 entries and deep dependency chains (channels depend on workspace,
messages depend on channels, threads depend on messages, reactions depend
on messages, search depends on messages, notifications depend on messages
and mentions), many Groups are blocked until foundation Groups land. The
effective concurrency is constrained by the dependency graph depth.

In practice: a 5-level-deep dependency chain (foundation → auth →
workspace → channel → messages → threads → reactions) means at most 5
sequential land waves even with perfect parallelism. With 30 Groups and
5 waves, the merge queue is doing fine in theory. But:

- Each land attempt reruns checks. With 30 Groups each running 3 checks,
  that's 90 check runs in the land stage alone.
- The audit loop (Layer 2) operates per-Feature. With 30 Features, one
  failure triggers a repair + re-audit. Re-audit of "only affected
  Features" requires the audit agent to still walk the product. With
  30 Features, even a scoped re-audit touches a lot of surface area.
- Cost scales linearly. At $0.10 per Group-agent session average,
  30 Groups = $3 just in build agents, before audit. The $5 default
  cost cap in the wireframe's New Run dialog (screen 8) would be
  exhausted before audit completes.

**No documentation in research.md addresses the cost/scale interaction
for large products.** The configurable budget section (§5) exists but
provides no guidance on what a realistic budget looks like for a
30-Feature product. This is a friction point for real users.

---

## 4. Spec review at scale

Screen 4b wireframe shows a doc-editor spec: 2 Groups, 6 Features, fits
cleanly in one drawer panel. It's readable and actionable.

Scale to a Slack v1 spec: 8 Groups, 30 Features. The Groups & Features
section of screen 4b would require scrolling through approximately 10
screen-heights of content. Every Group is an expandable section; every
Feature has a checkbox + description + `[edit] [remove]` controls.

At 30 Features, the Form view (screen 4b) is technically navigable but
practically overwhelming. The user is asked to review 30 checkboxes and
decide which to drop before building. This is the spec review gate's core
value proposition — but at this scale, most users will either (a) approve
blindly without reading, defeating the gate's purpose, or (b) be
overwhelmed and abort.

**Specific UX gaps:**

- No bulk-group operation: "Drop all Features in group X" requires 5
  separate uncheck clicks.
- No priority/MVP filter: no way to say "build only the first 10 Features
  in this order."
- No grouping by dependency: the form shows Groups in no guaranteed order;
  the user can't tell which Groups need to land first without reading
  every `depends on:` annotation.
- The "Suggestions from compile" panel at the bottom grows proportionally.
  For a 30-Feature product, compile will suggest 10+ additional Features,
  each needing an explicit accept/dismiss. This panel overflows.

**The screen doesn't overflow in the HTML sense** — it scrolls — but it
overflows in the *cognitive* sense. The wireframe's information density
rules ("verdict above evidence") work beautifully for a 6-Feature product;
they don't have a scaling strategy for a 30-Feature product.

---

## 5. Audit walkthrough — multi-user scenarios

The audit model is one walkthrough agent with one browser session. This
is described explicitly in research.md §6 (Audit) and the screen 6
wireframe — one walkthrough, one set of screenshots, one narrative.

**The multi-user scenario:** "User A in #general DMs user B; user B sees
the DM with desktop notification." This requires:
- User A logs in (session 1)
- User A composes and sends a DM to user B
- User B's browser (session 2) receives the DM via WebSocket
- User B's browser shows a desktop notification

A single-browser-session audit agent cannot execute this. It can verify
that the DM was stored in the database (StateInvariant), and it can
verify that the notification API exists (ApiProbe), but it cannot
simultaneously be User A *and* observe User B's live browser reacting to
the message.

**This is a fundamental limit, not a configuration gap.** The current
audit model assumes one actor, one perspective. Real-time features are
verified by proxy (the message was stored, the WebSocket endpoint accepts
connections, the notification schema is correct) rather than by
observation (User B actually saw the notification in their live browser).

**What evidence would the Proof show for a DM receipt?**
- A screenshot of User A's DM compose view
- An ApiProbe result: `POST /api/messages` → 201 Created
- A StateInvariant: message row exists in DB with correct recipient_id
- Possibly a BrowserJourney: navigate to User B's DM inbox URL and verify
  the message is listed there (simulating what User B would see)

This is meaningful evidence, but it doesn't prove real-time delivery. It
proves that the message was stored and is retrievable. A reviewer looking
at this Proof cannot distinguish "DMs work" from "DMs are stored but
WebSocket delivery is broken." The Proof passes; production WebSocket
delivery is broken.

**No architectural change fixes this within the current single-agent
audit model.** Addressing it requires either: (a) a second concurrent
audit agent in a separate browser session (complex orchestration, not in
the current design), or (b) explicit acknowledgment in the Proof that
"real-time delivery is tested by proxy (StateInvariant + ApiProbe), not
by observed live delivery." Option (b) is honest; option (a) is a design
extension. Currently neither is specified.

---

## 6. Per-Feature proof persuasiveness

The wireframe (screen 6) for the `auth` Feature is genuinely persuasive.
It shows:
- A walkthrough segment with 3 timestamped screenshots
- 4 deterministic checks with explicit pass/fail and output excerpts
- A plain-English audit narrative (2 paragraphs)

This looks like a real test report. The audit narrative is specific:
"I tried 6 wrong passwords; the 6th was rate-limited as expected." A
reviewer can read this and have justified confidence.

**Now apply this to "Mentions trigger notifications":**

Evidence Otto can currently collect:
- BrowserJourney: navigate to #general, type "@username test message",
  submit, screenshot the message rendered with mention highlight
- ApiProbe: verify the message endpoint stored `mentions: ["username"]`
  in the response
- StateInvariant: verify a `notifications` table row was created for
  the mentioned user

Evidence Otto cannot currently collect:
- That the notification appeared in the mentioned user's browser UI
- That the desktop notification fired (requires a second browser session)
- That the notification badge incremented for the mentioned user (visible
  only in the recipient's UI)

**What the Proof would actually show:** A screenshot grid of the *sender's*
perspective — the message with the mention highlighted, an API response
showing the mention was parsed, a DB row showing the notification was
created. A reviewer looking at this grid could blow through it in 30
seconds thinking "looks good" — because the evidence is *technically
correct* (notification record created) without proving the *user experience*
(notification delivered and visible).

The gap is not in the Proof format — screen 6's format is excellent. The
gap is in what a single-browser-session audit agent can observe. For
Features that require a recipient's perspective to verify, the Proof will
systematically show sender-side evidence and call it complete.

This is the honest version of the problem: the Proof format is
persuasive; the *evidence collection capability* is incomplete for
multi-actor Features.

---

## 7. Iterative product development — 4-wave Slack build

The brownfield iteration model (research.md §8, §9.4) is designed for
this pattern. Let's walk each wave:

**Wave 1 — channels + DMs (v1):**
Greenfield run. Compile produces a spec. Spec review gate opens. Build
runs ~15 Groups in parallel. This works cleanly under the current design.
The main friction is §3 (30 Groups at scale) and §2 (shared WebSocket
infrastructure).

**Wave 2 — threads (v2):**
User runs `otto run "add message threads to channels and DMs"`. Compile
reads existing spec + working tree. The `threads` feature overlaps with
`messages` table in `models.py`. Compile must decide: new Group (new
Feature, minimal overlap) or extend the `messages` Group (deep overlap).

Research.md §9.4 acknowledges this is "acknowledged-needed but not yet
specified." The brownfield compile mode is explicitly deferred to Phase A6,
which is "gated on Phase A0–A5 proving stable on greenfield." For Wave 2,
the user is blocked — `otto run` against an existing Slack codebase
doesn't have a specified behavior yet.

**This is the biggest friction point for the 4-wave scenario.** Waves 2,
3, and 4 all require brownfield compile. The design acknowledges the gap
but defers it. Until Phase A6 ships, iterative SaaS development above
v1 isn't supported.

**Wave 3 — search (v3):**
Assuming Phase A6 is shipped, `search` is a relatively independent
Group (reads messages, writes nothing). The file overlap with `messages`
is read-only. The brownfield compile rule for "new Feature, no file
overlap" (research.md §3, "Brownfield routing for new Features") should
produce a clean new Group. This wave is the low-friction case.

**Wave 4 — integrations (v4):**
Slash commands and webhooks touch the message parsing pipeline (modifies
message handler), the channel routing logic (modifies channel model), and
adds new outbound HTTP infrastructure. This is a high-overlap wave —
Compile will detect overlaps with existing Groups and must decide whether
to extend them or produce new Groups with `owned_paths` conflicts. The
merge queue will need to handle contention on `routes/messages.py` and
`models.py`. This is the hard brownfield case, and it's the case the
design hasn't fully specified.

**Synthesis:** Waves 2 and 4 create real friction. Wave 2 is blocked
until Phase A6. Wave 4 stresses the brownfield compile rules in ways the
design hasn't exercised. Wave 1 and Wave 3 work cleanly. The overall
story is: Otto supports greenfield builds and feature-addition to clean
surfaces; it struggles when the new work modifies existing critical paths.

---

## 8. Specific suggestions for multi-tenant + real-time + multi-user products

**S1 — Foundation Group as a first-class compile output.**
Compile should recognize multi-tenant products and automatically emit a
`Foundation` Group containing: workspace/tenant data model, auth
primitives, shared DB helpers. All other Groups declare
`dependencies: ["foundation"]` and their `owned_paths` never include the
foundation files. This eliminates the file-overlap collapse problem (§1)
and gives the merge queue a clean root node.

**S2 — Infrastructure blocks in the spec schema.**
Add a new spec-level concept: `infrastructure_blocks[]`, distinct from
Features. Each block has `owned_paths`, `checks` (StateInvariant,
ApiProbe), but no user-visible verdict. The Proof surfaces blocks as
"Components" — a separate section below Features, collapsed by default.
This solves the WebSocket hub problem (§2) without polluting the Feature
list with plumbing.

**S3 — Spec review scale: MVP mode.**
Add an "MVP scope" affordance to the spec review gate (screen 4): a
toggle or slider that filters the Feature list to "core" vs "extended."
Compile should tag Features as `tier: core | extended | optional` based
on whether they're mentioned directly in the Intent or inferred. The spec
review gate defaults to showing only `tier: core` Features, with an
"+ N extended features" expansion. This keeps the gate usable at 30
Features without removing the user's ability to review everything.

**S4 — Acknowledge the single-session audit limit honestly in the Proof.**
For Features that require multi-actor verification (DM receipt, mention
notification delivery, presence indicators), the Proof should explicitly
flag: "This Feature includes real-time delivery components that were
verified by proxy (StateInvariant + ApiProbe). Live multi-user delivery
was not directly observed." This is more honest than a passing verdict
backed only by sender-side screenshots. Add an `evidence_completeness`
field to Feature verdicts: `full | proxy_only | partial`.

**S5 — Cost guidance for large products.**
Add a product-complexity estimator to the compile output. After Compile,
before the spec review gate, show: "Estimated cost: $8–14 · wall: 35–50
min (based on N Groups, M Features)." This lets users make an informed
budget decision at the spec review gate rather than discovering at $7
that the $5 cap was too low. The wireframe's screen 8 (New Run dialog)
already shows a cost cap field; giving the user an estimate before they
set that cap is the missing step.

**S6 — Brownfield compile prioritization.**
Accelerate Phase A6 (brownfield compile mode). For iterative SaaS
development — which is the realistic sweet spot for Otto — greenfield v1
is not the bottleneck. Wave 2 and beyond are where the product actually
develops. Phase A6 being deferred until A0–A5 are stable means the
design won't serve iterative development until well into the roadmap. At
minimum, document the intended brownfield behavior for the file-overlap
cases so users know what to expect.

**S7 — Merge queue cost transparency.**
In the running-Run drawer (screen 5), show a projected cost breakdown:
"Groups building: 4 · Groups waiting: 12 · Audit pending: ~$2.50." At
30 Groups, users need visibility into cost trajectory mid-run, not just
at completion. The current screen 5 shows "Cost so far: $1.40 (cap $5)"
which is good for a 4-Group run but insufficient for a 30-Group run
where $1.40 in Build means $8 total is coming.

---

## Summary assessment

Otto's architecture is well-suited for 4–10 Feature greenfield products.
The Feature/Group/Guardrail model, the two retry layers, the per-Feature
Proof, and the spec review gate all compose cleanly at that scale.

At Slack-clone scale (30 Features, multi-tenant, real-time,
multi-actor), four structural gaps emerge:

1. File-overlap collapse at compile time (all Groups collapse into one
   when `models.py` is shared).
2. No first-class slot for shared infrastructure (WebSocket hub has no
   Feature, no owner).
3. Brownfield compile (Phase A6) is deferred, blocking iterative
   development above v1.
4. Single-session audit cannot verify multi-actor real-time features
   honestly.

Gaps 1 and 2 are design extensions (Foundation Groups, infrastructure
blocks). Gap 3 is a roadmap prioritization question. Gap 4 is a
fundamental constraint that should be documented honestly in the Proof
output rather than silently hidden behind proxy evidence.

None of these gaps are fatal. The design is sound at its intended scale.
The Slack-clone scenario reveals where the boundary of that scale is.
