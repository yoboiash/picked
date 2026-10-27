# 🏈 Vignery Pick'ems

A little web app you run on your Mac so you and your friends can pick NFL winners every week.

- Everyone makes an account with a name and a PIN
- Pick the winner of every game; each pick **locks at kickoff**
- Schedule, live scores, results and betting lines (spread, over/under, moneyline) load automatically from ESPN's free scoreboard feed
- Each week, one pick on an underdog can be your **2×**, worth double points
- Friends' picks stay hidden until a game kicks off (no copying)
- Season standings, weekly records, and 🏆 for each week won
- **Primetime themes**: Thursday, Sunday and Monday night games switch the app to their own look automatically (commissioners can override it from Theme in the top bar)
- Optional **weekly entry fee on Venmo**: each week is its own pot and the week's winner takes it. Unpaid players can't make that week's picks, paying extra covers the next weeks, and the app unlocks them automatically when Venmo's payment email arrives (setup in HOSTING.md)
- The first person to join is the **Commissioner** (can reset PINs, remove players, force a refresh)

## Start it

1. Unzip the folder somewhere (e.g. your Desktop).
2. Open **Terminal** and run this once so macOS lets you double-click the launcher:
   ```
   chmod +x ~/Desktop/pickem/start.command
   ```
3. Double-click **start.command**. The first run takes a minute to set up, then your browser opens to the app.
   - If macOS says it can't be opened, right-click it → **Open** → **Open**.
4. Click **Join the league** — you'll be the Commissioner.

Leave the Terminal window open while people are using the app. Press **Ctrl+C** in it to stop.

Prefer the command line? From the folder:
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Letting friends in

**Same Wi-Fi:** when the app starts, Terminal prints an address like `http://192.168.1.23:8000`. Send that to friends. If macOS asks whether Python can accept incoming connections, click **Allow**.

**Friends anywhere, without your Mac:** put it online for free with GitHub + Neon + Render. Step-by-step instructions are in **HOSTING.md**.

## How scoring works

- 1 point per correct pick. Wrong picks are losses. A game that ends in a tie counts for nobody.
- **Underdog 2×:** once a week you can tap **2×** on an underdog (the team getting points on the spread). If it wins, that pick is worth 2 points. If it loses, it's just a loss. You can move your 2× to another underdog until the game it's on kicks off. Games with no line yet, or a pick'em line, have no underdog.
- Standings sort by points, then wins, then fewest losses.
- A week is "won" by whoever had the most points once all of that week's games are final.
- **Monday night tiebreaker:** each week, guess the total points in the last game of the week (usually Monday Night Football). If players tie for the most points, the closest guess wins the week. Guesses lock at that game's kickoff and stay hidden from others until then. If it's still tied, or nobody guessed, the players share the week.

## Files

| File | What it does |
| --- | --- |
| `app.py` | The web app: pages, logins, picks, scoring |
| `espn.py` | Downloads the schedule, scores and betting lines |
| `payments.py` | Reads Venmo payment emails from the league's Gmail |
| `templates/` | HTML for each page |
| `static/` | The look: styles, logo, and fonts (Graduate and Barlow, both free under the SIL Open Font License) |
| `pickem.db` | Your league's data when running on your Mac (created on first run) |
| `HOSTING.md` | How to put the app online for free |

Online, the app stores data in Postgres instead (set `DATABASE_URL`). Locally it uses `pickem.db`.

## Settings

Set these as environment variables before starting if you need to change them:

- `PICKEM_SEASON` — season year (default `2026`)
- `PICKEM_PORT` — port number (default `8000`; avoid 5000, which macOS uses for AirPlay)
- `DATABASE_URL` — a Postgres connection string (used when hosted)
- `SECRET_KEY` — keeps people signed in across restarts (used when hosted). Also used to make each player's payment code, so don't change it mid-season.
- `MAIL_ADDRESS` / `MAIL_APP_PASSWORD` — the league Gmail and its app password, for automatic Venmo payments

## Ideas to add next

- Point spreads (pick against the spread)
- Playoff weeks
- Email or text reminders before Sunday kickoff
