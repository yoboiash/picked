"""
Pulls the NFL schedule and scores from ESPN's free public scoreboard feed.
No API key needed. Only standard-library modules are used here.
"""
import json
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

        games.append({
            "id": event["id"],
            "season": season,
            "week": week,
            "kickoff": event["date"],  # UTC, e.g. "2026-10-18T17:00Z"
            "home": home["team"]["abbreviation"],
            "away": away["team"]["abbreviation"],
            "home_name": home["team"].get("displayName", home["team"]["abbreviation"]),
            "away_name": away["team"].get("displayName", away["team"]["abbreviation"]),
            "home_logo": home["team"].get("logo", ""),
            "away_logo": away["team"].get("logo", ""),
            "home_score": hs if state != "pre" else None,
            "away_score": as_ if state != "pre" else None,
            "state": state,
            "detail": status.get("shortDetail", ""),
            "winner": winner,
        })
    return games


def fetch_week(season, week):
    return parse_scoreboard(fetch_week_json(season, week), season, week)
