# Step 1 — Google credentials

This is the only part nobody can do for you: Google requires a human to click.
About ten minutes. Do it **with a personal Gmail account**, not a work one — that
way the project is yours and no organisation admin can switch it off.

You end up with a file called `credentials.json`. That file is the key.

---

## 1. Create the project

Go to [console.cloud.google.com](https://console.cloud.google.com) with your
personal Gmail.

Top left, in the project selector → **New project**. Name it `CalSync` and create
it. If it asks you to set up billing, **skip it** — nothing here costs money.

Wait a few seconds and make sure the selector at the top says `CalSync`.

## 2. Enable the Calendar API

Side menu (☰) → **APIs & Services** → **Library**.

Search for `Google Calendar API`, open it, and click **Enable**.

## 3. Configure the consent screen

Side menu → **APIs & Services** → **OAuth consent screen** → **Get started**.

Fill the wizard in:

| Field | Value |
|---|---|
| App name | `CalSync` |
| User support email | your personal Gmail |
| **Audience / User type** | **External** ← important |
| Contact information | your personal Gmail |

**Why External and not Internal?** Because your accounts live in different
organisations. Internal would only work if every account belonged to the same
Workspace — and it would then refuse your personal Gmail.

Accept the terms and create.

## 4. Publish the app ← THE CRITICAL STEP

Go to the **Audience** tab and find the **Publish app** button. Click it and
confirm.

Check that the status now reads **In production**, not *Testing*.

**Why this matters so much:** apps left in Testing have their permission killed
by Google every 7 days, so CalSync would shut down every week asking you to
reconnect every account. Published, the permission does not expire. It is the
difference between a tool and a weekly chore.

Do not be put off by the word "publish": the app stays private and unverified.
Nobody will find it or use it.

## 5. Declare the permission it needs

In the **Data Access** tab → **Add or remove scopes**.

Find and tick: `https://www.googleapis.com/auth/calendar`

Google will flag it as a **sensitive scope** and show a **Verification required**
notice:

> *A sensitive scope was added. To verify your app, it will need to go through
> the verification process, otherwise users will see an unverified app screen.*

**Ignore it and save.** Do not start the verification process.

That notice is correct but does not apply here. Verification exists for apps
asking strangers for their data: it requires a published privacy policy, a demo
video and several weeks of review. You are the only user of this app, and the
"sensitive data" it touches is your own calendars.

The only consequence of not verifying is what the warning says at the end: you
will see an "unverified app" screen when connecting each account. Once per
account, then never again. A sensitive scope **does** work unverified; what Google
blocks without verification are *restricted* scopes (Gmail, full Drive), and
`calendar` is not one of those.

## 6. Create the client

**Clients** tab → **Create client**.

| Field | Value |
|---|---|
| Application type | **Desktop app** |
| Name | `CalSync local` |

Create it. A window appears with the Client ID → click **Download JSON**.

## 7. Put the file in place

The downloaded file is called something like
`client_secret_1234-abcd.apps.googleusercontent.com.json`.

Rename it to exactly **`credentials.json`** and move it into the CalSync folder,
next to `autostart.command`.

Done. Go back to the README and carry on.

---

## What to expect afterwards

When you connect each account, Google will show a screen saying **"Google hasn't
verified this app"**. Normal and expected: verification is a process for public
apps with thousands of users, and this one is only used by you. Get past it with
**Advanced → Go to CalSync (unsafe)**.

If connecting a work account gives you an error like *"access blocked by your
administrator"*, you did nothing wrong: that organisation restricts third-party
apps. Options, in order of least effort:

1. Ask the Workspace admin to add your Client ID to the trusted app list. It is a
   small, auditable exception, not a broad permission.
2. If they refuse, that account can still be read-only via its private iCal URL:
   its events would block time in your other calendars, but not the other way
   round. Partial, not fatal.

## A note on security

`credentials.json` identifies the app, not you — on its own it grants access to
nothing. The actual permissions live in `~/.calsync/`, only on your disk. Even
so, do not put it on GitHub or share it; the project already excludes it via
`.gitignore`.

## If several people at your company will use this

Each person can follow this guide and create their own project, which keeps
everything independent. Or one person creates the project once and shares the
`credentials.json` with the team, which is allowed — with one ceiling worth
knowing: **an unverified published app is capped at 100 accounts** that have ever
granted consent. Three accounts each means roughly 33 people. Past that, or to
remove the warning screen, the app has to go through Google verification.
