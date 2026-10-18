"""
NFL Pick'em — a small Flask web app for picking NFL winners with friends.

Run it with ./start.command (Mac) or:  python3 app.py
Then open http://localhost:8000
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import espn

# ---------------------------------------------------------------- settings
SEASON = int(os.environ.get("PICKEM_SEASON", 2026))
WEEKS = range(1, 19)               # NFL regular season weeks 1-18
REFRESH_SECONDS = 5 * 60           # how often a week's scores get re-fetched
LIVE_REFRESH_SECONDS = 60          # ...or this often while games are live
PORT = int(os.environ.get("PICKEM_PORT", 8000))

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PICKEM_DB", os.path.join(HERE, "pickem.db"))
FETCH = espn.fetch_week            # swapped out in tests


def load_secret_key():
    """Keep a random secret in a file so logins survive restarts."""
    path = os.path.join(HERE, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = load_secret_key()


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pin_hash  TEXT NOT NULL,
    is_admin  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY, season INTEGER, week INTEGER, kickoff TEXT,
    home TEXT, away TEXT, home_name TEXT, away_name TEXT,
    home_logo TEXT, away_logo TEXT,
    home_score INTEGER, away_score INTEGER,
    state TEXT, detail TEXT, winner TEXT
);
CREATE TABLE IF NOT EXISTS picks (
    player_id INTEGER NOT NULL REFERENCES players(id),
    game_id   TEXT NOT NULL REFERENCES games(id),
    team      TEXT NOT NULL,
    PRIMARY KEY (player_id, game_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- Monday night tiebreaker: guess the total points scored in the week's last game
CREATE TABLE IF NOT EXISTS tiebreakers (
    player_id INTEGER NOT NULL REFERENCES players(id),
    season    INTEGER NOT NULL,
    week      INTEGER NOT NULL,
    guess     INTEGER NOT NULL,
    PRIMARY KEY (player_id, season, week)
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


def get_setting(key, default=None):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
    db().commit()


# ---------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc)


def parse_time(s):
    # ESPN sends times like "2026-10-18T17:00Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_locked(game):
    """A game locks at kickoff (or as soon as ESPN says it has started)."""
    return game["state"] != "pre" or parse_time(game["kickoff"]) <= now_utc()


def current_player():
    pid = session.get("player_id")
    if pid is None:
        return None
    return db().execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()


@app.context_processor
def inject_globals():
    return {"me": current_player(), "season": SEASON, "weeks": WEEKS}


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_player() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- syncing
def sync_week(week, force=False):
    """Fetch one week from ESPN and save it. Throttled unless force=True."""
    key = f"synced:{SEASON}:{week}"
    last = float(get_setting(key, 0))
    live = db().execute(
        "SELECT 1 FROM games WHERE season=? AND week=? AND state='in'",
        (SEASON, week)).fetchone()
    wait = LIVE_REFRESH_SECONDS if live else REFRESH_SECONDS
    if not force and now_utc().timestamp() - last < wait:
        return True
    try:
        games = FETCH(SEASON, week)
    except Exception as e:  # no internet, feed down, etc.
        app.logger.warning("Could not fetch week %s: %s", week, e)
        return False
    for gm in games:
        db().execute("""
            INSERT INTO games VALUES (:id,:season,:week,:kickoff,:home,:away,
                :home_name,:away_name,:home_logo,:away_logo,:home_score,
                :away_score,:state,:detail,:winner)
            ON CONFLICT(id) DO UPDATE SET
                kickoff=excluded.kickoff, home_score=excluded.home_score,
                away_score=excluded.away_score, state=excluded.state,
                detail=excluded.detail, winner=excluded.winner,
                week=excluded.week""", gm)
    db().commit()
    set_setting(key, now_utc().timestamp())
    return True


def sync_season(force=False):
    return sum(sync_week(w, force) for w in WEEKS)


def current_week():
    """The earliest week that still has unfinished games."""
    row = db().execute(
        "SELECT MIN(week) w FROM games WHERE season=? AND state!='post'",
        (SEASON,)).fetchone()
    if row["w"]:
        return row["w"]
    row = db().execute("SELECT MAX(week) w FROM games WHERE season=?",
                       (SEASON,)).fetchone()
    return row["w"] or 1


def week_games(week):
    return db().execute(
        "SELECT * FROM games WHERE season=? AND week=? ORDER BY kickoff, id",
        (SEASON, week)).fetchall()


@app.before_request
def first_run_sync():
    """Load the whole schedule the first time the app runs."""
    if request.endpoint in ("static", None):
        return
    have = db().execute("SELECT COUNT(*) c FROM games WHERE season=?",
                        (SEASON,)).fetchone()["c"]
    if not have:
        sync_season(force=True)


# ---------------------------------------------------------------- scoring
def standings():
    """Return players sorted by season wins, with per-week records."""
    players = db().execute("SELECT id, name FROM players ORDER BY name").fetchall()
    rows = db().execute("""
        SELECT p.player_id, g.week, p.team, g.winner
        FROM picks p JOIN games g ON g.id = p.game_id
        WHERE g.season=? AND g.winner IS NOT NULL""", (SEASON,)).fetchall()

    table = {p["id"]: {"name": p["name"], "wins": 0, "losses": 0, "weeks": {}}
             for p in players}
    for r in rows:
        t = table.get(r["player_id"])
        if t is None:
            continue
        wk = t["weeks"].setdefault(r["week"], [0, 0])
        if r["team"] == r["winner"]:
            t["wins"] += 1
            wk[0] += 1
        elif r["winner"] != "TIE":   # a tied game counts for nobody
            t["losses"] += 1
            wk[1] += 1

    result = sorted(table.values(), key=lambda t: (-t["wins"], t["losses"], t["name"].lower()))
    # weekly winners: most correct picks, Monday night guess breaks ties
    for t in result:
        t["week_wins"] = 0
    for w in {w for t in table.values() for w in t["weeks"]}:
        for pid in week_winners(w):
            if pid in table:
                table[pid]["week_wins"] += 1
    return result


def monday_game(games):
    """The tiebreaker game: the last game of the week (usually Monday night)."""
    return max(games, key=lambda gm: (gm["kickoff"], gm["id"])) if games else None


def tiebreak_guesses(week):
    return {r["player_id"]: r["guess"] for r in db().execute(
        "SELECT player_id, guess FROM tiebreakers WHERE season=? AND week=?",
        (SEASON, week))}


def week_winners(week):
    """Player ids who won the week. Empty until every game is final.

    Most correct picks wins. If several players tie, whoever guessed closest to
    the Monday night total points wins (anyone tied on that shares the week).
    """
    if not week_complete(week):
        return set()
    wins = {}
    for r in db().execute("""
            SELECT p.player_id, SUM(p.team = g.winner) w
            FROM picks p JOIN games g ON g.id = p.game_id
            WHERE g.season=? AND g.week=? GROUP BY p.player_id""", (SEASON, week)):
        wins[r["player_id"]] = r["w"] or 0
    best = max(wins.values(), default=0)
    if not best:
        return set()
    leaders = {pid for pid, w in wins.items() if w == best}
    if len(leaders) == 1:
        return leaders

    mnf = monday_game(week_games(week))
    actual = (mnf["home_score"] or 0) + (mnf["away_score"] or 0)
    guesses = tiebreak_guesses(week)
    misses = {pid: abs(guesses[pid] - actual) for pid in leaders if pid in guesses}
    if not misses:          # nobody tied made a guess -> they share it
        return leaders
    closest = min(misses.values())
    return {pid for pid, m in misses.items() if m == closest}


def week_complete(week):
    row = db().execute(
        "SELECT COUNT(*) total, SUM(state='post') done FROM games WHERE season=? AND week=?",
        (SEASON, week)).fetchone()
    return row["total"] > 0 and row["total"] == row["done"]


# ---------------------------------------------------------------- routes
@app.route("/")
def home():
    if current_player() is None:
        return redirect(url_for("login"))
    return redirect(url_for("picks", week=current_week()))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        pin = request.form.get("pin", "").strip()
        mode = request.form.get("mode")
        if not name or not pin:
            flash("Enter a name and a PIN.")
            return redirect(url_for("login"))
        player = db().execute("SELECT * FROM players WHERE name=?", (name,)).fetchone()

        if mode == "join":
            if player:
                flash("That name is taken. Pick another, or sign in.")
                return redirect(url_for("login"))
            if not (pin.isdigit() and 4 <= len(pin) <= 8):
                flash("PIN must be 4–8 digits.")
                return redirect(url_for("login"))
            first = db().execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 0
            cur = db().execute(
                "INSERT INTO players (name, pin_hash, is_admin) VALUES (?,?,?)",
                (name, generate_password_hash(pin), int(first)))
            db().commit()
            session["player_id"] = cur.lastrowid
            if first:
                flash("Welcome, Commissioner! You're the first player, so you run the league.")
            return redirect(url_for("home"))

        if not player or not check_password_hash(player["pin_hash"], pin):
            flash("Wrong name or PIN.")
            return redirect(url_for("login"))
        session["player_id"] = player["id"]
        return redirect(request.args.get("next") or url_for("home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/week/<int:week>", methods=["GET", "POST"])
@login_required
def picks(week):
    if week not in WEEKS:
        abort(404)
    me = current_player()

    if request.method == "POST":
        saved = blocked = 0
        for game in week_games(week):
            choice = request.form.get(f"pick_{game['id']}")
            if choice not in (game["home"], game["away"]):
                continue
            if is_locked(game):
                blocked += 1
                continue
            db().execute("INSERT OR REPLACE INTO picks VALUES (?,?,?)",
                         (me["id"], game["id"], choice))
            saved += 1
        msg = f"Saved {saved} pick{'s' if saved != 1 else ''}."
        if blocked:
            msg += f" {blocked} game{'s' if blocked != 1 else ''} had already kicked off."

        guess = request.form.get("tiebreak", "").strip()
        mnf = monday_game(week_games(week))
        if guess and mnf:
            if is_locked(mnf):
                msg += " Monday night has kicked off, so the tiebreaker is locked."
            elif guess.isdigit() and 0 <= int(guess) <= 200:
                db().execute("INSERT OR REPLACE INTO tiebreakers VALUES (?,?,?,?)",
                             (me["id"], SEASON, week, int(guess)))
                msg += f" Tiebreaker: {int(guess)} total points."
            else:
                msg += " Tiebreaker must be a number from 0 to 200."
        db().commit()
        flash(msg)
        return redirect(url_for("picks", week=week))

    online = sync_week(week)
    games = week_games(week)
    mine = {r["game_id"]: r["team"] for r in db().execute(
        "SELECT game_id, team FROM picks WHERE player_id=?", (me["id"],))}
    record = [0, 0]
    for gm in games:
        if gm["winner"] and gm["id"] in mine and gm["winner"] != "TIE":
            record[0 if mine[gm["id"]] == gm["winner"] else 1] += 1
    return render_template("picks.html", week=week, games=games, mine=mine,
                           locked={gm["id"]: is_locked(gm) for gm in games},
                           record=record, online=online,
                           picked=sum(1 for gm in games if gm["id"] in mine),
                           mnf=monday_game(games),
                           my_guess=tiebreak_guesses(week).get(me["id"]))


@app.route("/week/<int:week>/everyone")
@login_required
def everyone(week):
    """Everyone's picks — only shown for games that have kicked off."""
    if week not in WEEKS:
        abort(404)
    sync_week(week)
    games = week_games(week)
    players = db().execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE").fetchall()
    grid = {}
    for r in db().execute("""SELECT p.player_id, p.game_id, p.team FROM picks p
                             JOIN games g ON g.id=p.game_id
                             WHERE g.season=? AND g.week=?""", (SEASON, week)):
        grid[(r["player_id"], r["game_id"])] = r["team"]
    totals = {}
    for pl in players:
        totals[pl["id"]] = sum(1 for gm in games
                               if gm["winner"] and grid.get((pl["id"], gm["id"])) == gm["winner"])
    mnf = monday_game(games)
    mnf_total = None
    if mnf and mnf["state"] == "post":
        mnf_total = (mnf["home_score"] or 0) + (mnf["away_score"] or 0)
    return render_template("everyone.html", week=week, games=games, players=players,
                           grid=grid, totals=totals,
                           locked={gm["id"]: is_locked(gm) for gm in games},
                           me_id=current_player()["id"],
                           mnf=mnf, mnf_locked=bool(mnf and is_locked(mnf)),
                           mnf_total=mnf_total, guesses=tiebreak_guesses(week),
                           winners=week_winners(week))


@app.route("/standings")
@login_required
def leaderboard():
    played = [w for w in WEEKS if db().execute(
        "SELECT 1 FROM games WHERE season=? AND week=? AND winner IS NOT NULL",
        (SEASON, w)).fetchone()]
    return render_template("standings.html", table=standings(), played=played)


@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    me = current_player()
    if not me["is_admin"]:
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "refresh":
            n = sync_season(force=True)
            flash(f"Refreshed {n} of {len(WEEKS)} weeks from ESPN."
                  if n else "Couldn't reach ESPN — check the internet connection.")
        elif action == "reset_pin":
            pid = int(request.form["player_id"])
            pin = request.form.get("pin", "")
            if pin.isdigit() and 4 <= len(pin) <= 8:
                db().execute("UPDATE players SET pin_hash=? WHERE id=?",
                             (generate_password_hash(pin), pid))
                db().commit()
                flash("PIN updated.")
            else:
                flash("PIN must be 4–8 digits.")
        elif action == "remove":
            pid = int(request.form["player_id"])
            if pid != me["id"]:
                db().execute("DELETE FROM picks WHERE player_id=?", (pid,))
                db().execute("DELETE FROM tiebreakers WHERE player_id=?", (pid,))
                db().execute("DELETE FROM players WHERE id=?", (pid,))
                db().commit()
                flash("Player removed.")
        elif action == "toggle_admin":
            pid = int(request.form["player_id"])
            if pid != me["id"]:
                db().execute("UPDATE players SET is_admin = 1 - is_admin WHERE id=?", (pid,))
                db().commit()
        return redirect(url_for("admin"))
    players = db().execute("SELECT * FROM players ORDER BY name COLLATE NOCASE").fetchall()
    games = db().execute("SELECT COUNT(*) c FROM games WHERE season=?", (SEASON,)).fetchone()["c"]
    return render_template("admin.html", players=players, game_count=games)


# ---------------------------------------------------------------- start
init_db()

if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = "your-mac's-ip"
    print("\n  🏈  NFL Pick'em is running!")
    print(f"  On this Mac:          http://localhost:{PORT}")
    print(f"  Friends on your Wi-Fi: http://{lan_ip}:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
