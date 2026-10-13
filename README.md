# CalSync

Keeps several Google calendars aware of each other. When something takes your
time in one account, it shows up as a busy block in the others — and when you
move the original, the blocks move with it. It also protects recurring time
(lunch, focus, exercise) across every account at once.

Google cannot do this on its own: **it only shows your primary calendar to other
people.** A second calendar you subscribe to is invisible to whoever is checking
whether you are free, so they book straight over it.

Everything runs on your own Mac. No server, no subscription, and **no AI tokens**
— it is Python doing date arithmetic.

---

## Setup, once

1. **Get Google credentials.** Follow `STEP-1-Google-credentials.md`. About ten
   minutes; ends with a `credentials.json` file in this folder.

2. **Double-click `autostart.command`.** CalSync installs itself as a background
   service, starts with your Mac, and comes back up on its own if it crashes. No
   Terminal window to leave open.

   The same file installs, updates and turns it off — it gives you a menu.

3. **Open the dashboard** at `http://127.0.0.1:8765` and connect your accounts.
   Type a short name (`Work`, `Client`, `Personal`), click *Connect account*, and
   authorise in the browser. Repeat per account.

   Google will warn that the app is not verified. That is expected — see the
   note in step 1. Continue via *Advanced*.

4. **Review before going live.** CalSync starts in **dry run**: it reads
   everything, works out every block it would create, and writes nothing. The
   *What it would do* list shows each one.

   Stay here a while. Check that the events you expect are listed, and that the
   ones you do not want are not. Open *events skipped, and why* if something is
   missing — the reason is spelled out.

5. **When the plan looks right**, uncheck *Dry run*, save, and hit *Sync now*.

> **Why the service copies the code elsewhere.** macOS protects `~/Documents`,
> `~/Desktop` and `~/Downloads`. A launchd service starts with no inherited
> permission and gets *Operation not permitted* on any file there, so the service
> runs from `~/Library/Application Support/CalSync`. This folder stays the
> source: whenever the code changes, double-click `autostart.command` and choose
> *update*. Settings and Google permissions live in `~/.calsync/`, shared by both
> ways of running it.
>
> Autostart works while your user session is open. If the Mac is off or asleep,
> syncing pauses and catches up on wake.

To run it by hand in a Terminal window — useful while developing — use
`start.command`. The first line of the log says which folder it started from, so
you always know which copy you are looking at.

---

## Needs your attention

The first section of the dashboard, with a counter in the top row. Three kinds:

| Kind | What it means |
|---|---|
| **double booked** | Two meetings in different accounts overlap. The problem this tool exists for, and the only one CalSync cannot solve: they involve other people |
| **meeting inside your block** | Someone booked inside a protected block. Names the meeting, the account, and the block it landed in |
| **block found no slot** | A flexible block did not fit even at its shortest length. Names the meetings that took the window |

Two things deliberately **not** reported: overlaps between two meetings in the
same account (you can see those yourself in Google), and the day simply running
out of hours. Both would bury what matters.

**CalSync never moves meetings that involve other people and never declines
invitations.** This list exists so you can decide.

---

## Busy blocks between accounts

*Settings → Directions and detail* has one row per direction — six rows for
three accounts. Each row decides whether that direction is on and how much it
reveals:

| Mode | Looks like |
|---|---|
| Just "Busy (source)" | `🔒 Busy (Client)` |
| Full detail | `🔒 Roadmap Q4` + location + meeting link + guests |
| Fixed label | `🔒 Work` |

It is per direction, not per account, because how much detail you want depends on
where you are looking from: a client's events in full detail in your work
calendar, and the same ones as "Work" in your personal one.

**Visible to others** is a different control: it does not change what *you* see,
but what everyone else sees in the target calendar. Off, your colleagues see a
titleless "Busy" block even when you see full detail. On, they see it like any
other event of yours — right when the other calendar belongs to a client or
project of the same organisation.

Guests are never copied as real guests, only as text in the description. Copying
them would make Google send a fresh invitation from the other account and
duplicate the event for everyone.

### Turning one off

| Want | Do |
|---|---|
| One event not to block | Put `[nosync]` in its title, from Google Calendar |
| Same, more cleanly | Mark it *Free* in Google |
| A standing rule | *Settings → Which events do NOT block time* |
| Undo everything | *Danger zone* → type `DELETE` |

`[nosync]` means "do not mirror this", **not** "this does not exist" — a tagged
event still occupies your time when blocks are being placed.

---

## Protected blocks

Each block is a rule, not an appointment:

| Field | What it does |
|---|---|
| finds a slot / fixed time | "Finds a slot" moves and shortens itself to fit. "Fixed time" always takes its hours, whether or not something is on top |
| ideal / shortest | What you want and what you will settle for. Lunch ships at 90/60 |
| from / until | The window it may move inside |
| preferred time | Where to try first. Empty = wherever it fits |
| how often | Every day checked, or N times among the checked days |
| visible | Whether colleagues see the real name or just a "Busy" block |
| color | The event color in Google |
| lives in | The account the original belongs to |
| clear when done | Off by default. On, each block is deleted once it is over |

A flexible block **gives up its time before its length**: 90 minutes elsewhere
beats 60 at the preferred hour. If not even the shortest length fits, it appears
under *Needs your attention* and you decide.

Three rules that are not obvious:

- **A placed block does not move while it still fits.** Without that, blocks
  would wander around your calendar every three minutes.
- **Fixed blocks do not get in the way of flexible ones.** "Not available" from 5
  to 8 tells the world not to book you; it does not stop you exercising at 6.
- **Blocks that already happened are never touched.** They are the record of what
  you did. Turn on *clear when done* per block if you would rather they vanish.

Planned 14 days ahead. Further out is pointless — it will move anyway once the
calendar fills.

### The original and the copies

Each block **lives in one account** (its own row). The others get copies with the
same name. That gives you a direct handle: **move or delete the block in the
account it lives in and CalSync follows you**, pushing the change to the copies.
Drag lunch to 11:30 in your personal calendar and all accounts follow. Delete it
on a Tuesday and it does not come back that day.

Editing a copy does nothing: it gets recalculated. Deliberate — if all three
could win, two different edits on the same day would fight with no winner.

Saving a block's settings clears the record of days you removed by hand: the new
rule wins.

---

## What other people read

When a direction is marked visible, colleagues read the event description. Those
texts are editable in *Settings → What other people read*. On private events
Google shows the description to nobody, so it only applies to what you chose to
share.

---

## Which file does what

| File | What is inside |
|---|---|
| `calsync/config.py` | The knobs: how often it checks, how far ahead it looks, timezone, colors |
| `calsync/db.py` | The local database and settings |
| `calsync/google_cal.py` | **All** contact with Google. Nothing else knows Google exists |
| `calsync/rules.py` | Which events deserve to block time, and why |
| `calsync/engine.py` | The busy-block engine: read → plan → apply |
| `calsync/habits.py` | The protected-block engine: where each block fits |
| `calsync/app.py` | The dashboard and the clock |
| `calsync/purge.py` | Cleanup from the terminal, bypassing the app |
| `tests/` | 107 tests that run without touching Google |

Start reading at `engine.py`. It is written to be read in order: how a busy block
is built, then how the decision is made, then how it is applied.

Code comments are in Spanish — the tool was built as a learning project. The
interface, the docs and everything written into your calendars are in English.

```bash
./.venv/bin/python -m pytest tests/ -q
```

The tests cover the parts that are frightening: that a busy block never spawns
another one (three independent safeguards), that moving an event produces a move
and not a duplicate, that guests are never invited by accident, and that the
engine **refuses** to delete an event it did not create.

Anything touching this code must stay compatible with **Python 3.9**, the version
macOS ships:

```bash
vermin -t=3.9 --eval-annotations --backport zoneinfo calsync/ tests/
```

---

## Giving it to someone else

A teammate needs exactly two files, side by side in any folder of their choice:

1. `autostart.command` — from this repository
2. `credentials.json` — the same one everybody uses, sent to them directly.
   **It is not in this repository and must never be.** Send it over Slack or
   1Password, not over email and not in a public link.

**Send it as a .zip.** Right-click `autostart.command` in Finder and choose
Compress, then send the zip. A `.command` file downloaded straight from GitHub or
pasted into a chat loses its executable permission, and double-clicking it then
opens a text editor instead of running. Zipping on a Mac preserves it.

**Warn them about the first double-click.** macOS quarantines anything that
arrived from the internet, so the first attempt shows "cannot be opened because
it is from an unidentified developer". The way through is right-click → Open →
Open. It happens once, per person.

They double-click `autostart.command`. It notices there is no code next to it,
downloads the newest published version from this repository, installs it, and
sets it to start with their Mac. They never install Python by hand, never clone
anything, and never need a GitHub account.

Their Google permissions and their database live on their own Mac, in their own
`~/.calsync/`. Nobody — including you — ever holds anyone else's calendar token.

### Shipping a fix to everyone

1. Change the code here and run the tests.
2. Publish it (GitHub Desktop → **Commit**, then **Push origin**).
3. Tell the team to double-click `autostart.command` and choose **1**.

That is the whole release process. Their copy pulls whatever is published at
that moment; yours keeps using the code in this folder, so you can test before
anyone else sees it.

Once more than a couple of people depend on it, change `BRANCH` at the top of
`autostart.command` from `main` to a tag such as `v1.0`. Then a half-finished
commit cannot reach anybody until you deliberately move the tag.

**The trade-off, stated plainly:** `autostart.command` runs code downloaded from
this repository on your teammates' Macs. If this GitHub account is compromised,
so are they. Turn on two-factor authentication and do not add collaborators you
would not give calendar access to.

---

## Where your data lives

| What | Where |
|---|---|
| Google permissions and database | `~/.calsync/` — readable only by your user |
| App identity | `credentials.json` in this folder |
| Dashboard | `127.0.0.1` — unreachable from the network |

Nothing leaves your Mac except to Google. `credentials.json` and `~/.calsync/`
are never shared or committed — `.gitignore` already excludes them.

---

## Known limits

- **Google does not stop anyone booking over a busy block.** It shows you as
  busy and steers *Find a time* away, but someone determined can still book. The
  block is a signal, not a lock — which is why a visible name helps: people read
  `Lunch` and route around it.
- **The Mac has to be awake.** The price of not depending on a server.
- **Recurring events** are expanded into individual instances inside the window,
  so each instance gets its own block.
- Colombia has no daylight saving, but counterparts abroad do. Everything is
  stored in UTC and shown in local time; no fixed offsets are ever assumed.

## Not built

Buffers between back-to-back meetings, priorities between blocks, a unified
weekly view, and any provider other than Google.
