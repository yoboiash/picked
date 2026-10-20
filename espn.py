"""
Pulls the NFL schedule, scores and betting lines from ESPN's free public
scoreboard feed. No API key needed. Only standard-library modules are used here.
"""
import json
import re
import urllib.request

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?dates={season}&seasontype=2&week={week}"
)


def fetch_week_json(season, week):
    """Download the raw scoreboard JSON for one regular-season week."""
    url = SCOREBOARD_URL.format(season=season, week=week)
    req = urllib.request.Request(url, headers={"User-Agent": "pickem-app/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _num(value):
    """Turn 47.5, "47.5", "+150", "-3.5" into a number, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"[-+]?\d+(\.\d+)?", str(value))
    return float(m.group()) if m else None


def _moneyline(side_odds, moneyline_block, side):
    ml = _num((side_odds or {}).get("moneyLine"))
    if ml is None:
        try:
            ml = _num(moneyline_block[side]["close"]["odds"])
        except (KeyError, TypeError):
            ml = None
    return int(ml) if ml is not None else None


def parse_odds(comp, home, away):
    """Pull the spread, favorite, over/under and moneylines for one game.

    Returns a dict with favorite (team abbreviation, or None for a pick'em),
    spread (always positive, e.g. 3.5), over_under, home_ml and away_ml.
    Every value can be None when ESPN has no line for the game.
    """
    out = {"favorite": None, "spread": None, "over_under": None,
           "home_ml": None, "away_ml": None}
    odds_list = comp.get("odds") or []
    odds = odds_list[0] if odds_list and isinstance(odds_list[0], dict) else None
    if not odds:
        return out

    h, a = odds.get("homeTeamOdds") or {}, odds.get("awayTeamOdds") or {}
    details = (odds.get("details") or "").strip()
    out["over_under"] = _num(odds.get("overUnder"))
    out["home_ml"] = _moneyline(h, odds.get("moneyline"), "home")
    out["away_ml"] = _moneyline(a, odds.get("moneyline"), "away")

    # Who's favored: ESPN flags it directly; otherwise read "KC -3.5".
    fav = None
    if h.get("favorite") is True and a.get("favorite") is not True:
        fav = home
    elif a.get("favorite") is True and h.get("favorite") is not True:
        fav = away
    m = re.match(r"([A-Z]{2,4})\s*(-?\d+(?:\.\d+)?)", details)
    if fav is None and m and m.group(1) in (home, away):
        fav = m.group(1)

    spread = None
    if m:
        spread = abs(float(m.group(2)))
    elif _num(odds.get("spread")) is not None:
        spread = abs(_num(odds["spread"]))

    if details.upper() in ("EVEN", "PK", "PICK", "PICK'EM") or spread == 0:
        fav, spread = None, 0.0
    if fav is None and spread is None and out["home_ml"] and out["away_ml"]:
        # Only moneylines: the negative (or smaller) number is the favorite.
        fav = home if out["home_ml"] < out["away_ml"] else away
    out["favorite"] = fav
    out["spread"] = spread
    return out


def parse_scoreboard(data, season, week):
    """Turn ESPN's JSON into a simple list of game dicts."""
    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = teams["home"], teams["away"]
        status = event.get("status", {}).get("type", {})
        state = status.get("state", "pre")  # "pre", "in" or "post"

        def score(team):
            try:
                return int(team.get("score"))
            except (TypeError, ValueError):
                return None

        hs, as_ = score(home), score(away)
        winner = None
        if status.get("completed") and hs is not None and as_ is not None:
            if hs > as_:
                winner = home["team"]["abbreviation"]
            elif as_ > hs:
                winner = away["team"]["abbreviation"]
            else:
                winner = "TIE"

        h_abbr = home["team"]["abbreviation"]
        a_abbr = away["team"]["abbreviation"]
        game = {
            "id": event["id"],
            "season": season,
            "week": week,
            "kickoff": event["date"],  # UTC, e.g. "2026-10-18T17:00Z"
            "home": h_abbr,
            "away": a_abbr,
            "home_name": home["team"].get("displayName", h_abbr),
            "away_name": away["team"].get("displayName", a_abbr),
            "home_logo": home["team"].get("logo", ""),
            "away_logo": away["team"].get("logo", ""),
            "home_color": home["team"].get("color") or None,
            "away_color": away["team"].get("color") or None,
            "home_score": hs if state != "pre" else None,
            "away_score": as_ if state != "pre" else None,
            "state": state,
            "detail": status.get("shortDetail", ""),
            "winner": winner,
        }
        game.update(parse_odds(comp, h_abbr, a_abbr))
        games.append(game)
    return games


def fetch_week(season, week):
    return parse_scoreboard(fetch_week_json(season, week), season, week)
