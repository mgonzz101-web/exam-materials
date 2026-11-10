# Exam Materials Inventory

A phone-friendly web app for tracking exam **record forms** and **response booklets**.
People scan a QR code, pick materials, and tap **Complete Checkout** (free — it just records what
was taken). Stock drops automatically, the owner dashboard updates, and low-stock emails go out.

* Checkout page (the QR code opens this): `/`
* Owner dashboard: `/admin`
* First-time setup: `/setup`

## What you need (all have free plans)

| Piece | Service I'd use | Cost / limits (checked Nov 2026 — confirm on their pricing pages) |
|---|---|---|
| App hosting | **Render** web service | Free: 750 instance-hours/month; **sleeps after 15 min idle, ~1 min to wake**. Paid "Starter" stays awake. |
| Database (permanent) | **Neon** Postgres | Free: 1 GB storage, 100 compute-hours/month, auto-suspends after 5 min idle (data is kept). |
| Alert email | **Brevo** (or Resend) | Brevo free: 300 emails/day. Resend free: 100/day, 3,000/month. |

Important findings:

* **Do not use Render's free Postgres** — it expires after 30 days. Use Neon (it did not list an expiry).
* **Render's free web services block SMTP ports (25/465/587)**, so email must go through an HTTPS API
  (Brevo or Resend). That is how this app is set up.
* Free-tier sleep means the *first* scan after a quiet period takes about a minute. To avoid that,
  upgrade the Render service (about $7/month) or ping `https://YOUR-SITE/healthz` every 10 minutes with a free
  uptime monitor (one always-on free service fits within 750 hours).

## Deploy (about 20 minutes)

**1. Database (Neon).** Create a free project at neon.com → copy the connection string
(`postgresql://…?sslmode=require`). The app creates its own tables on first start.

**2. Code.** Put this folder in a GitHub repository.

**3. Hosting (Render).** New → Web Service → connect the repo (or use the included `render.yaml`).
Set these environment variables:

| Variable | Value |
|---|---|
| `DATABASE_URL` | your Neon connection string |
| `SETUP_KEY` | a long random phrase (required once, to create the owner account) |
| `PUBLIC_URL` | optional: your final address, e.g. `https://materials.yourschool.org` |

Start command: `gunicorn app:app --workers 2 --threads 4 --bind 0.0.0.0:$PORT`

**4. Email alerts — what YOU must supply.** Pick one:

*Brevo (recommended for no domain):* create a free account → Senders → add and verify the address you
want alerts to come from → create an API key. Set `BREVO_API_KEY` and `MAIL_FROM` (the verified address).
*Resend:* create an API key and set `RESEND_API_KEY` and `MAIL_FROM`. I could not confirm from Resend's
public pages whether a verified domain is required to send to arbitrary recipients — check
their dashboard before relying on it.
*Plain SMTP (not on Render free):* `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`.

Until this is set, low stock is still flagged on the dashboard, and the alert log shows "Not sent".

**5. First-time setup.** Open `https://YOUR-SITE/setup`:
1. Create the owner account (enter your `SETUP_KEY`).
2. The dashboard shows a checklist: add inventory (or **Load sample inventory** to try it), set alert
   recipients in **Settings** and press **Send a test email**, then download the **QR code**.

**Print the QR code only after the site is on its final address.** The code is just that web address.
If you move later, point the same address at the new site and old printouts keep working
(a custom domain makes that easy).

## How it behaves

* **Cart doesn't touch stock.** Inventory only drops when Complete Checkout succeeds.
* **Safe under simultaneous use.** The whole cart is one database transaction and each item is deducted with
  "only if enough remains", so stock can't go negative. If something ran out, nothing is taken and the person
  sees exactly what changed and can adjust their cart.
* **No double deductions.** Each checkout attempt has a one-time token; double-taps, retries and refreshing the
  confirmation re-show the same receipt.
* **Reverse/correct** (owner only) restores or changes quantities exactly once.
* **Audit log** records every stock change: item, change, before/after, time, person, reason or checkout.
* **Alerts:** one email when an item reaches its own alert level (covers checkouts *and* manual changes; one email
  can list several items). No repeats while it stays low; it re-arms after restocking above the level. Newly
  created items already below their level are flagged but not emailed.
* **Permissions** are enforced on the server. People can check out with just a name and email, or optionally create
  an account (Settings can require sign-in). Owners can add other owners/users in Settings → Accounts.
* Quantities count individual forms/booklets; if you buy packs, enter the number of sheets, not packs.

## Run on your own computer (testing)

```
pip install flask requests
SETUP_KEY=anything-long python app.py        # http://localhost:5000/setup  (SQLite file in ./data)
python -m unittest discover -s tests -v      # 36 automated tests
```

## Honest limitations

* The test suite runs against SQLite. The Postgres code path uses the same SQL and standard row-level locking, but
  I could not run it here (no Postgres driver or network), so do your own quick checkout test after deploying.
* Brevo/Resend/SMTP sending was tested against fakes (payload shape, SMTP delivery), not live accounts.
* Login throttling is per server process; fine for this scale.
* Back up: Neon's free plan has limited history, so use **Export CSV** (Dashboard / Settings) periodically.
