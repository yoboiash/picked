# Putting Pick'em online (free)

When you're done you'll have a link like `https://my-pickem.onrender.com` that
works on any phone or computer, even with your Mac turned off.

- **GitHub** holds the code.
- **Neon** holds the league data (players, picks, scores).
- **Render** runs the app.

All three are free. The only catch: if nobody has used the app for 15 minutes,
it falls asleep, and the next person waits about a minute while it wakes up.

---

## 1. Put the code on GitHub (about 5 min)

1. Go to **github.com** and click **Sign up**. Make an account and verify your email.
2. Click the **+** in the top-right corner, then **New repository**.
3. Name it `pickem`. Leave it **Public** (or choose Private, either works).
   Don't tick any of the "Add a README" boxes. Click **Create repository**.
4. On the next page, click the link that says **uploading an existing file**.
5. Open the unzipped `pickem` folder in Finder. Select these items and drag them
   into the browser window:
   `app.py`, `espn.py`, `requirements.txt`, `README.md`, `HOSTING.md`,
   `start.command`, and the `static` and `templates` folders.
   - Don't drag `.venv` or `pickem.db` if you see them. Those are only for your Mac.
6. Click **Commit changes**. Your code is now on GitHub.

## 2. Make the database on Neon (about 3 min)

1. Go to **neon.com** and click **Sign up**. Choosing "Continue with GitHub" is easiest.
2. Create a project. Name it `pickem` and pick the region closest to you
   (for Colorado, a US region like **AWS US East** or **US West** is fine).
3. On the project dashboard, click **Connect**. You'll see a connection string that
   starts with `postgresql://`. Click **Copy**.
   - Keep this private. It's the password to your league's data.
4. Paste it somewhere handy (like Notes). You'll need it in the next step.

## 3. Run the app on Render (about 5 min)

1. Go to **render.com** and click **Get Started**. Choose **GitHub** to sign up.
2. Click **New +**, then **Web Service**.
3. Choose **Git Provider → GitHub**, allow Render to see your repositories,
   then pick **pickem**.
4. Fill in the settings:
   - **Name:** whatever you want in the link, e.g. `ashten-pickem`
   - **Language:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** **Free**
5. Under **Environment Variables**, add two:
   | Key | Value |
   | --- | --- |
   | `DATABASE_URL` | the Neon connection string you copied |
   | `SECRET_KEY` | click **Generate** (or type any long random string) |
6. Click **Deploy Web Service**. Wait a few minutes until it says **Live**.
7. Click the link at the top of the page (`https://….onrender.com`).
   **Join the league first** so you become the Commissioner.

## 4. Invite your friends

Text them the `onrender.com` link. They click **Join the league**, pick a name
and PIN, and they're in. They stay signed in for the season.

Tip: on iPhone, open the link in Safari, tap **Share → Add to Home Screen**, and
it shows up like an app.

---

## Updating the app later

When you get new code from Claude: open your `pickem` repository on GitHub,
click **Add file → Upload files**, drag in the changed files, and click
**Commit changes**. Render notices and redeploys on its own in a few minutes.
Your league's data in Neon stays untouched.

## If something goes wrong

- **The page is spinning for a minute:** it's waking up. That's normal.
- **Render says "Deploy failed":** open the **Logs** tab, copy the last red lines,
  and paste them to Claude.
- **"Internal Server Error" on the site:** usually a typo in `DATABASE_URL`.
  In Render, open **Environment**, paste the Neon string again, and save.

---

## Entry fees with Venmo (optional)

Players pay you on Venmo, and the app unlocks their picks on its own by reading
Venmo's "___ paid you $10.00" emails from a Gmail inbox made just for this.

1. **Make a free Gmail** for the league (for example `vignerypickems@gmail.com`) and turn on
   **2-Step Verification** at myaccount.google.com → Security.
2. **Make an app password** at **myaccount.google.com/apppasswords**. Name it `Pickems`
   and copy the 16-letter password.
3. **Send Venmo's payment emails to that Gmail.** Either sign up a Venmo with it, or in
   the email account your Venmo already uses, forward them with a filter:
   From `venmo@venmo.com`, Subject `paid you` → Forward to the new Gmail.
   In the Venmo app, make sure **Settings → Notifications → email** is on for payments.
4. **In Render**, open your service → **Environment** and add:
   | Key | Value |
   | --- | --- |
   | `MAIL_ADDRESS` | the new Gmail address |
   | `MAIL_APP_PASSWORD` | the 16-letter app password |
   Save. Render restarts the app.
5. **In the app**, open **League**, enter your Venmo username and the fee, tick
   **Players must pay before they can make picks**, and save. The page should say
   **Inbox connected**.

How it works: each player gets a code (shown on their Pay page and on your League page).
The Pay button opens Venmo with your username, the fee, and a note like
`Vignery Pick'ems #K7Q2`. When that email arrives, the app credits that player within
about a minute. Payments without a code, or that can't be confirmed as really from Venmo,
show up under **Payments to sort out** for you to assign with one tap. You can also
**Mark paid** anyone who pays in cash.

The app only reads that one inbox. It never deletes or marks anything as read.
To cut off its access, delete the app password at myaccount.google.com/apppasswords.
