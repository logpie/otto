# Otto design walkthrough — Twitter clone (tweetfeed)

Scope: greenfield product. Timeline of posts, follow/unfollow, replies,
likes, profile pages, real-time updates (websockets), media uploads
(images), search, notifications, basic moderation. A real product a
small team would ship.

This review walks through how the research.md + plan.md design handles
this scale, and where it produces a weaker product than a senior
engineering team would build by hand.

---

## 1. Decomposition

### What Compile would likely emit

A senior team would organize a Twitter clone along these verticals:
auth/accounts, feed/timeline, social graph, content (posts, media),
engagement (likes, replies), notifications, search, moderation, and
real-time infrastructure. Otto's Compile agent should derive something
similar from the intent. Here is a plausible Spec:

**Groups and Features:**

```
Group: foundation
  owned_paths: models.py, db.py, migrations/, config.py
  Features:
    - user-accounts        (register, login, profile model)
    - follow-graph         (follow/unfollow, follower/following counts)

Group: feed
  owned_paths: routes/feed.py, templates/feed.html, static/feed.js
  depends_on: [foundation]
  Features:
    - home-timeline        (paginated feed of followed users' posts)
    - post-creation        (text post, 280 chars, timestamp)

Group: content
  owned_paths: routes/posts.py, routes/media.py, templates/post.html,
               static/upload.js, storage/
  depends_on: [foundation]
  Features:
    - post-detail          (single post view, permalink)
    - image-upload         (attach image, server resize, embed inline)
    - reply-thread         (threaded replies, collapse depth > 2)

Group: engagement
  owned_paths: routes/likes.py, routes/replies.py, models.py (shared)
  depends_on: [foundation, content]
  Features:
    - like-tweet           (like/unlike, idempotent, count)
    - reply-creation       (reply to post, @mention parent author)

Group: profiles
  owned_paths: routes/profile.py, templates/profile.html
  depends_on: [foundation, content]
  Features:
    - profile-page         (user's posts, follower counts, bio)
    - profile-edit         (avatar upload, bio, display name)

Group: search
  owned_paths: routes/search.py, templates/search.html, search_index.py
  depends_on: [foundation, content]
  Features:
    - post-search          (full-text search, result list, click to post)
    - user-search          (find users by handle/name)

Group: notifications
  owned_paths: routes/notifications.py, templates/notifications.html,
               models.py (shared)
  depends_on: [foundation, engagement, content]
  Features:
    - notification-feed    (inbox: liked, replied, followed, @mentioned)
    - notification-mark-read

Group: realtime
  owned_paths: ws_server.py, static/ws_client.js, routes/ws.py
  depends_on: [foundation, feed, notifications]
  Features:
    - ws-timeline-push     (new post from followed user appears without reload)
    - ws-notification-push (notification badge updates live)

Group: moderation
  owned_paths: routes/admin.py, templates/admin.html
  depends_on: [foundation, content, engagement]
  Features:
    - report-post          (flag post for review)
    - admin-review         (moderator queue, approve/remove)
```

**Evidence kinds per Feature (representative):**
- `like-tweet`: `ApiProbe` (POST /likes, DELETE /likes idempotency),
  `StateInvariant` (likes count in DB matches API), `RepoTestCheck`
  (pytest), `BrowserJourney` (click heart, count increments, click again
  decrements)
- `ws-timeline-push`: `BrowserJourney` (two-browser session, post from
  account A, confirm appears in account B's feed within 2s)
- `image-upload`: `ApiProbe` (multipart POST, 400 on oversized),
  `StateInvariant` (file exists on disk / in storage after upload),
  `BrowserJourney`

### Does this match how a senior team would structure it?

Mostly yes, with two notable divergences:

**Problem 1: `models.py` is owned by multiple Groups.** Foundation,
engagement, notifications, and content all touch `models.py`. Otto's
Compile rule says overlapping `owned_paths` forces a merge. If taken
literally, everything that touches `models.py` collapses into one
massive Group — the exact opposite of a senior team's domain
separation. In practice the merge rule needs a "shared file" exception:
a file can be listed in multiple `owned_paths` when it's a shared
schema layer rather than a domain boundary. This exception is not
specified in research.md. The compile agent would need prompt guidance
to emit `models.py` as a cross-cutting dependency rather than an
ownership signal. Without that guidance, Compile either produces one
giant Group or leaves agents fighting over schema changes.

**Problem 2: `realtime` Group doesn't fit the model cleanly.** The
websocket server is infrastructure, not a feature vertical — it has no
user-visible content of its own, it is a delivery mechanism used by
Feed and Notifications. Otto's model works well when a Group is a
self-contained product slice (auth, feed, profile). It works less well
when a Group is a platform layer that other Groups call into. Explored
further in section 6.

---

## 2. Brownfield iteration — adding real-time updates

Scenario: user ships v1 with timeline, follow, post. Now wants
websocket-based push.

**How Otto handles it:**

The user runs `otto run "add real-time timeline push via websockets"`.
Brownfield Compile (research §9.4) reads the working tree and existing
`spec/spec.json`, detects that `routes/feed.py` and `templates/feed.html`
already exist (owned by the `feed` Group), and determines that the new
Feature (`ws-timeline-push`) requires new files: `ws_server.py`,
`static/ws_client.js`, plus edits to the client-side `static/feed.js`.

The routing rules (research §3, brownfield routing):
- `ws_server.py` and `ws_client.js` are new files — no overlap with
  `feed` Group's `owned_paths`. Under the rule, this spawns a new Group.
- But `static/feed.js` is in the feed Group's `owned_paths`. This
  triggers the "new Feature, overlaps existing files" branch — new Group
  with extended `owned_paths` covering the overlapped file.

**Where the design works:** The spec diff screen (wireframe 4d) shows
the user exactly what would re-execute. The `feed` Group's agent gets a
focused repair brief: "add websocket client integration to
`static/feed.js`." That's the targeted edit Otto's model was designed
for.

**Where the design is fragile:**

First, the websocket server needs to know which users are following
which other users in real time. It needs access to the follow-graph
data, which is owned by the `foundation` Group. The new `realtime` Group
declares `dependencies: [foundation, feed]`. But brownfield Compile must
correctly detect this dependency — it can't just look at file overlap;
it needs to reason about runtime data flow (the ws server queries the DB
for follow relationships). The owned_paths heuristic is purely
file-based and misses runtime dependencies entirely.

Second, the existing feed Group built a standard HTTP polling timeline.
To switch to push, the client JavaScript needs to replace its polling
loop with a WebSocket listener. This is not a surgical addition — it
changes the fundamental update mechanism for an existing feature. Otto's
model dispatches a "new Group for ws infrastructure" and a "repair brief
for feed.js." But the agent receiving that brief for `feed.js` may not
have full context about how the websocket server works, because the ws
server was built by a different Group's agent in a different worktree.
The merge queue serializes the land, but agents in parallel worktrees
don't share context during Build. The result is a real risk of
interface mismatch: ws server emits one message format, feed.js expects
another. Otto's design has no mechanism to share an agreed contract
(e.g., a shared types file or event schema) across Group boundaries
during Build.

---

## 3. Audit feasibility — user A posts, user B sees within 2s

This is the core correctness requirement for real-time push. It requires:
- Two browser sessions, different authenticated accounts
- Account A posts a tweet
- Account B's timeline updates within 2 seconds without a reload

**What the audit walkthrough log would contain:**

```jsonl
{"feature_ids":["ws-timeline-push"], "action":"navigate", "url":"http://localhost:5000/login", "narrative":"Opening account B session in Tab 2"}
{"feature_ids":["ws-timeline-push"], "action":"fill_form", "fields":{"email":"userB@example.com","password":"..."}, "narrative":"Logging in as user B"}
{"feature_ids":["ws-timeline-push"], "action":"screenshot", "path":"assets/ws-01-userB-timeline.png", "narrative":"User B timeline loaded; 0 posts visible; WebSocket connected (saw ws:// upgrade in network tab)"}
{"feature_ids":["ws-timeline-push"], "action":"open_tab", "tab_id":"tab-a", "narrative":"Switching to Account A in new tab"}
{"feature_ids":["ws-timeline-push"], "action":"navigate", "url":"http://localhost:5000/login", "tab":"tab-a", "narrative":"Logging in as user A (who B follows)"}
{"feature_ids":["ws-timeline-push"], "action":"post_tweet", "text":"hello world from A", "tab":"tab-a", "narrative":"Posting from account A"}
{"feature_ids":["ws-timeline-push"], "action":"record_timestamp", "label":"post_sent_at", "value":"14:03:22.441"}
{"feature_ids":["ws-timeline-push"], "action":"switch_tab", "tab_id":"tab-b"}
{"feature_ids":["ws-timeline-push"], "action":"wait_for", "selector":".tweet[data-author='userA']", "timeout_ms":2000, "result":"found", "elapsed_ms":340}
{"feature_ids":["ws-timeline-push"], "action":"screenshot", "path":"assets/ws-02-userB-receives-post.png", "narrative":"User B's timeline now shows A's post; elapsed 340ms"}
{"feature_ids":["ws-timeline-push"], "action":"verify", "check":"post text matches 'hello world from A'", "result":"pass"}
```

**Can the audit agent actually produce this?**

The audit agent is a single LLM pass (not two). It uses a browser tool.
Maintaining two authenticated sessions simultaneously requires two
browser contexts or two tabs. Most browser automation tools handle this
(separate contexts, or incognito). But the audit prompt (`otto/prompts/audit.md`)
must explicitly instruct the agent to:
1. Open two separate authenticated sessions before testing real-time Features
2. Record timestamps at both the send and receive events
3. Compute elapsed time and assert it is under 2s

None of this is specified in the current design. The generic "walk the
product, evidence each Feature" audit prompt will not spontaneously
execute a two-account, two-tab choreography. The Feature's
`acceptance_detail` field (research §2 vocabulary) is the right place to
encode this requirement: "Log in as user A and user B in separate
sessions. Post from A. Verify B's timeline updates within 2 seconds."
If `acceptance_detail` is populated and the audit prompt is instructed
to read it as a script, the walkthrough becomes executable. If
`acceptance_detail` is left empty or vague, the audit will verify "the
websocket endpoint exists" rather than "push actually works end to end."

This is a gap in the current design: `acceptance_detail` is defined in
the dataclass but the audit prompt's contract with it is unspecified.
For straightforward Features (a form submits, a page renders) the audit
agent's judgment suffices. For cross-account, multi-session, timing-
sensitive Features, `acceptance_detail` must be treated as a structured
audit script, not free-text description.

---

## 4. Quality findings vs blocked — when does "passed" mean shipped?

The design classifies quality findings as informational and non-blocking
(research §4). A Twitter clone can have every Feature pass its check
while shipping a product that feels broken:

- Timeline shows posts in random order instead of reverse chronological.
  Not a check failure. No spec line enforces ordering.
- Likes count shows stale data because the client doesn't re-fetch after
  liking. The audit agent clicks like, sees the count increment (client
  optimistic update), and passes. The underlying stale-data bug only
  shows if you reload.
- Images render at 4000×3000px because the server-side resize ran but
  the client CSS doesn't constrain dimensions. "Image upload" feature
  passes. The UX is broken.
- Notifications arrive but the badge count doesn't decrement when you
  read them. "Notification-mark-read" may pass (the API call returns 200)
  while the badge state is wrong.
- Profile page loads in 8 seconds because of an N+1 query. No
  performance check exists.

The design's stance (quality findings are informational, 3/5 quality
score) is correct for "don't block on polish." It is too lenient for
"is this usable at all." A Twitter clone with 8-second profile loads
is not a product anyone would use — yet it passes the current quality
model.

**What's missing:** a distinction between *quality findings* (polish,
aesthetic, nice-to-have) and *usability blockers* (behavior that makes
a Feature functionally unusable despite technically passing its check).
The design has no mechanism for "this Feature passed its check but the
audit agent flagged it as non-functional in practice." The
`quality-findings.json` severity field exists but the design doesn't say
what happens when severity is "critical" — it's still non-blocking.

A specific recommendation: add a `severity: "blocker"` level to quality
findings that *does* trigger the audit loop (same as a failing Feature
verdict). The user can suppress it with a Guardrail ("accept slow
profiles in v1"), but by default, a finding that makes a Feature
non-functional in practice should behave like a failure.

---

## 5. Per-Feature proof — like-tweet

What `proof/features/like-tweet/proof.html` would contain, and whether
it persuades a real reviewer:

**Nominal content (what the design produces):**

```
Feature: like-tweet
Verdict: ✓ passed

Description: User can like a tweet. Clicking the heart increments the
count. Clicking again (unlike) decrements it.

Built in group: engagement
Files changed: routes/likes.py, models.py, templates/post.html,
               static/like.js
Repair attempts: 0

Walkthrough segment:
  [screenshot: post with 0 likes, heart outline]
  00:14 GET /posts/42
  [screenshot: clicked heart, count shows 1, heart filled]
  00:17 POST /api/likes {post_id: 42}  → 201 Created
  [screenshot: unliked, count shows 0]
  00:21 DELETE /api/likes/42  → 204 No Content

Deterministic checks:
  ✓ ApiProbe  POST /api/likes/{id}  201 Created, body: {likes_count: N+1}
  ✓ ApiProbe  DELETE /api/likes/{id}  204, count back to N
  ✓ StateInvariant  likes table row created/deleted correctly
  ✓ RepoTestCheck  pytest tests/test_likes.py  5 passed

Audit narrative:
  "I navigated to a post by user A (post #42). I clicked the heart. The
   count incremented from 0 to 1 and the icon changed to filled. I
   clicked again; count went back to 0. The API calls returned 201 and
   204 respectively."
```

**Does this persuade a real reviewer? No — four gaps:**

**Gap 1: Race conditions.** Two users liking the same post simultaneously.
The proof shows serial single-user like/unlike. A senior reviewer asks:
is the likes count updated atomically? Does the DB use `UPDATE posts SET
likes_count = likes_count + 1 WHERE id = ?` (atomic) or a read-modify-write
(race condition)? The proof contains no `StateInvariant` that checks the
underlying SQL, and no concurrent test. This gap is invisible in the
proof unless the Check was explicitly written to test it. It won't be,
because the Compile agent generates evidence kinds generically.

**Gap 2: Duplicate likes.** Nothing in the walkthrough segment shows that
liking a post twice doesn't double-count. The `ApiProbe` checks pass on
the first call but don't test a second identical POST from the same user.
Without a `StateInvariant` or `RepoTestCheck` that explicitly tests
idempotency (user can't like the same post twice), the proof says nothing
about this case.

**Gap 3: Like a deleted post.** What happens if another user deletes the
post between when the heart is shown and when the like request arrives?
The proof has no evidence of a 404 or 410 response being handled
gracefully. The `acceptance_detail` field could capture this ("verify
liking a nonexistent post returns 404 and the UI shows an error"), but
only if the Compile agent includes it.

**Gap 4: Like-then-unlike state.** The walkthrough shows like then unlike
in sequence. But does the `likes_count` on the database side correctly
return to exactly the pre-like value? If there was a bug where unlike
decrements below zero, the screenshot showing "0" on the client would
still pass (client displays `max(0, count)`) while the DB has `-1`. The
`StateInvariant` check should verify the DB value directly, not the
rendered count.

**Root cause:** these gaps exist because the Compile agent generates
evidence kinds at a surface level (ApiProbe checks the API, BrowserJourney
clicks the UI). Edge cases — race conditions, idempotency, invalid state
transitions — require an explicitly written `acceptance_detail` that names
them. The current design has no mechanism to prompt Compile to generate
adversarial acceptance criteria. A Compile prompt that includes "for
mutation Features (like, follow, delete), add acceptance_detail covering:
concurrent mutations, duplicate operations, operation on nonexistent
resource" would close most of these gaps.

---

## 6. Cross-cutting infrastructure — the real scaling failure

A Twitter clone has three pieces of infrastructure that no single Group
owns and every Group depends on:

1. **WebSocket layer**: the ws server, connection registry, message
   routing. Used by Feed and Notifications Groups.
2. **Search index**: index writer called by Post creation (content Group),
   search queries handled by search Group. If the index is Elasticsearch
   or SQLite FTS, both Groups need to agree on the schema.
3. **Notification fan-out**: when user A posts, all of A's followers need
   a notification. This requires a query against the follow-graph (foundation
   Group) triggered by a post event (content Group) to write notifications
   (notifications Group). No single Group owns this flow end-to-end.

**How the design currently handles this:**

Otto's model assigns each Group a separate agent on a separate branch.
Agents don't share context during Build. The design's only cross-Group
coordination mechanism is `Group.dependencies[]` (serializes the merge
order) and `owned_paths` overlap detection (merges Groups with conflicting
files). Neither mechanism handles *runtime interface contracts*.

The notification fan-out example is the clearest failure: when post
creation triggers notification fan-out, the content Group's agent writes
`routes/posts.py`. The notification fan-out logic could live in:
- `routes/posts.py` (content Group), calling into the notifications module
- A separate event/signal handler
- A background job queue

If there is no agreed interface, the content Group agent will pick one
approach and the notifications Group agent will pick another. At Land
time, the merge queue detects no `owned_paths` conflict (different files),
so both branches land cleanly. The notification fan-out either doesn't
work at all, or is duplicated in two places.

**What the design lacks:**

A mechanism for Groups to declare *interface contracts* — not just which
files they own, but which APIs, events, or data schemas they expose to
other Groups. The current Spec only has `dependencies[]` (build ordering)
and `owned_paths` (file ownership). Missing: `provides[]` (APIs/events
this Group exposes) and `consumes[]` (APIs/events from other Groups this
Group calls).

Without `provides` and `consumes`, the compile step cannot verify that
when the content Group exposes `POST /api/posts → emits post.created
event`, the notifications Group is consuming `post.created`. Two agents
build compatible halves only if the LLM happens to make consistent
choices — which at scale it won't, especially in separate worktree
contexts.

For the websocket layer specifically: the ws server needs to receive
events from both Feed and Notifications, and push them to connected
clients. The connection registry (which user is connected on which socket)
is a shared mutable resource. If the ws Group builds the registry and
the feed Group's agent writes code that reads from it, the interface
must be agreed before Build starts. The merge queue can serialize Land,
but it can't retroactively reconcile incompatible interfaces.

---

## 7. Specific suggestions

**7.1 Add a "shared infrastructure" Group type.**
Introduce an optional `kind: "platform"` annotation on Groups. A platform
Group (ws server, search index, job queue) gets built first, before any
consumer Group. Its `provides[]` list is emitted as a literal interface
contract (e.g., a typed event schema file, a shared types module). Consumer
Groups receive this contract as input context at Build time. This is the
compile step's responsibility to wire together — not agent judgment.

**7.2 Acceptance detail as audit scripts for complex Features.**
For Features whose `evidence_kinds` includes cross-session or timing-
sensitive `BrowserJourney`, require `acceptance_detail` to be a structured
script (step-by-step instructions for the audit agent), not free text.
Compile should generate this automatically for any Feature tagged with
`requires_multi_session: true`. The audit prompt must be updated to
execute `acceptance_detail` scripts as written, not interpret them.

**7.3 Idempotency and race condition acceptance criteria in Compile.**
The Compile prompt should detect mutation Features (like, follow, delete,
upload) and automatically include in `acceptance_detail`: duplicate
operation test, concurrent operation test, operation on nonexistent
resource. These are mechanical additions the Compile LLM can make
reliably with the right prompt guidance.

**7.4 "Usability blocker" severity level in quality findings.**
Add `severity: "blocker"` to `quality-findings.json`. A blocker finding
triggers the audit loop the same way a failing Feature verdict does. The
user can suppress specific blocker classes via Guardrails. This closes
the gap where a Feature "passes" but is functionally unusable.

**7.5 Shared file handling in owned_paths.**
The current merge rule (overlapping owned_paths → merge Groups) is too
aggressive for large products. Add a `shared_paths` field at the Spec
level: files listed here are shared across Groups and do not trigger
Group merging. The Compile agent should identify schema files, config
files, and utility modules as shared_paths. Groups can read from shared
files but must route writes through the Group that "owns" the canonical
definition.

**7.6 Interface contracts between Groups.**
Extend the Group dataclass with `provides: [{name, kind, schema_ref}]`
and `consumes: [{group_id, name}]`. Compile derives these from the
intent. The `dispatch_plan` for each consumer Group includes the
provider's interface contract as context. This is the minimum needed
to prevent interface mismatch across independently-built Groups at
scale.

**7.7 Cross-account audit setup step.**
For products with authentication and real-time features, the audit
prompt needs an explicit "setup fixtures" phase: create N test accounts,
establish follow relationships, create baseline content. Without
pre-seeded fixture state, the audit agent spends most of its time
creating test data and little time actually exercising multi-user flows.
A `audit.fixtures` section in `otto.yaml` or per-Feature `test_setup`
field would let the user pre-declare what state should exist before
the walkthrough begins.
