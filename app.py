"""
NFL Pick'em — a small Flask web app for picking NFL winners with friends.

Run it with ./start.command (Mac) or:  python3 app.py
Then open http://localhost:8000

On a host like Render, set DATABASE_URL to a Postgres database (e.g. Neon)
and SECRET_KEY to a long random string. Without DATABASE_URL it uses a local
SQLite file, so it still runs on your Mac with no setup.
"""
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

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
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
FETCH = espn.fetch_week            # swapped out in tests


def load_secret_key():
    """Use SECRET_KEY if set (hosting); otherwise keep one in a local file."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(HERE, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = load_secret_key()
app.permanent_session_lifetime = timedelta(days=120)   # stay signed in all season
if os.environ.get("RENDER") or USE_POSTGRES:
    app.config["SESSION_COOKIE_SECURE"] = True        # hosted over https


# ---------------------------------------------------------------- database
PLAYERS_ID = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS players (
    id        {PLAYERS_ID},
    name      TEXT NOT NULL,
    pin_hash  TEXT NOT NULL,
    is_admin  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS players_name ON players (lower(name));
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY, season INTEGER, week INTEGER, kickoff TEXT,
    home TEXT, away TEXT, home_name TEXT, away_name TEXT,
    home_logo TEXT, away_logo TEXT,
    home_score INTEGER, away_score INTEGER,
    state TEXT, detail TEXT, winner TEXT
);
-- Underdog double: one pick per week on an underdog that's worth 2 points
CREATE TABLE IF NOT EXISTS doubles (
    player_id INTEGER NOT NULL REFERENCES players(id),
    season    INTEGER NOT NULL,
    week      INTEGER NOT NULL,
    game_id   TEXT NOT NULL REFERENCES games(id),
    team      TEXT NOT NULL,
    PRIMARY KEY (player_id, season, week)
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


class DB:
    """Tiny wrapper so the same SQL (with ? placeholders) runs on SQLite or Postgres."""

    def __init__(self):
        if USE_POSTGRES:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_POSTGRES:
            sql = re.sub(r"\?", "%s", sql)
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# Columns added after the first version. Existing databases get them on startup,
# so a league that's already running keeps all its players and picks.
NEW_GAME_COLUMNS = {
    "home_color": "TEXT", "away_color": "TEXT",
    "favorite": "TEXT", "spread": "REAL", "over_under": "REAL",
    "home_ml": "INTEGER", "away_ml": "INTEGER",
}


def existing_columns(conn, table):
    if USE_POSTGRES:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,)).fetchall()
        return {r["column_name"] for r in rows}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    conn = DB()
    for statement in SCHEMA.split(";"):
        lines = [ln for ln in statement.splitlines() if not ln.strip().startswith("--")]
        if "".join(lines).strip():
            conn.execute("\n".join(lines))
    have = existing_columns(conn, "games")
    for col, kind in NEW_GAME_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE games ADD COLUMN {col} {kind}")
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute("""INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                 (key, str(value)))
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


def underdog(game):
    """The underdog's abbreviation, or None if there's no line or it's a pick'em."""
    fav = game["favorite"]
    if not fav or not game["spread"]:
        return None
    return game["away"] if fav == game["home"] else game["home"]


def fmt_line(n):
    """3.0 -> '3', 3.5 -> '3.5'."""
    return f"{n:g}" if n is not None else ""


def fmt_ml(n):
    return "" if n is None else (f"+{n}" if n > 0 else str(n))


def current_player():
    pid = session.get("player_id")
    if pid is None:
        return None
    return db().execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()


@app.context_processor
def inject_globals():
    return {"me": current_player(), "season": SEASON, "weeks": WEEKS,
            "underdog": underdog, "fmt_line": fmt_line, "fmt_ml": fmt_ml}


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
    cols = ["id", "season", "week", "kickoff", "home", "away", "home_name",
            "away_name", "home_logo", "away_logo", "home_score", "away_score",
            "state", "detail", "winner"] + list(NEW_GAME_COLUMNS)
    # Once a game kicks off ESPN may drop its line, so keep the last one we saw.
    keep = ", ".join(f"{c}=COALESCE(excluded.{c}, games.{c})" for c in NEW_GAME_COLUMNS)
    for gm in games:
        db().execute(f"""
            INSERT INTO games ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})
            ON CONFLICT(id) DO UPDATE SET
                kickoff=excluded.kickoff, home_score=excluded.home_score,
                away_score=excluded.away_score, state=excluded.state,
                detail=excluded.detail, winner=excluded.winner,
                week=excluded.week, home_logo=excluded.home_logo,
                away_logo=excluded.away_logo, {keep}""",
                     [gm.get(c) for c in cols])
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
SCORED_PICKS_SQL = """
    SELECT p.player_id, g.week, p.team, g.winner, d.team AS dbl
    FROM picks p
    JOIN games g ON g.id = p.game_id
    LEFT JOIN doubles d ON d.player_id = p.player_id AND d.game_id = p.game_id
    WHERE g.season=? AND g.winner IS NOT NULL"""


def pick_points(row):
    """1 point for a correct pick, 2 if it was your underdog double."""
    if row["team"] != row["winner"]:
        return 0
    return 2 if row["dbl"] == row["winner"] else 1


def week_points(week):
    pts = {}
    for r in db().execute(SCORED_PICKS_SQL + " AND g.week=?", (SEASON, week)):
        pts[r["player_id"]] = pts.get(r["player_id"], 0) + pick_points(r)
    return pts


def standings():
    """Return players sorted by season points, with per-week results."""
    players = db().execute("SELECT id, name FROM players ORDER BY name").fetchall()
    rows = db().execute(SCORED_PICKS_SQL, (SEASON,)).fetchall()

    table = {p["id"]: {"name": p["name"], "points": 0, "wins": 0, "losses": 0,
                       "dogs": 0, "weeks": {}} for p in players}
    for r in rows:
        t = table.get(r["player_id"])
        if t is None:
            continue
        wk = t["weeks"].setdefault(r["week"], [0, 0, 0])   # points, wins, losses
        pts = pick_points(r)
        t["points"] += pts
        wk[0] += pts
        if r["team"] == r["winner"]:
            t["wins"] += 1
            wk[1] += 1
            if pts == 2:
                t["dogs"] += 1
        elif r["winner"] != "TIE":   # a tied game counts for nobody
            t["losses"] += 1
            wk[2] += 1

    result = sorted(table.values(),
                    key=lambda t: (-t["points"], -t["wins"], t["losses"], t["name"].lower()))
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

    Most points wins. If several players tie, whoever guessed closest to
    the Monday night total points wins (anyone tied on that shares the week).
    """
    if not week_complete(week):
        return set()
    wins = week_points(week)
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
        """SELECT COUNT(*) total, SUM(CASE WHEN state='post' THEN 1 ELSE 0 END) done
           FROM games WHERE season=? AND week=?""",
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
        player = db().execute("SELECT * FROM players WHERE lower(name)=lower(?)",
                              (name,)).fetchone()
        session.permanent = True

        if mode == "join":
            if player:
                flash("That name is taken. Pick another, or sign in.")
                return redirect(url_for("login"))
            if not (pin.isdigit() and 4 <= len(pin) <= 8):
                flash("PIN must be 4–8 digits.")
                return redirect(url_for("login"))
            first = db().execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 0
            db().execute(
                "INSERT INTO players (name, pin_hash, is_admin) VALUES (?,?,?)",
                (name, generate_password_hash(pin), int(first)))
            db().commit()
            session["player_id"] = db().execute(
                "SELECT id FROM players WHERE lower(name)=lower(?)", (name,)).fetchone()["id"]
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


def get_double(player_id, week):
    return db().execute(
        "SELECT game_id, team FROM doubles WHERE player_id=? AND season=? AND week=?",
        (player_id, SEASON, week)).fetchone()


def save_double(player_id, week, game_id):
    """Set, change or clear a player's underdog double. Returns a message to show."""
    games = {gm["id"]: gm for gm in week_games(week)}
    current = get_double(player_id, week)
    if current and current["game_id"] == game_id:
        # Same game as before: make sure they didn't switch that pick to the favorite.
        gm = games.get(game_id)
        pick = db().execute("SELECT team FROM picks WHERE player_id=? AND game_id=?",
                            (player_id, game_id)).fetchone()
        if gm and not is_locked(gm) and (not pick or pick["team"] != current["team"]):
            db().execute("DELETE FROM doubles WHERE player_id=? AND season=? AND week=?",
                         (player_id, SEASON, week))
            return " Your 2× was removed because you switched off the underdog."
        return ""
    if current and current["game_id"] in games and is_locked(games[current["game_id"]]):
        return " Your 2× game has kicked off, so it's locked in for the week."
    if not game_id:
        if current:
            db().execute("DELETE FROM doubles WHERE player_id=? AND season=? AND week=?",
                         (player_id, SEASON, week))
            return " Removed your 2×."
        return ""

    gm = games.get(game_id)
    if gm is None:
        return ""
    if is_locked(gm):
        return " That game has kicked off, so it can't be your 2×."
    dog = underdog(gm)
    if dog is None:
        return " That game has no underdog yet, so it can't be your 2×."
    pick = db().execute("SELECT team FROM picks WHERE player_id=? AND game_id=?",
                        (player_id, game_id)).fetchone()
    if not pick or pick["team"] != dog:
        return f" Your 2× has to be on the underdog. Pick {dog} in that game to use it."
    db().execute("""INSERT INTO doubles (player_id, season, week, game_id, team)
                    VALUES (?,?,?,?,?) ON CONFLICT (player_id, season, week)
                    DO UPDATE SET game_id = excluded.game_id, team = excluded.team""",
                 (player_id, SEASON, week, game_id, dog))
    return f" 2× on {dog} (+{fmt_line(gm['spread'])})."


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
            db().execute("""INSERT INTO picks (player_id, game_id, team) VALUES (?,?,?)
                            ON CONFLICT (player_id, game_id) DO UPDATE SET team = excluded.team""",
                         (me["id"], game["id"], choice))
            saved += 1
        msg = f"Saved {saved} pick{'s' if saved != 1 else ''}."
        if blocked:
            msg += f" {blocked} game{'s' if blocked != 1 else ''} had already kicked off."
        if "double" in request.form:
            msg += save_double(me["id"], week, request.form.get("double", ""))

        guess = request.form.get("tiebreak", "").strip()
        mnf = monday_game(week_games(week))
        if guess and mnf:
            if is_locked(mnf):
                msg += " Monday night has kicked off, so the tiebreaker is locked."
            elif guess.isdigit() and 0 <= int(guess) <= 200:
                db().execute("""INSERT INTO tiebreakers (player_id, season, week, guess)
                                VALUES (?,?,?,?) ON CONFLICT (player_id, season, week)
                                DO UPDATE SET guess = excluded.guess""",
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
    dbl = get_double(me["id"], week)
    upcoming = [gm["kickoff"] for gm in games if not is_locked(gm)]
    return render_template("picks.html", week=week, games=games, mine=mine,
                           locked={gm["id"]: is_locked(gm) for gm in games},
                           record=record, online=online,
                           points=week_points(week).get(me["id"], 0),
                           picked=sum(1 for gm in games if gm["id"] in mine),
                           mnf=monday_game(games),
                           my_guess=tiebreak_guesses(week).get(me["id"]),
                           double=dbl,
                           double_locked=bool(dbl and any(
                               gm["id"] == dbl["game_id"] and is_locked(gm) for gm in games)),
                           next_kickoff=min(upcoming) if upcoming else None)


@app.route("/week/<int:week>/everyone")
@login_required
def everyone(week):
    """Everyone's picks — only shown for games that have kicked off."""
    if week not in WEEKS:
        abort(404)
    sync_week(week)
    games = week_games(week)
    players = db().execute("SELECT id, name FROM players ORDER BY lower(name)").fetchall()
    grid = {}
    for r in db().execute("""SELECT p.player_id, p.game_id, p.team FROM picks p
                             JOIN games g ON g.id=p.game_id
                             WHERE g.season=? AND g.week=?""", (SEASON, week)):
        grid[(r["player_id"], r["game_id"])] = r["team"]
    totals = week_points(week)
    doubles = {r["player_id"]: r["game_id"] for r in db().execute(
        "SELECT player_id, game_id FROM doubles WHERE season=? AND week=?", (SEASON, week))}
    mnf = monday_game(games)
    mnf_total = None
    if mnf and mnf["state"] == "post":
        mnf_total = (mnf["home_score"] or 0) + (mnf["away_score"] or 0)
    return render_template("everyone.html", week=week, games=games, players=players,
                           grid=grid, totals=totals, doubles=doubles,
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
                db().execute("DELETE FROM doubles WHERE player_id=?", (pid,))
                db().execute("DELETE FROM players WHERE id=?", (pid,))
                db().commit()
                flash("Player removed.")
        elif action == "toggle_admin":
            pid = int(request.form["player_id"])
            if pid != me["id"]:
                db().execute("UPDATE players SET is_admin = 1 - is_admin WHERE id=?", (pid,))
                db().commit()
        return redirect(url_for("admin"))
    players = db().execute("SELECT * FROM players ORDER BY lower(name)").fetchall()
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
    print("\n  🏈  Vignery Pick'ems is running!")
    print(f"  On this Mac:          http://localhost:{PORT}")
    print(f"  Friends on your Wi-Fi: http://{lan_ip}:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
