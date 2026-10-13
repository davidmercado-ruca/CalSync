# From "works on my Mac" to "the team uses it"

Written for David, September 2026, as a decision document rather than a plan.
Nothing here has been built.

---

## The question behind the question

"Turn it into a live web app" is really three separate decisions bundled
together, and you can change any one of them without touching the others:

1. **Distribution** — how do teammates get it?
2. **Hosting** — where does the sync actually run?
3. **Identity** — whose Google credentials, and who holds them?

Most of the cost and all of the risk live in #3. Most people arrive thinking the
hard part is #2.

---

## What breaks when you host it

CalSync today is one long-lived Python process with a file on your disk. Every
one of those properties is load-bearing:

| Piece | Local today | Hosted |
|---|---|---|
| The 3-minute loop | APScheduler inside a process that never exits | Serverless has no process that never exits. Functions run and die |
| SQLite at `~/.calsync` | A file on your disk | Ephemeral filesystem, wiped on every deploy. Needs a real database |
| Google tokens | Yours, on your Mac, mode 0600 | Everyone's, on a server you operate |
| `credentials.json` | One "Desktop app" client | A "Web application" client with a redirect URL |
| Timezone | `America/Bogota`, hardcoded | Per user |
| The dashboard | `127.0.0.1`, no login needed | A public URL. Needs authentication |

That last row is easy to underestimate. Right now the dashboard has no login
because it physically cannot be reached from outside your Mac. The moment it has
a URL, "no login" means anyone can read and rewrite your calendars.

---

## About Vercel specifically

Vercel is excellent at what it is built for: static sites and short request/
response functions, mostly JavaScript. It is a poor fit for the shape of this
app, and two verified facts explain why.

**Cron cannot run every three minutes on the free tier.** Vercel's own docs are
explicit: Hobby is limited to **once per day**, with ±59 minutes of imprecision.
Once per minute requires **Pro**, at $20/user/month
([Vercel docs](https://vercel.com/docs/cron-jobs/usage-and-pricing)).

**There is no long-running process at all**, on any tier. Your scheduler, the
in-memory lock that stops two cycles overlapping, and the SQLite connection all
assume a process that stays alive. On Vercel each function invocation is a fresh,
isolated, short-lived thing.

If you go hosted, **Render, Railway or Fly.io** fit this app far better: they run
a container continuously, so `python -m calsync` works essentially unchanged.
Vercel would mean rewriting the engine around functions.

### The better hosted design: stop polling

There is a genuine upside to having a public URL, and it is worth knowing even if
you never host anything.

Google can **push** to you instead of you polling it. `events.watch` registers an
HTTPS endpoint, and Google calls it when a calendar changes. The only requirement
is a valid SSL certificate — no domain verification
([Google docs](https://developers.google.com/workspace/calendar/api/guides/push)).
Any host gives you that for free.

Push notification channels expire after about a week, so you still need one
**daily** cron to renew them — which even Vercel Hobby can do.

That design is lower latency than your current 3 minutes, uses a fraction of the
API calls, and is the only shape that fits serverless. It is also the one piece
of hosted architecture that would genuinely improve the tool rather than just
relocate it.

---

## Yes, you need a database

Not because of multi-user — because of the filesystem. A hosted container's disk
is wiped on every deploy, so SQLite is out.

**Postgres**, via [Neon](https://neon.com) or [Supabase](https://supabase.com).
Both have free tiers that comfortably fit a team this size.

The good news: your schema barely changes. It is already keyed by account, so
multi-user is mostly one new `users` table plus a `user_id` column on
`accounts`, `habits`, `settings`, `mirrors`, `habit_slots`, `habit_blocks` and
`oplog` — and then a `WHERE user_id = ?` on every single query. That last part is
mechanical and it is also exactly where a mistake leaks one person's calendar to
another.

---

## The part nobody mentions: you would be holding everyone's keys

This is the real reason to think twice, and it has nothing to do with technology
choices.

A Google refresh token is **permanent read and write access** to that person's
calendar until they explicitly revoke it. Storing thirty of them means:

- One leaked environment variable, one misconfigured database, or one missing
  `WHERE user_id = ?` exposes thirty people's calendars — including a client's.
- You become the person who has to answer for that. Not Ruca's IT, not Google.
  You.
- Minimum responsible bar: tokens encrypted at rest with a key that is not in the
  database, per-user isolation verified by tests, an audit log of every write,
  and a documented way for someone to revoke and delete their data.

Right now none of this applies to you, because every token sits on the Mac of the
person it belongs to. That is not a limitation of the current design — it is the
single best security property it has, and hosting is what gives it up.

There is also a wall on the Google side: an unverified published app is capped at
**100 accounts** that have ever granted consent, and shows the "unverified app"
warning to every one of them. Three accounts each puts you at roughly 33 people.
Getting verified needs a privacy policy at a real URL, a public homepage, a demo
video and several weeks of Google's review.

---

## Three paths

### Path A — Distribute the local app

Each teammate runs their own copy, exactly as you do.

- **Build:** nothing. Zip the folder, or put it in a private Git repo.
- **They do:** follow `STEP-1-Google-credentials.md` (or use a shared
  `credentials.json`), then double-click `autostart.command`.
- **Cost:** $0. **Token custody:** none — each person holds their own.
  **Verification pressure:** none until ~33 people.
- **Effort:** an afternoon, mostly writing a one-page install note.

**Timezone per person** is already done — `config.TZ` reads the Mac's own zone.
**Business-hours defaults** are still yours (17:00 evening guard, 13:00 lunch);
fine as defaults, as long as the first run says so.

**This breaks when:** somebody is on Windows, or the team grows past the point
where "double-click this file" support requests stop being charming.

**What it does NOT break on:** shipping fixes. See the next section — that was
the strongest argument against this path, and it costs about ten lines to remove.

---

## The update problem, and why it is not an argument for hosting

The real objection to Path A is not cost or laptop lids. It is this: the moment
you hand out copies, every bug you fix lives in your folder and nowhere else. You
end up supporting four versions of the tool and cannot tell which one a
bug report came from.

Hosting solves this by making there be exactly one copy. That is genuinely its
main benefit, and it is worth naming clearly because the usual pitch ("it runs
even when your laptop is closed") is the smaller half.

But a distributed copy does not have to be a frozen copy. `start.command` and
`autostart.command` already run on every login. Having them refresh the code
first turns every teammate's Mac into a copy that updates itself:

```bash
# In the launcher, before starting Python:
curl -sL https://github.com/<you>/calsync/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=1 -C "$TARGET" 'calsync-main/calsync'
```

- No git needed on their Mac, no authentication, no account.
- **The repo can be public**, because it contains no secrets. `credentials.json`
  is not in it and never should be — it goes out once over Slack or 1Password,
  and every user's Google token stays in their own `~/.calsync`.
- Your fix reaches everyone on their next login. You keep one version.
- Pin it to a **tag** instead of `main` once more than two people use it, so
  pushing a broken commit does not reach anyone until you move the tag.

**What this costs, honestly:** it is a supply-chain surface. Code fetched from a
URL runs on your teammates' machines with access to their calendars. If your
GitHub account is compromised, so are they. That risk is real, and it is the
mirror image of the hosted risk — there you hold everyone's tokens, here you hold
the code path into everyone's laptop. The difference is that a compromised
GitHub account is recoverable and 2FA-protectable, while thirty leaked refresh
tokens have to be revoked one by one by thirty people.

**The lazy version, if ten lines still feels like too many:** put the folder in a
shared Drive folder and tell people to re-run `autostart.command` when you say
so. It works. It just relies on people doing it.

### Path B — One hosted instance per person

Same app, containerised, one small always-on instance per teammate on Fly.io,
Railway or a cheap VPS.

- **No multi-tenancy.** No shared database, no `user_id` columns, no cross-user
  leak surface. Each instance holds exactly one person's tokens, like their Mac
  did.
- **Keeps SQLite** (on a small persistent volume) and **keeps APScheduler**.
  Almost no code changes: a Dockerfile, a volume mount, and a password on the
  dashboard.
- **Cost:** on Fly.io, an always-on `shared-cpu-1x` with 256MB is **$1.94/month**
  plus **$0.15/GB/month** for the volume — call it about **$2.10 per person per
  month**, with no minimum plan fee
  ([Fly.io pricing](https://fly.io/docs/about/pricing/)). Railway is priced per
  plan rather than per machine: Hobby is **$5/month including $5 of usage**, and
  RAM bills at $10/GB/month
  ([Railway pricing](https://docs.railway.com/pricing/plans)), so for the
  one-instance-per-person shape Fly is the cheaper fit.
- **Effort:** about a day.
- **Solves:** the "it stops when my laptop sleeps" complaint, which is the only
  real functional weakness of Path A.

This is the lazy hosted answer and it is badly underrated. You get the benefit
people actually want without becoming a custodian of everyone's credentials.

### Path C — A real multi-tenant web app

- **Stack:** a container host (not Vercel) + Neon Postgres + push webhooks +
  session login + encrypted token storage.
- **What has to be built:** users table and `user_id` everywhere; the web OAuth
  flow with redirect URLs; encrypted tokens; push channel registration and daily
  renewal; authentication on every route; per-user timezone; and an admin view
  for the day it breaks for someone who is not you.
- **Cost:** $20–45/month plus your time, permanently. **Effort:** weeks to build,
  then it never stops being maintained.
- **Worth it when:** this is a product with an owner, not a team tool.

---

## Recommendation

**Do Path A now, with self-update turned on.** It costs an afternoon, and it tells
you something you cannot find out any other way: whether anyone other than you
actually keeps using it after week two. Most internal tools die there, and every
hour spent on hosting before that point is spent on a guess. The self-update line
is what makes this reversible — you are not committing to a version, you are
committing to a distribution channel you can keep pushing to.

**Move to Path B if and when** someone complains that it stops when their laptop
is closed. That is the one complaint hosting genuinely fixes, and B fixes it
without the token-custody problem.

**Only consider Path C** if it stops being a side project — if it has a real
owner, a security review, and someone other than you on call.

## If you want to learn the hosted path anyway

That is a legitimate goal on its own — it is the point of Skills Dev. But do it
as a **separate, deliberately small project**, not by migrating the tool people
have started to depend on.

Migrating a working tool in order to learn is how you end up with neither: the
tool breaks while you are halfway through, and you are debugging under pressure
instead of learning. Build a throwaway two-table multi-user app to learn OAuth
redirects, Postgres and sessions. Then decide about CalSync with those instincts
already in hand.

If and when you do migrate, the order that keeps it working the whole way:

1. Swap SQLite for Postgres locally, still single-user. Nothing else changes.
2. Containerise and deploy for yourself only. This is Path B.
3. Add login and `user_id` isolation, with tests that prove one user cannot read
   another's rows.
4. Add the web OAuth flow, encrypted at rest, with your account as the only user.
5. Only then invite a second person.

Each step leaves you with something that works.
