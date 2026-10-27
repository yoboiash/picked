"""
NFL Pick'em — a small Flask web app for picking NFL winners with friends.

Run it with ./start.command (Mac) or:  python3 app.py
Then open http://localhost:8000

On a host like Render, set DATABASE_URL to a Postgres database (e.g. Neon)
and SECRET_KEY to a long random string. Without DATABASE_URL it uses a local
SQLite file, so it still runs on your Mac with no setup.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import espn
import payments

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
MAIL_ADDRESS = os.environ.get("MAIL_ADDRESS", "").strip()
MAIL_APP_PASSWORD = os.environ.get("MAIL_APP_PASSWORD", "").replace(" ", "")
FETCH_PAYMENTS = payments.fetch_payments   # swapped out in tests
INBOX_CHECK_SECONDS = 60
# Entry fees used to be one payment per season. When the app switched to weekly
# pots, the payments already made were that week's pot (already paid out).
OLD_PAYMENTS_WEEK = int(os.environ.get("PICKEM_OLD_PAYMENTS_WEEK", 7))


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
-- Entry fees: one row per payment (from Venmo emails, or marked by the commissioner)
CREATE TABLE IF NOT EXISTS payments (
    id        {PLAYERS_ID},
    player_id INTEGER NOT NULL REFERENCES players(id),
    season    INTEGER NOT NULL,
    amount    REAL NOT NULL,
    source    TEXT NOT NULL,
    ref       TEXT,
    created   TEXT
);
-- Every Venmo email the app has read, so nothing is counted twice
CREATE TABLE IF NOT EXISTS venmo_inbox (
    msg_id    TEXT PRIMARY KEY,
    season    INTEGER,
    payer     TEXT,
    amount    REAL,
    code      TEXT,
    note      TEXT,
    verified  INTEGER,
    status    TEXT,
    player_id INTEGER,
    received  TEXT
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
    if "week" not in existing_columns(conn, "payments"):
        # Switching to weekly pots: every payment made so far was for one week,
        # and that pot has already been paid out.
        conn.execute("ALTER TABLE payments ADD COLUMN week INTEGER")
        old = conn.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"]
        conn.execute("UPDATE payments SET week=? WHERE week IS NULL", (OLD_PAYMENTS_WEEK,))
        if old:
            conn.execute("""INSERT INTO settings (key, value) VALUES (?, '1')
                            ON CONFLICT (key) DO NOTHING""",
                         (f"payout:{SEASON}:{OLD_PAYMENTS_WEEK}",))
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


@app.template_filter("ordinal")
def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 11 -> '11th'."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def current_player():
    pid = session.get("player_id")
    if pid is None:
        return None
    return db().execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()


@app.context_processor
def inject_globals():
    me = current_player()
    pay = pay_settings()
    return {"me": me, "season": SEASON, "weeks": WEEKS, "this_week": current_week,
            "underdog": underdog, "fmt_line": fmt_line, "fmt_ml": fmt_ml,
            "pay": pay, "fmt_money": fmt_money,
            "me_paid": bool(me and pay["required"] and is_paid(me["id"])),
            "pay_week": current_week,
            "theme": theme_state(), "theme_names": THEME_NAMES}


# ---------------------------------------------------------------- entry fees
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I mix-ups


def pay_settings():
    fee = get_setting("pay_fee", "10")
    try:
        fee = float(fee)
    except ValueError:
        fee = 10.0
    return {"required": get_setting("pay_required", "0") == "1",
            "fee": fee,
            "venmo": get_setting("venmo_user", ""),
            "inbox": bool(MAIL_ADDRESS and MAIL_APP_PASSWORD),
            "inbox_address": MAIL_ADDRESS}


def fmt_money(n):
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


def pay_code(player_id):
    """A short code (like K7Q2) that ties a Venmo payment to one player."""
    digest = hmac.new(app.secret_key.encode(), f"pay:{SEASON}:{player_id}".encode(),
                      hashlib.sha256).digest()
    return "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in digest[:4])


def pay_note(player_id):
    return f"Vignery Pick'ems #{pay_code(player_id)}"


def venmo_link(player_id):
    s = pay_settings()
    user = s["venmo"].lstrip("@")
    if not user:
        return None
    amount = f"{s['fee']:g}"
    return (f"https://venmo.com/{quote(user)}?txn=pay&amount={amount}"
            f"&note={quote(pay_note(player_id))}")


def paid_amount(player_id, week=None):
    """What a player has paid toward one week's entry (this week by default)."""
    week = week or current_week()
    row = db().execute("""SELECT COALESCE(SUM(amount), 0) s FROM payments
                          WHERE player_id=? AND season=? AND week=?""",
                       (player_id, SEASON, week)).fetchone()
    return float(row["s"] or 0)


def is_paid(player_id, week=None):
    return paid_amount(player_id, week) >= pay_settings()["fee"] - 0.001


def can_pick(player, week=None):
    return not pay_settings()["required"] or is_paid(player["id"], week)


def paid_through(player_id):
    """The last week in a row a player has paid for, starting this week (None if unpaid)."""
    last = None
    for w in WEEKS:
        if w < current_week():
            continue
        if not is_paid(player_id, w):
            break
        last = w
    return last


def pot(week=None):
    """(total, number of players) paid into one week's pot."""
    week = week or current_week()
    row = db().execute("""SELECT COALESCE(SUM(amount), 0) s, COUNT(DISTINCT player_id) n
                          FROM payments WHERE season=? AND week=?""", (SEASON, week)).fetchone()
    return float(row["s"] or 0), int(row["n"] or 0)


def add_payment(player_id, amount, source, ref=None, week=None):
    db().execute("""INSERT INTO payments (player_id, season, amount, source, ref, created, week)
                    VALUES (?,?,?,?,?,?,?)""",
                 (player_id, SEASON, round(amount, 2), source, ref, now_utc().isoformat(),
                  week or current_week()))


def week_at(when):
    """The pick'em week a moment belongs to: the first week whose last game
    hadn't finished yet (a game is over about 4 hours after kickoff)."""
    rows = db().execute("""SELECT week, MAX(kickoff) last FROM games WHERE season=?
                           GROUP BY week ORDER BY week""", (SEASON,)).fetchall()
    for r in rows:
        if parse_time(r["last"]) + timedelta(hours=4) > when:
            return r["week"]
    return rows[-1]["week"] if rows else 1


def credit(player_id, amount, source, ref=None, start=None):
    """Put a payment toward the week's entry. Anything extra rolls into the
    following unpaid weeks, so $20 covers two weeks."""
    fee = pay_settings()["fee"]
    start = start or current_week()
    left = amount
    for w in WEEKS:
        if w < start or left <= 0.001:
            continue
        owed = fee - paid_amount(player_id, w)
        if owed <= 0.001:
            continue
        take = left if w == WEEKS[-1] else min(owed, left)
        add_payment(player_id, take, source, ref, w)
        left -= take
    if left > 0.001:                   # paid for every week already: keep it on the last one
        add_payment(player_id, left, source, ref, WEEKS[-1])


def entrants(week):
    """Players who paid that week's entry in full."""
    fee = pay_settings()["fee"]
    return {r["player_id"] for r in db().execute(
        """SELECT player_id, SUM(amount) s FROM payments WHERE season=? AND week=?
           GROUP BY player_id""", (SEASON, week)) if float(r["s"]) >= fee - 0.001}


def pot_winners(week):
    """Who takes that week's pot: the week winner among players who paid in."""
    paid = entrants(week)
    return week_winners(week, among=paid) if paid else set()


def pot_history():
    """Every week that had a pot: amount, winners and whether it's been paid out."""
    names = {p["id"]: p["name"] for p in db().execute("SELECT id, name FROM players")}
    out = []
    for r in db().execute("""SELECT week, SUM(amount) s, COUNT(DISTINCT player_id) n
                             FROM payments WHERE season=? GROUP BY week ORDER BY week DESC""",
                          (SEASON,)).fetchall():
        w = r["week"]
        done = week_complete(w)
        out.append({"week": w, "total": float(r["s"]), "count": int(r["n"]), "done": done,
                    "winners": sorted((names.get(i, "?") for i in pot_winners(w)), key=str.lower)
                    if done else [],
                    "paid_out": get_setting(f"payout:{SEASON}:{w}") == "1"})
    return out


def payment_week(received):
    """The week a Venmo payment counts toward, from when it was sent."""
    try:
        when = datetime.fromisoformat(received)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return current_week()
    return week_at(when)


def check_inbox(force=False):
    """Read new Venmo emails and credit the matching players.

    Returns (ok, message). Throttled to once a minute unless force=True.
    """
    if not (MAIL_ADDRESS and MAIL_APP_PASSWORD):
        return False, "The payment inbox isn't connected yet."
    last = float(get_setting("inbox_checked", 0))
    gap = 15 if force else INBOX_CHECK_SECONDS
    if now_utc().timestamp() - last < gap:
        return True, ""
    set_setting("inbox_checked", now_utc().timestamp())
    seen = {r["msg_id"] for r in db().execute("SELECT msg_id FROM venmo_inbox")}
    try:
        found = FETCH_PAYMENTS(MAIL_ADDRESS, MAIL_APP_PASSWORD, date(SEASON, 8, 1), seen)
    except Exception as e:
        app.logger.warning("Payment inbox check failed: %s", e)
        set_setting("inbox_error", str(e)[:200])
        return False, "Couldn't open the payment inbox. Check MAIL_ADDRESS and MAIL_APP_PASSWORD."
    set_setting("inbox_error", "")
    codes = {pay_code(p["id"]): p["id"] for p in db().execute("SELECT id FROM players")}
    credited = 0
    for p in found:
        pid = codes.get(p["code"]) if p["code"] else None
        status = "matched" if (pid and p["verified"]) else "unmatched"
        cur = db().execute("""INSERT INTO venmo_inbox (msg_id, season, payer, amount, code, note,
                            verified, status, player_id, received)
                        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (msg_id) DO NOTHING""",
                     (p["msg_id"], SEASON, p["payer"], p["amount"], p["code"], p["note"],
                      int(p["verified"]), status, pid if status == "matched" else None,
                      p["received"]))
        if status == "matched" and cur.rowcount == 1:   # skip if another request got it first
            credit(pid, p["amount"], "venmo", p["msg_id"], start=payment_week(p["received"]))
            credited += 1
    db().commit()
    return True, f"Found {credited} new payment{'s' if credited != 1 else ''}." if found else ""


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
    if request.endpoint in ("static", "health", None):
        return
    have = db().execute("SELECT COUNT(*) c FROM games WHERE season=?",
                        (SEASON,)).fetchone()["c"]
    if not have:
        sync_season(force=True)


WEEK_PAGES = ("picks", "everyone")


def came_from_inside_app():
    """True when the click came from a page of this app, not a bookmark,
    home-screen shortcut, typed address or a link texted from somewhere else."""
    site = request.headers.get("Sec-Fetch-Site")
    if site:                                  # every current browser sends this
        return site == "same-origin"
    ref = request.headers.get("Referer", "")
    return bool(ref) and ref.split("/")[2:3] == [request.host]


@app.before_request
def old_week_links_go_home():
    """Old links to a week page (/week/5, /week/5/everyone) open the homepage instead."""
    if (request.method == "GET" and request.endpoint in WEEK_PAGES
            and not came_from_inside_app()):
        return redirect(url_for("home"))


# ---------------------------------------------------------------- primetime themes
# Thursday, Sunday and Monday night games get their own look. The NFL slots
# games in Eastern time, so that's how the nights are worked out.
ET = ZoneInfo("America/New_York")
THEME_NAMES = {"regular": "Regular", "thursday": "Thursday Night",
               "sunday": "Sunday Night", "monday": "Monday Night"}
THEME_CHOICES = ["auto"] + list(THEME_NAMES)
MORNING = 6            # a Thursday/Monday theme comes on at 6 AM Eastern on game day


def kickoff_et(game):
    return parse_time(game["kickoff"]).astimezone(ET)


def primetime_games(games):
    """This week's primetime games by night: {'thursday': [..], 'sunday': [..], 'monday': [..]}."""
    nights = {"thursday": [], "sunday": [], "monday": []}
    for gm in sorted(games, key=lambda g: (g["kickoff"], g["id"])):
        k = kickoff_et(gm)
        if k.weekday() == 3:
            nights["thursday"].append(gm)
        elif k.weekday() == 6 and k.hour >= 19:     # the 8:20 PM Eastern game
            nights["sunday"].append(gm)
        elif k.weekday() == 0:
            nights["monday"].append(gm)
    return {n: g for n, g in nights.items() if g}


def featured_game(night_games):
    """The game to show: the one being played, else the next one, else the last."""
    for state in ("in", "pre"):
        for gm in night_games:
            if gm["state"] == state:
                return gm
    return night_games[-1]


def auto_theme(games, now=None):
    """Which primetime theme should be on right now, or None for the regular look."""
    now = now or now_utc()
    prime = primetime_games(games)
    for night in ("thursday", "monday"):
        gms = prime.get(night)
        if not gms:
            continue
        start = kickoff_et(gms[0]).replace(hour=MORNING, minute=0, second=0, microsecond=0)
        end = parse_time(gms[-1]["kickoff"]) + timedelta(hours=6)   # in case scores go stale
        if start <= now < end and any(gm["state"] != "post" for gm in gms):
            return night
    snf = prime.get("sunday")
    if snf:
        first = parse_time(snf[0]["kickoff"])
        day = kickoff_et(snf[0]).date()
        afternoon = [gm for gm in games if kickoff_et(gm).date() == day
                     and parse_time(gm["kickoff"]) < first]
        if (first - timedelta(hours=8) <= now < parse_time(snf[-1]["kickoff"]) + timedelta(hours=6)
                and all(gm["state"] == "post" for gm in afternoon)
                and any(gm["state"] != "post" for gm in snf)):
            return "sunday"
    return None


def theme_state():
    """The look for this page: the commissioner's choice, or the automatic one."""
    if "theme" not in g:
        choice = get_setting("theme", "auto")
        if choice not in THEME_CHOICES:
            choice = "auto"
        games = week_games(current_week())
        auto = auto_theme(games)
        name = auto if choice == "auto" else (None if choice == "regular" else choice)
        night = primetime_games(games).get(name) if name else None
        g.theme = {"choice": choice, "auto": auto, "name": name,
                   "games": night or [], "game": featured_game(night) if night else None}
    return g.theme


def live_points(week):
    """Each player's points for the week if every game being played ended right now."""
    pts = dict(week_points(week))
    for r in db().execute(
            """SELECT p.player_id, p.team, g.home, g.away, g.home_score hs, g.away_score aws,
                      d.team AS dbl
               FROM picks p JOIN games g ON g.id = p.game_id
               LEFT JOIN doubles d ON d.player_id = p.player_id AND d.game_id = p.game_id
               WHERE g.season=? AND g.week=? AND g.state='in'""", (SEASON, week)):
        hs, aws = r["hs"] or 0, r["aws"] or 0
        if hs == aws:
            continue
        leader = r["home"] if hs > aws else r["away"]
        if r["team"] == leader:
            pts[r["player_id"]] = pts.get(r["player_id"], 0) + (2 if r["dbl"] == leader else 1)
    return pts


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


def week_winners(week, among=None):
    """Player ids who won the week. Empty until every game is final.

    Most points wins. If several players tie, whoever guessed closest to
    the Monday night total points wins (anyone tied on that shares the week).
    Pass `among` to only count some players (like those who paid into the pot).
    """
    if not week_complete(week):
        return set()
    wins = week_points(week)
    if among is not None:
        wins = {pid: w for pid, w in wins.items() if pid in among}
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
def leaders(table, n=3):
    """The top n spots in the standings (players tied on points share a rank)."""
    out, rank, prev = [], 0, None
    for i, t in enumerate(table, 1):
        if not t["points"]:
            break
        if t["points"] != prev:
            rank, prev = i, t["points"]
        if rank > n:
            break
        out.append(dict(t, rank=rank))
    return out


def last_week_result():
    """The latest finished week: who won it, their points, and the pot they took."""
    names = {p["id"]: p["name"] for p in db().execute("SELECT id, name FROM players")}
    row = db().execute("SELECT MAX(week) w FROM games WHERE season=? AND winner IS NOT NULL",
                       (SEASON,)).fetchone()
    for w in range(row["w"] or 0, 0, -1):
        if not week_complete(w):
            continue
        total, _ = pot(w)
        ids = pot_winners(w) if total else week_winners(w)
        pts = week_points(w)
        return {"week": w, "pot": total,
                "names": sorted((names[i] for i in ids if i in names), key=str.lower),
                "points": max((pts.get(i, 0) for i in ids), default=0)}
    return None


@app.route("/")
def home():
    """The front page: this week at a glance, your status and the leaderboard."""
    me = current_player()
    week = current_week()
    online = sync_week(week)
    games = week_games(week)
    open_games = [gm for gm in games if not is_locked(gm)]
    upcoming = [gm["kickoff"] for gm in open_games]
    table = standings()
    total, count = pot()
    ctx = dict(week=week, online=online, games=games,
               open_count=len(open_games), next_kickoff=min(upcoming) if upcoming else None,
               first_kickoff=min((gm["kickoff"] for gm in games), default=None),
               top=leaders(table), last=last_week_result(),
               player_count=len(table), pot_total=total, pot_count=count)
    if me:
        mine = {r["game_id"]: r["team"] for r in db().execute(
            """SELECT p.game_id, p.team FROM picks p JOIN games g ON g.id=p.game_id
               WHERE p.player_id=? AND g.season=? AND g.week=?""", (me["id"], SEASON, week))}
        mnf = monday_game(games)
        my_row = next((t for t in table if t["name"] == me["name"]), None)
        rank = None
        if my_row and my_row["points"]:
            rank = 1 + sum(1 for t in table if t["points"] > my_row["points"])
        ctx.update(
            picked=len(mine),
            missing=sum(1 for gm in open_games if gm["id"] not in mine),
            double=get_double(me["id"], week),
            dogs_left=any(underdog(gm) for gm in open_games),
            mnf=mnf, mnf_open=bool(mnf and not is_locked(mnf)),
            my_guess=tiebreak_guesses(week).get(me["id"]),
            my_points=week_points(week).get(me["id"], 0),
            my_row=my_row, my_rank=rank,
            paywall=not can_pick(me, week),
            paid_thru=paid_through(me["id"]))
    ctx["prime"] = primetime_panel(me, week, games, table)
    return render_template("home.html", **ctx)


@app.route("/theme", methods=["POST"])
@login_required
def set_theme():
    """Commissioners can force a theme, or put it back on automatic."""
    if not current_player()["is_admin"]:
        abort(403)
    choice = request.form.get("theme", "auto")
    if choice in THEME_CHOICES:
        set_setting("theme", choice)
        g.pop("theme", None)
        auto = theme_state()["auto"]
        flash("Theme back on automatic." + (f" It's {THEME_NAMES[auto]} right now." if auto else "")
              if choice == "auto" else
              f"Theme set to {THEME_NAMES[choice]} for everyone until you switch it back to Auto.")
    back = request.form.get("next", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("home"))


def primetime_panel(me, week, games, table):
    """Everything the homepage's primetime banner shows."""
    th = theme_state()
    gm = th["game"]
    if not gm:
        return None
    names = {p["id"]: p["name"] for p in db().execute("SELECT id, name FROM players")}
    locked = is_locked(gm)
    split = {gm["away"]: 0, gm["home"]: 0}
    for r in db().execute("SELECT team FROM picks WHERE game_id=?", (gm["id"],)):
        if r["team"] in split:
            split[r["team"]] += 1
    mine = dbl = None
    if me:
        row = db().execute("SELECT team FROM picks WHERE player_id=? AND game_id=?",
                           (me["id"], gm["id"])).fetchone()
        mine = row["team"] if row else None
        d = get_double(me["id"], week)
        dbl = bool(d and d["game_id"] == gm["id"])
    leader = None
    if gm["state"] != "pre" and gm["home_score"] is not None:
        if gm["home_score"] != gm["away_score"]:
            leader = gm["home"] if gm["home_score"] > gm["away_score"] else gm["away"]
    panel = {"game": gm, "locked": locked, "split": split if locked else None,
             "mine": mine, "double": dbl, "leader": leader, "night": th["name"]}

    # "If it ends now": season standings counting the games being played.
    if any(x["state"] == "in" for x in games):
        live, now_pts = live_points(week), week_points(week)
        proj = []
        for t in table:
            pid = next((i for i, n in names.items() if n == t["name"]), None)
            proj.append({"name": t["name"], "points": t["points"] + live.get(pid, 0) - now_pts.get(pid, 0),
                         "was": t["points"]})
        proj.sort(key=lambda t: (-t["points"], t["name"].lower()))
        rank_was = {t["name"]: 1 + sum(1 for u in table if u["points"] > t["points"]) for t in table}
        for i, t in enumerate(proj[:3]):
            t["rank"] = 1 + sum(1 for u in proj if u["points"] > t["points"])
            t["move"] = rank_was[t["name"]] - t["rank"]
        panel["projected"] = proj[:3]

    # Monday night decides ties for the week: show who's tied and how close their guesses are.
    mnf = monday_game(games)
    if mnf and mnf["id"] == gm["id"] and gm["state"] != "pre":
        paid = entrants(week) if pot(week)[0] else None
        pts = {pid: p for pid, p in live_points(week).items() if paid is None or pid in paid}
        best = max(pts.values(), default=0)
        tied = [pid for pid, p in pts.items() if p == best and best]
        total = (gm["home_score"] or 0) + (gm["away_score"] or 0)
        guesses = tiebreak_guesses(week)
        panel["tracker"] = {
            "total": total, "best": best, "pot": pot(week)[0],
            "rows": sorted(({"name": names.get(pid, "?"), "guess": guesses.get(pid),
                             "off": abs(guesses[pid] - total) if pid in guesses else None,
                             "me": bool(me and pid == me["id"])} for pid in tied),
                           key=lambda r: (r["off"] is None, r["off"] or 0, r["name"].lower()))}
    return panel


@app.route("/health")
def health():
    """A tiny page for uptime pingers. Doesn't touch the database."""
    return "ok", 200, {"Content-Type": "text/plain"}


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

    # Weeks that are over are all locked anyway, so only this week and later need paying for.
    paywall = week >= current_week() and not can_pick(me, week)
    if request.method == "POST" and paywall:
        flash(f"Pay your {fmt_money(pay_settings()['fee'])} week {week} entry to make picks.")
        return redirect(url_for("pay"))

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

    if paywall:
        check_inbox()            # maybe they just paid
        paywall = not can_pick(me, week)
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
                           next_kickoff=min(upcoming) if upcoming else None,
                           paywall=paywall)


@app.route("/pay")
@login_required
def pay():
    me = current_player()
    s = pay_settings()
    if s["required"] and not is_paid(me["id"]):
        check_inbox()
    week = current_week()
    total, count = pot(week)
    return render_template("pay.html", link=venmo_link(me["id"]), note=pay_note(me["id"]),
                           week=week, paid=paid_amount(me["id"], week), done=is_paid(me["id"], week),
                           paid_thru=paid_through(me["id"]), pot_total=total, pot_count=count)


@app.route("/pay/check", methods=["POST"])
@login_required
def pay_check():
    me = current_player()
    before = paid_through(me["id"])
    ok, msg = check_inbox(force=True)
    after = paid_through(me["id"])
    if after and after != before:
        flash(f"Payment received. You're in for week {current_week()}. Go make your picks!"
              if after == current_week() else
              f"Payment received. You're paid through week {after}.")
        return redirect(url_for("picks", week=current_week()))
    flash("No payment from you yet. Venmo's email can take a minute or two, "
          "so try again shortly." if ok else msg)
    return redirect(url_for("pay"))


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
    total, count = pot()
    unpaid = set()
    if pay_settings()["required"]:
        unpaid = {p["name"] for p in db().execute("SELECT id, name FROM players")
                  if not is_paid(p["id"])}
    return render_template("standings.html", table=standings(), played=played,
                           week=current_week(), pot_total=total, pot_count=count, unpaid=unpaid,
                           pots=[h for h in pot_history() if h["done"]])


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
                db().execute("DELETE FROM payments WHERE player_id=?", (pid,))
                db().execute("UPDATE venmo_inbox SET status='unmatched', player_id=NULL "
                             "WHERE player_id=?", (pid,))
                db().execute("DELETE FROM players WHERE id=?", (pid,))
                db().commit()
                flash("Player removed.")
        elif action == "toggle_admin":
            pid = int(request.form["player_id"])
            if pid != me["id"]:
                db().execute("UPDATE players SET is_admin = 1 - is_admin WHERE id=?", (pid,))
                db().commit()
        elif action == "pay_settings":
            venmo = request.form.get("venmo", "").strip().lstrip("@")
            if venmo and not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", venmo):
                flash("That doesn't look like a Venmo username. Use just the name, like Ashten-Vignery.")
                return redirect(url_for("admin"))
            try:
                fee = float(request.form.get("fee", "10"))
                assert 0 < fee <= 1000
            except (ValueError, AssertionError):
                flash("Entry fee must be a dollar amount, like 10.")
                return redirect(url_for("admin"))
            required = request.form.get("required") == "on"
            if required and not venmo:
                flash("Add your Venmo username before turning on required payment.")
                return redirect(url_for("admin"))
            set_setting("venmo_user", venmo)
            set_setting("pay_fee", f"{fee:g}")
            set_setting("pay_required", "1" if required else "0")
            flash("Payment settings saved. " + ("Players must pay before making picks."
                                                if required else "Payment is optional for now."))
        elif action == "check_inbox":
            ok, msg = check_inbox(force=True)
            flash(msg or ("Inbox checked. No new payments." if ok else "Couldn't check the inbox."))
        elif action == "mark_paid":
            pid = int(request.form["player_id"])
            owed = pay_settings()["fee"] - paid_amount(pid)
            if owed > 0:
                add_payment(pid, owed, "manual")
                db().commit()
            flash(f"Marked as paid for week {current_week()}.")
        elif action == "clear_paid":
            pid = int(request.form["player_id"])
            week = current_week()
            # A Venmo payment can cover several weeks, so undo all of it and put the
            # email back in the list to sort out (giving it out again re-spreads it).
            refs = [r["ref"] for r in db().execute(
                "SELECT DISTINCT ref FROM payments WHERE player_id=? AND season=? AND week=? "
                "AND source='venmo' AND ref IS NOT NULL", (pid, SEASON, week))]
            db().execute("DELETE FROM payments WHERE player_id=? AND season=? AND week=?",
                         (pid, SEASON, week))
            for ref in refs:
                db().execute("DELETE FROM payments WHERE ref=? AND source='venmo'", (ref,))
                db().execute("UPDATE venmo_inbox SET status='unmatched', player_id=NULL WHERE msg_id=?",
                             (ref,))
            db().commit()
            flash(f"Week {week} payment cleared." + (
                " Their Venmo payment is back in the list to sort out." if refs else ""))
        elif action == "assign":
            row = db().execute("SELECT * FROM venmo_inbox WHERE msg_id=? AND status='unmatched'",
                               (request.form.get("msg_id", ""),)).fetchone()
            pid = request.form.get("player_id", "")
            if row and pid.isdigit():
                db().execute("UPDATE venmo_inbox SET status='matched', player_id=? WHERE msg_id=?",
                             (int(pid), row["msg_id"]))
                credit(int(pid), row["amount"], "venmo", row["msg_id"],
                       start=payment_week(row["received"]))
                db().commit()
                flash(f"Gave {row['payer']}'s {fmt_money(row['amount'])} to that player.")
        elif action == "payout":
            key = f"payout:{SEASON}:{int(request.form.get('week', 0))}"
            set_setting(key, "0" if get_setting(key) == "1" else "1")
        elif action == "dismiss":
            db().execute("UPDATE venmo_inbox SET status='dismissed' WHERE msg_id=?",
                         (request.form.get("msg_id", ""),))
            db().commit()
        return redirect(url_for("admin"))
    if pay_settings()["inbox"]:
        check_inbox()
    players = db().execute("SELECT * FROM players ORDER BY lower(name)").fetchall()
    games = db().execute("SELECT COUNT(*) c FROM games WHERE season=?", (SEASON,)).fetchone()["c"]
    unmatched = db().execute("""SELECT * FROM venmo_inbox WHERE status='unmatched' AND season=?
                                ORDER BY received DESC""", (SEASON,)).fetchall()
    total, count = pot()
    return render_template("admin.html", players=players, game_count=games, week=current_week(),
                           paid={p["id"]: paid_amount(p["id"]) for p in players},
                           paid_thru={p["id"]: paid_through(p["id"]) for p in players},
                           pots=pot_history(),
                           codes={p["id"]: pay_code(p["id"]) for p in players},
                           unmatched=unmatched, pot_total=total, pot_count=count,
                           inbox_error=get_setting("inbox_error", ""))


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
