#!/usr/bin/env python3
"""
Imposter game — multiplayer backend.

Pure standard library (no pip installs). Serves the static client from
./public and exposes a small JSON REST API for room/game state. Clients
poll GET /api/rooms/<code> every ~1.5s to stay in sync.

Run: python3 server.py [port]
"""
import json
import os
import random
import re
import secrets
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

CATEGORIES = {
    "Animals": ["Platypus", "Mongoose", "Ocelot", "Narwhal", "Pangolin", "Meerkat", "Iguana", "Armadillo", "Toucan", "Bison", "Otter", "Falcon"],
    "Foods": ["Bruschetta", "Falafel", "Gazpacho", "Croissant", "Empanada", "Tempura", "Baklava", "Ceviche", "Paella", "Bibimbap", "Poutine", "Risotto"],
    "Movies & Shows": ["Inception", "Whiplash", "Parasite", "The Wire", "Chernobyl", "Fargo", "Interstellar", "Severance", "Succession", "Arrival"],
    "Occupations": ["Cartographer", "Actuary", "Locksmith", "Choreographer", "Sommelier", "Blacksmith", "Taxidermist", "Orthodontist", "Upholsterer", "Podiatrist"],
    "Everyday Objects": ["Corkscrew", "Thermostat", "Colander", "Trowel", "Carabiner", "Whisk", "Grommet", "Tweezers", "Clothespin", "Spatula"],
    "Places": ["Monastery", "Vineyard", "Boardwalk", "Speakeasy", "Observatory", "Lighthouse", "Greenhouse", "Courtroom", "Auditorium", "Marina"],
    "Sports": ["Fencing", "Curling", "Archery", "Badminton", "Rowing", "Snowboarding", "Water Polo", "Lacrosse", "Rugby", "Pentathlon"],
}
ALL_CATEGORY = "Random (all categories)"

# Vaguer, harder-to-exploit hints shown to the imposter instead of the literal
# category name (e.g. "Foods" is a dead giveaway once you know the category list;
# "Something You Eat" still narrows it down without handing over the answer).
CATEGORY_HINTS = {
    "Animals": "A Living Creature",
    "Foods": "Something You Eat",
    "Movies & Shows": "A Piece Of Entertainment",
    "Occupations": "A Job Someone Does",
    "Everyday Objects": "A Physical Object",
    "Places": "A Location You Could Visit",
    "Sports": "A Physical Activity",
}


def category_hint(category):
    return CATEGORY_HINTS.get(category, "Something Common")

ROOM_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous 0/O/1/I

# Tap-only reactions for the live room chat — no free typing allowed.
EXPRESSIONS = [
    "😏 NOICE", "😂 Lol", "🤨 Sus", "🤔 Hmm", "🔥 FIA", "❓ Doesn't make sense",
    "😮 Whoa", "👏 Clap", "🦈 Biting", "😬 Bite", "🎣 Real Me In", "💪 Tuff",
    "⏰ Hurry Up", "⏳ Time", "🕵️ IMPOSTER",
]
CHAT_HISTORY_LIMIT = 100

LOCK = threading.Lock()
ROOMS = {}  # code -> room dict
STALE_SECONDS = 6 * 60 * 60  # gc rooms untouched for 6h

LEADERBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.json")
LEADERBOARD = {}  # name.lower() -> {"name": display name, "wins": int}


def load_leaderboard():
    global LEADERBOARD
    try:
        with open(LEADERBOARD_FILE, "r") as f:
            LEADERBOARD = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        LEADERBOARD = {}


def save_leaderboard():
    try:
        tmp = LEADERBOARD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(LEADERBOARD, f)
        os.replace(tmp, LEADERBOARD_FILE)
    except OSError:
        pass  # best-effort; don't crash the game over a disk hiccup


def record_imposter_win(name):
    key = (name or "").strip().lower()
    if not key:
        return
    entry = LEADERBOARD.get(key)
    if entry is None:
        entry = {"name": name.strip(), "wins": 0}
        LEADERBOARD[key] = entry
    entry["wins"] += 1
    save_leaderboard()


def leaderboard_list():
    rows = sorted(LEADERBOARD.values(), key=lambda e: (-e["wins"], e["name"].lower()))
    return [{"name": r["name"], "wins": r["wins"]} for r in rows]


def now():
    return time.time()


def gen_room_code():
    while True:
        code = "".join(secrets.choice(ROOM_CODE_CHARS) for _ in range(4))
        if code not in ROOMS:
            return code


def gen_player_id():
    return secrets.token_hex(8)


def gc_rooms():
    cutoff = now() - STALE_SECONDS
    dead = [c for c, r in ROOMS.items() if r["lastActivity"] < cutoff]
    for c in dead:
        del ROOMS[c]


def pick_word(category):
    if category in CATEGORIES:
        return random.choice(CATEGORIES[category]), category
    # "Random (all categories)" — pick a category first so we can still hand
    # the imposter a category hint even though the word could be from anywhere.
    cat = random.choice(list(CATEGORIES.keys()))
    return random.choice(CATEGORIES[cat]), cat


def public_players(room, viewer_id):
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "isHost": p["id"] == room["hostId"],
            "isYou": p["id"] == viewer_id,
            "connected": True,
        }
        for p in room["players"]
    ]


def room_view(room, player_id):
    sync_turn(room)
    sync_voting(room)
    phase = room["phase"]
    view = {
        "code": room["code"],
        "phase": phase,
        "players": public_players(room, player_id),
        "numImposters": room["numImposters"],
        "category": room["category"],
        "categories": [ALL_CATEGORY] + list(CATEGORIES.keys()),
        "isHost": player_id == room["hostId"],
        "youId": player_id,
        "timerSeconds": room["timerSeconds"],
        "expressions": EXPRESSIONS,
        "chat": room["chat"][-CHAT_HISTORY_LIMIT:],
    }
    if phase in ("playing", "results"):
        is_imposter = player_id in room["imposterIds"]
        view["yourRole"] = "imposter" if is_imposter else room["word"]
        if is_imposter:
            view["categoryHint"] = category_hint(room.get("wordCategory"))
        ts = room["timer"]
        if ts["status"] == "running":
            remaining = max(0, ts["endsAt"] - now())
        else:
            remaining = ts["remaining"]
        view["timer"] = {"status": ts["status"], "remaining": round(remaining)}
    if phase == "playing":
        view["turnSubPhase"] = room.get("turnSubPhase")
        if room.get("turnSubPhase") == "clues" and room.get("turn"):
            turn = room["turn"]
            cur_id = turn["order"][turn["index"]]
            cur_player = next((p for p in room["players"] if p["id"] == cur_id), None)
            remaining = max(0, turn["endsAt"] - now()) if turn["endsAt"] else 0
            view["turn"] = {
                "round": turn["round"],
                "totalRounds": turn["totalRounds"],
                "state": turn["state"],
                "currentPlayerId": cur_id,
                "currentPlayerName": cur_player["name"] if cur_player else "?",
                "isYourTurn": cur_id == player_id,
                "remaining": round(remaining),
                "currentWord": turn["currentWord"],
                "words": turn["words"],
            }
        elif room.get("turnSubPhase") == "voting":
            votes = room.get("votes", {})
            view["voting"] = {
                "yourGuess": votes.get(player_id),
                "lockedCount": len(votes),
                "totalPlayers": len(room["players"]),
                "suspects": public_players(room, player_id),
            }
    if phase == "results":
        view["word"] = room["word"]
        view["results"] = [
            {"id": p["id"], "name": p["name"], "isImposter": p["id"] in room["imposterIds"]}
            for p in room["players"]
        ]
        view["turnHistory"] = room.get("turn", {}).get("words", []) if room.get("turn") else []
        votes = room.get("votes", {})
        view["votes"] = [
            {
                "playerId": p["id"],
                "name": p["name"],
                "guessId": votes.get(p["id"]),
                "guessName": next((pp["name"] for pp in room["players"] if pp["id"] == votes.get(p["id"])), None),
                "correct": votes.get(p["id"]) in room["imposterIds"],
            }
            for p in room["players"]
        ]
        view["leaderboard"] = leaderboard_list()
    return view


TURN_SUBMIT_SECONDS = 80
TURN_REVEAL_SECONDS = 7
TURN_TOTAL_ROUNDS = 3
TURN_TOTAL_ROUNDS_LARGE_GROUP = 2
LARGE_GROUP_SIZE = 4
VOTE_SECONDS = 90


def rounds_for_group(num_players):
    # Bigger groups take longer to get through a round of clues, so trim
    # to 2 rounds once there are 4 or more players to keep the game moving.
    return TURN_TOTAL_ROUNDS_LARGE_GROUP if num_players >= LARGE_GROUP_SIZE else TURN_TOTAL_ROUNDS


def rotate_imposters(room, ids, n):
    """Pick the next n imposters from a fair round-robin queue so everyone gets
    a turn before anyone repeats, instead of pure randomness which can (and did)
    pick the same person several games in a row."""
    cycle = [i for i in room.get("imposterCycle", []) if i in ids]
    newcomers = [i for i in ids if i not in cycle]
    random.shuffle(newcomers)
    cycle = cycle + newcomers
    if len(cycle) < n:
        cycle = ids[:]
        random.shuffle(cycle)
        last = room.get("imposterIds") or set()
        attempts = 0
        while len(ids) > n and set(cycle[:n]) == last and attempts < 8:
            random.shuffle(cycle)
            attempts += 1
    chosen = set(cycle[:n])
    room["imposterCycle"] = cycle[n:]
    return chosen


def start_round(room):
    ids = [p["id"] for p in room["players"]]
    random.shuffle(ids)
    n = max(1, min(room["numImposters"], max(1, len(ids) // 3), len(ids) - 1))
    room["imposterIds"] = rotate_imposters(room, ids, n)
    room["word"], room["wordCategory"] = pick_word(room["category"])
    room["phase"] = "playing"
    room["timer"] = {"status": "stopped", "remaining": room["timerSeconds"], "endsAt": None}
    room["votes"] = {}
    room["leaderboardApplied"] = False
    # Turn-based "give a word" rounds: same shuffled order, through everyone.
    room["turnSubPhase"] = "clues"
    room["turn"] = {
        "round": 1,
        "totalRounds": rounds_for_group(len(ids)),
        "order": ids[:],
        "index": 0,
        "state": "submitting",
        "endsAt": now() + TURN_SUBMIT_SECONDS,
        "words": [],
        "currentWord": None,
    }


def sync_turn(room):
    """Lazily advance the clue-round turn state based on wall-clock deadlines.
    Called whenever we build a view so polling clients see auto-advances
    (turn timeout -> reveal -> next player) without needing a background thread."""
    if room["phase"] != "playing" or room.get("turnSubPhase") != "clues":
        return
    turn = room["turn"]
    guard = 0
    while turn["endsAt"] is not None and now() >= turn["endsAt"] and guard < 200:
        guard += 1
        if turn["state"] == "submitting":
            cur_id = turn["order"][turn["index"]]
            player = next((p for p in room["players"] if p["id"] == cur_id), None)
            name = player["name"] if player else "?"
            turn["words"].append({"round": turn["round"], "playerId": cur_id, "name": name, "word": ""})
            turn["currentWord"] = {"playerId": cur_id, "name": name, "word": "", "skipped": True}
            turn["state"] = "revealed"
            turn["endsAt"] = now() + TURN_REVEAL_SECONDS
        elif turn["state"] == "revealed":
            turn["index"] += 1
            if turn["index"] >= len(turn["order"]):
                turn["index"] = 0
                turn["round"] += 1
            if turn["round"] > turn["totalRounds"]:
                # All 3 clue rounds are done -> straight into the 90s "lock your guess" vote,
                # which starts running immediately (no host action needed).
                room["turnSubPhase"] = "voting"
                turn["state"] = "done"
                turn["endsAt"] = None
                room["votes"] = {}
                room["timer"] = {"status": "running", "remaining": VOTE_SECONDS, "endsAt": now() + VOTE_SECONDS}
            else:
                turn["state"] = "submitting"
                turn["endsAt"] = now() + TURN_SUBMIT_SECONDS
        else:
            break


def sync_voting(room):
    """Once the 3 clue rounds are done, everyone has 90s to lock in a guess for who
    the imposter is. When the timer runs out (or everyone's locked in), auto-reveal
    results — no manual 'Reveal Answers' button needed."""
    if room["phase"] != "playing" or room.get("turnSubPhase") != "voting":
        return
    ts = room["timer"]
    everyone_voted = len(room["players"]) > 0 and len(room.get("votes", {})) >= len(room["players"])
    if (ts["status"] == "running" and now() >= ts["endsAt"]) or everyone_voted:
        room["phase"] = "results"
        ts["status"] = "stopped"
        ts["remaining"] = 0
        ts["endsAt"] = None
        apply_leaderboard_wins(room)


def apply_leaderboard_wins(room):
    """Called exactly once, right as a game flips into 'results'. An imposter
    only counts as a leaderboard win if the group's guesses didn't land on
    them — i.e. they successfully fooled everyone."""
    if room.get("leaderboardApplied"):
        return
    room["leaderboardApplied"] = True

    votes = room.get("votes", {})
    tally = {}
    for guess_id in votes.values():
        tally[guess_id] = tally.get(guess_id, 0) + 1
    top_count = max(tally.values()) if tally else 0
    caught_ids = {pid for pid, count in tally.items() if count == top_count and top_count > 0}

    by_id = {p["id"]: p["name"] for p in room["players"]}
    for imp_id in room["imposterIds"]:
        if imp_id not in caught_ids:
            name = by_id.get(imp_id)
            if name:
                record_imposter_win(name)


#
# ---------- Brain Jam (Scattergories-style) ----------
#

BJ_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H", "J", "L", "M", "N", "O", "P", "R", "S", "T", "W"]

BJ_CATEGORY_BANK = [
    "Type Of Bird", "Type Of Insect", "Color", "Things Found In This Room",
    "Things You Say While Playing A Sport", "Sports", "Things You Say Before Falling Asleep",
    "Things You Do At Night", "Things You Do During The Day", "Type Of Fish", "Fruit", "Vegetable",
    "Article Of Clothing", "Kitchen Item", "School Subject", "Job Or Occupation", "Music Genre",
    "Board Game", "Video Game", "Superhero Or Villain", "Cartoon Character", "Dance Move", "Holiday",
    "Type Of Weather", "Drink", "Musical Instrument", "Car Brand", "Body Part", "Something Sticky",
    "Something Cold", "Thing You'd Find At A Party", "Thing You'd Bring To The Beach", "Emotion",
    "Type Of Tree", "Dog Breed",
]

BJ_BONUS_CATEGORIES = [
    "Movie", "Country", "Famous Athlete", "Common Phrase Or Saying", "TV Show",
    "Celebrity", "Song Title", "Brand Name", "Video Game", "City",
]

BJ_TOTAL_ROUNDS = 3
BJ_ROUND_SECONDS = 90
BJ_BONUS_ROUND_SECONDS = 75
BJ_BONUS_SLOTS = 10
BJ_BONUS_CHANCE = 0.4  # checked once between each normal round; capped at one bonus round per game
BJ_MIN_PLAYERS = 2

BJ_ROOMS = {}  # code -> room dict


def bj_gen_room_code():
    while True:
        code = "".join(secrets.choice(ROOM_CODE_CHARS) for _ in range(4))
        if code not in BJ_ROOMS:
            return code


def bj_gc_rooms():
    cutoff = now() - STALE_SECONDS
    dead = [c for c, r in BJ_ROOMS.items() if r["lastActivity"] < cutoff]
    for c in dead:
        del BJ_ROOMS[c]


def bj_normalize(s):
    cleaned = re.sub(r"[^a-z0-9 ]", "", (s or "").strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def bj_score_answer(answer, letter):
    """Base score = 1 point per word in the answer that starts with the round's
    letter, as long as the FIRST word starts with it (otherwise the whole answer
    is invalid). This is what makes a true alliterative answer like "Frosted
    Flakes" worth 2 instead of 1 — but a run broken by a non-matching word still
    only counts the words that actually match, nothing extra for "almost"."""
    words = [w for w in (answer or "").strip().split() if w]
    if not words or words[0][0].lower() != letter.lower():
        return 0
    return sum(1 for w in words if w[0].lower() == letter.lower())


def bj_public_players(room):
    return [
        {"id": p["id"], "name": p["name"], "isHost": p["id"] == room["hostId"], "score": p.get("score", 0)}
        for p in room["players"]
    ]


def bj_start_round(room, bonus=False):
    if bonus:
        room["roundType"] = "bonus"
        base_cat = random.choice(BJ_BONUS_CATEGORIES)
        room["bonusCategoryLabel"] = base_cat
        # 10 numbered blanks for the one category, modeled as 10 synthetic
        # per-slot "categories" so the existing duplicate-cancellation +
        # alliteration scoring machinery just works without any changes.
        room["categories"] = [f"{base_cat} #{i + 1}" for i in range(BJ_BONUS_SLOTS)]
        room["categorySlots"] = BJ_BONUS_SLOTS
        room["bonusUsed"] = True
        seconds = BJ_BONUS_ROUND_SECONDS
    else:
        room["roundType"] = "normal"
        room["roundNumber"] = room.get("roundNumber", 0) + 1
        room["categories"] = random.sample(BJ_CATEGORY_BANK, 12)
        room["categorySlots"] = None
        room["bonusCategoryLabel"] = None
        seconds = BJ_ROUND_SECONDS
    room["letter"] = random.choice(BJ_LETTERS)
    room["answers"] = {p["id"]: {} for p in room["players"]}
    room["locked"] = set()
    room["roundResults"] = None
    room["phase"] = "playing"
    room["timer"] = {"status": "running", "remaining": seconds, "endsAt": now() + seconds}


# Named milestones for a player's consecutive-scoring-answer streak within a
# round. Hitting one of these exact lengths (not just "at least") pays out a
# bonus on top of that category's normal points; the streak resets to 0 on any
# blank, invalid, or canceled-duplicate answer, so a player can earn the same
# milestone more than once per round by breaking and rebuilding the streak.
BJ_STREAK_MILESTONES = {3: ("Turkey", 1), 4: ("Octopus", 3), 6: ("Sixth Sense", 3)}
BJ_SOLE_SURVIVOR_BONUS = 2  # you're the only player who wrote anything for this category


def bj_compute_round(room):
    categories = room["categories"]
    letter = room["letter"]
    answers = room.get("answers", {})
    breakdown = []
    round_points = {p["id"]: 0 for p in room["players"]}
    streak = {p["id"]: 0 for p in room["players"]}
    mw_streak = {p["id"]: 0 for p in room["players"]}
    canceled_counts = {p["id"]: 0 for p in room["players"]}

    for cat in categories:
        norm_counts = {}
        answered_count = 0
        for p in room["players"]:
            raw = (answers.get(p["id"], {}).get(cat) or "").strip()
            if raw:
                answered_count += 1
            norm = bj_normalize(raw)
            if norm:
                norm_counts[norm] = norm_counts.get(norm, 0) + 1
        entries = []
        for p in room["players"]:
            pid = p["id"]
            raw = (answers.get(pid, {}).get(cat) or "").strip()
            norm = bj_normalize(raw)
            word_count = len(raw.split()) if raw else 0
            base_points = bj_score_answer(raw, letter) if raw else 0
            valid = base_points > 0
            canceled = valid and norm_counts.get(norm, 0) > 1
            scored = valid and not canceled

            if canceled:
                canceled_counts[pid] += 1

            badges = []
            if scored:
                if word_count >= 2:
                    mw_streak[pid] += 1
                    points = 2 * mw_streak[pid]  # escalating "double word" bonus: 2, 4, 6, ...
                else:
                    mw_streak[pid] = 0
                    points = base_points

                streak[pid] += 1
                milestone = BJ_STREAK_MILESTONES.get(streak[pid])
                if milestone:
                    label, bonus = milestone
                    badges.append({"label": label, "points": bonus})
                    points += bonus

                if answered_count == 1:
                    badges.append({"label": "Sole Survivor", "points": BJ_SOLE_SURVIVOR_BONUS})
                    points += BJ_SOLE_SURVIVOR_BONUS
            else:
                streak[pid] = 0
                mw_streak[pid] = 0
                points = 0

            round_points[pid] += points
            entries.append({
                "playerId": pid, "name": p["name"], "answer": raw,
                "valid": valid, "canceled": canceled, "points": points,
                "badges": badges,
            })
        breakdown.append({"category": cat, "entries": entries})

    mastermind_names = []
    for p in room["players"]:
        if canceled_counts[p["id"]] == 0:
            round_points[p["id"]] += 1
            mastermind_names.append(p["name"])

    return breakdown, round_points, mastermind_names


def bj_end_round(room):
    breakdown, round_points, mastermind_names = bj_compute_round(room)
    for p in room["players"]:
        p["score"] = p.get("score", 0) + round_points.get(p["id"], 0)
    room["roundResults"] = {
        "breakdown": breakdown,
        "roundPoints": round_points,
        "mastermindNames": mastermind_names,
    }
    room["phase"] = "reveal"
    room["timer"] = {"status": "stopped", "remaining": 0, "endsAt": None}


def bj_sync(room):
    """Lazily end the round once the timer runs out or everyone's locked in —
    same lazy wall-clock-comparison pattern as the Imposter game's sync_turn."""
    if room["phase"] != "playing":
        return
    ts = room["timer"]
    all_locked = len(room["players"]) > 0 and len(room.get("locked", set())) >= len(room["players"])
    if (ts["status"] == "running" and now() >= ts["endsAt"]) or all_locked:
        bj_end_round(room)


def bj_advance(room):
    """Move from a finished (reveal) round to the next one. Always plays exactly
    BJ_TOTAL_ROUNDS normal rounds; at most once, between two of them, a surprise
    bonus round can be inserted instead — never right after the very last round."""
    if room["roundType"] == "bonus":
        if room.get("roundNumber", 0) >= BJ_TOTAL_ROUNDS:
            room["phase"] = "gameover"
        else:
            bj_start_round(room, bonus=False)
        return
    if room.get("roundNumber", 0) >= BJ_TOTAL_ROUNDS:
        room["phase"] = "gameover"
        return
    if not room.get("bonusUsed") and random.random() < BJ_BONUS_CHANCE:
        bj_start_round(room, bonus=True)
    else:
        bj_start_round(room, bonus=False)


def bj_room_view(room, player_id):
    bj_sync(room)
    phase = room["phase"]
    view = {
        "code": room["code"],
        "phase": phase,
        "isHost": player_id == room["hostId"],
        "youId": player_id,
        "players": bj_public_players(room),
        "totalRounds": BJ_TOTAL_ROUNDS,
        "roundNumber": room.get("roundNumber", 0),
        "roundType": room.get("roundType"),
    }
    if phase == "playing":
        ts = room["timer"]
        remaining = max(0, ts["endsAt"] - now()) if ts["status"] == "running" else ts["remaining"]
        view["timer"] = {"status": ts["status"], "remaining": round(remaining)}
        view["letter"] = room["letter"]
        view["categories"] = room["categories"]
        view["categorySlots"] = room.get("categorySlots")
        view["bonusCategoryLabel"] = room.get("bonusCategoryLabel")
        view["yourAnswers"] = room.get("answers", {}).get(player_id, {})
        view["lockedCount"] = len(room.get("locked", set()))
        view["totalPlayers"] = len(room["players"])
        view["youLocked"] = player_id in room.get("locked", set())
    if phase == "reveal":
        view["letter"] = room["letter"]
        view["categories"] = room["categories"]
        view["categorySlots"] = room.get("categorySlots")
        view["bonusCategoryLabel"] = room.get("bonusCategoryLabel")
        view["roundResults"] = room.get("roundResults")
    if phase == "gameover":
        view["finalScores"] = sorted(bj_public_players(room), key=lambda p: -p["score"])
    return view


def bj_require_room(code):
    room = BJ_ROOMS.get(code.upper())
    if not room:
        raise ApiError(404, "Room not found")
    return room


def bj_require_player(room, player_id):
    p = next((p for p in room["players"] if p["id"] == player_id), None)
    if not p:
        raise ApiError(403, "Not a player in this room")
    return p


def bj_require_host(room, player_id):
    if room["hostId"] != player_id:
        raise ApiError(403, "Only the host can do that")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def require_room(code):
    room = ROOMS.get(code.upper())
    if not room:
        raise ApiError(404, "Room not found")
    return room


def require_player(room, player_id):
    p = next((p for p in room["players"] if p["id"] == player_id), None)
    if not p:
        raise ApiError(403, "Not a player in this room")
    return p


def require_host(room, player_id):
    if room["hostId"] != player_id:
        raise ApiError(403, "Only the host can do that")


class Handler(BaseHTTPRequestHandler):
    server_version = "ImposterGame/1.0"

    def log_message(self, fmt, *args):
        pass  # keep stdout clean

    # ---------- helpers ----------
    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON body")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.handle_api_get(parsed)
        return self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.handle_api_post(parsed)
        self.send_json(404, {"error": "Not found"})

    # ---------- static files ----------
    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        safe_path = os.path.normpath(path).lstrip("/")
        full_path = os.path.join(PUBLIC_DIR, safe_path)
        if not full_path.startswith(PUBLIC_DIR) or not os.path.isfile(full_path):
            full_path = os.path.join(PUBLIC_DIR, "index.html")
            if not os.path.isfile(full_path):
                self.send_response(404)
                self.end_headers()
                return
        ext = os.path.splitext(full_path)[1]
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
        }.get(ext, "application/octet-stream")
        with open(full_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- API ----------
    def handle_api_get(self, parsed):
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)
        try:
            if len(parts) == 2 and parts[0] == "api" and parts[1] == "categories":
                return self.send_json(200, {"categories": [ALL_CATEGORY] + list(CATEGORIES.keys())})
            if len(parts) == 2 and parts[0] == "api" and parts[1] == "leaderboard":
                with LOCK:
                    board = leaderboard_list()
                return self.send_json(200, {"leaderboard": board})
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "rooms":
                code = parts[2]
                player_id = (qs.get("playerId") or [""])[0]
                with LOCK:
                    room = require_room(code)
                    require_player(room, player_id)
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)
            if len(parts) == 4 and parts[0:3] == ["api", "bj", "rooms"]:
                code = parts[3]
                player_id = (qs.get("playerId") or [""])[0]
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_player(room, player_id)
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)
            return self.send_json(404, {"error": "Not found"})
        except ApiError as e:
            return self.send_json(e.status, {"error": e.message})
        except Exception as e:
            return self.send_json(500, {"error": str(e)})

    def handle_api_post(self, parsed):
        parts = [p for p in parsed.path.split("/") if p]
        try:
            body = self.read_json_body()

            # POST /api/rooms  {name, code?} -> create room, becomes host
            if parts == ["api", "rooms"]:
                name = (body.get("name") or "").strip()[:24] or "Host"
                custom_code = (body.get("code") or "").strip().upper()
                with LOCK:
                    gc_rooms()
                    if custom_code:
                        if not (3 <= len(custom_code) <= 8) or not custom_code.isalnum():
                            raise ApiError(400, "Room code must be 3-8 letters/numbers")
                        if custom_code in ROOMS:
                            raise ApiError(409, f"Room code \"{custom_code}\" is already in use")
                        code = custom_code
                    else:
                        code = gen_room_code()
                    player_id = gen_player_id()
                    room = {
                        "code": code,
                        "hostId": player_id,
                        "players": [{"id": player_id, "name": name}],
                        "phase": "lobby",
                        "numImposters": 1,
                        "category": ALL_CATEGORY,
                        "word": None,
                        "wordCategory": None,
                        "imposterIds": set(),
                        "imposterCycle": [],
                        "leaderboardApplied": False,
                        "timerSeconds": 300,
                        "timer": {"status": "stopped", "remaining": 300, "endsAt": None},
                        "turnSubPhase": None,
                        "turn": None,
                        "votes": {},
                        "chat": [],
                        "createdAt": now(),
                        "lastActivity": now(),
                    }
                    ROOMS[code] = room
                    view = room_view(room, player_id)
                view["playerId"] = player_id
                return self.send_json(200, view)

            # POST /api/rooms/<code>/join  {name}
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "join":
                code = parts[2]
                name = (body.get("name") or "").strip()[:24] or "Player"
                with LOCK:
                    room = require_room(code)
                    if room["phase"] != "lobby":
                        raise ApiError(409, "Game already started — ask the host for a new round")
                    if len(room["players"]) >= 12:
                        raise ApiError(409, "Room is full")
                    player_id = gen_player_id()
                    room["players"].append({"id": player_id, "name": name})
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                view["playerId"] = player_id
                return self.send_json(200, view)

            # POST /api/rooms/<code>/settings  {playerId, numImposters, category}  (host only)
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "settings":
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    if "numImposters" in body:
                        n = int(body["numImposters"])
                        room["numImposters"] = max(1, min(3, n))
                    if "category" in body and (body["category"] == ALL_CATEGORY or body["category"] in CATEGORIES):
                        room["category"] = body["category"]
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/start  {playerId}  (host only)
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "start":
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    if len(room["players"]) < 3:
                        raise ApiError(409, "Need at least 3 players")
                    start_round(room)
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/timer  {playerId, action: start|pause|reset}  (host only)
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "timer":
                code = parts[2]
                player_id = body.get("playerId", "")
                action = body.get("action")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    ts = room["timer"]
                    if action == "start" and ts["status"] != "running":
                        ts["status"] = "running"
                        ts["endsAt"] = now() + ts["remaining"]
                    elif action == "pause" and ts["status"] == "running":
                        ts["remaining"] = max(0, ts["endsAt"] - now())
                        ts["status"] = "paused"
                        ts["endsAt"] = None
                    elif action == "reset":
                        ts["status"] = "stopped"
                        ts["remaining"] = room["timerSeconds"]
                        ts["endsAt"] = None
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/reveal  {playerId}  (host only)
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "reveal":
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    room["phase"] = "results"
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/again  {playerId}  (host only) -> new round, same players
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "again":
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    start_round(room)
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/lobby  {playerId}  (host only) -> back to lobby (edit players/settings)
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "lobby":
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    room["phase"] = "lobby"
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/turn/submit  {playerId, word} -> submit your clue word for the current turn
            if len(parts) == 5 and parts[0:2] == ["api", "rooms"] and parts[3:5] == ["turn", "submit"]:
                code = parts[2]
                player_id = body.get("playerId", "")
                word = (body.get("word") or "").strip()[:40]
                with LOCK:
                    room = require_room(code)
                    player = require_player(room, player_id)
                    sync_turn(room)
                    if room["phase"] != "playing" or room.get("turnSubPhase") != "clues":
                        raise ApiError(409, "Not in the word round right now")
                    turn = room["turn"]
                    if turn["state"] != "submitting":
                        raise ApiError(409, "Hang on for the reveal — try again in a moment")
                    cur_id = turn["order"][turn["index"]]
                    if cur_id != player_id:
                        raise ApiError(403, "It's not your turn yet")
                    if not word:
                        raise ApiError(400, "Enter a word first")
                    turn["words"].append({"round": turn["round"], "playerId": player_id, "name": player["name"], "word": word})
                    turn["currentWord"] = {"playerId": player_id, "name": player["name"], "word": word, "skipped": False}
                    turn["state"] = "revealed"
                    turn["endsAt"] = now() + TURN_REVEAL_SECONDS
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/turn/skip  {playerId}  (host only) -> force-advance a stuck/away turn
            if len(parts) == 5 and parts[0:2] == ["api", "rooms"] and parts[3:5] == ["turn", "skip"]:
                code = parts[2]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = require_room(code)
                    require_host(room, player_id)
                    sync_turn(room)
                    if room["phase"] == "playing" and room.get("turnSubPhase") == "clues":
                        turn = room["turn"]
                        if turn["state"] == "submitting":
                            cur_id = turn["order"][turn["index"]]
                            cur_player = next((p for p in room["players"] if p["id"] == cur_id), None)
                            name = cur_player["name"] if cur_player else "?"
                            turn["words"].append({"round": turn["round"], "playerId": cur_id, "name": name, "word": ""})
                            turn["currentWord"] = {"playerId": cur_id, "name": name, "word": "", "skipped": True}
                            turn["state"] = "revealed"
                            turn["endsAt"] = now() + TURN_REVEAL_SECONDS
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/vote  {playerId, guessId} -> lock in your guess for who the imposter is
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "vote":
                code = parts[2]
                player_id = body.get("playerId", "")
                guess_id = body.get("guessId", "")
                with LOCK:
                    room = require_room(code)
                    require_player(room, player_id)
                    sync_turn(room)
                    sync_voting(room)
                    if room["phase"] != "playing" or room.get("turnSubPhase") != "voting":
                        raise ApiError(409, "Not in the guessing phase right now")
                    if player_id in room["votes"]:
                        raise ApiError(409, "You already locked in your guess")
                    if not any(p["id"] == guess_id for p in room["players"]):
                        raise ApiError(400, "Not a valid player to guess")
                    room["votes"][player_id] = guess_id
                    sync_voting(room)  # if that was the last vote, reveal immediately
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/rooms/<code>/chat  {playerId, expression} -> send a tap-only reaction to room chat
            if len(parts) == 4 and parts[0:2] == ["api", "rooms"] and parts[3] == "chat":
                code = parts[2]
                player_id = body.get("playerId", "")
                expression = body.get("expression", "")
                with LOCK:
                    room = require_room(code)
                    player = require_player(room, player_id)
                    if expression not in EXPRESSIONS:
                        raise ApiError(400, "Not a valid expression")
                    room["chat"].append({
                        "id": secrets.token_hex(6),
                        "playerId": player_id,
                        "name": player["name"],
                        "expression": expression,
                        "ts": now(),
                    })
                    if len(room["chat"]) > CHAT_HISTORY_LIMIT * 2:
                        room["chat"] = room["chat"][-CHAT_HISTORY_LIMIT:]
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms  {name} -> create Brain Jam room, becomes host
            if parts == ["api", "bj", "rooms"]:
                name = (body.get("name") or "").strip()[:24] or "Host"
                with LOCK:
                    bj_gc_rooms()
                    code = bj_gen_room_code()
                    player_id = gen_player_id()
                    room = {
                        "code": code,
                        "hostId": player_id,
                        "players": [{"id": player_id, "name": name, "score": 0}],
                        "phase": "lobby",
                        "roundNumber": 0,
                        "roundType": None,
                        "letter": None,
                        "categories": [],
                        "categorySlots": None,
                        "bonusCategoryLabel": None,
                        "answers": {},
                        "locked": set(),
                        "roundResults": None,
                        "bonusUsed": False,
                        "timer": {"status": "stopped", "remaining": 0, "endsAt": None},
                        "createdAt": now(),
                        "lastActivity": now(),
                    }
                    BJ_ROOMS[code] = room
                    view = bj_room_view(room, player_id)
                view["playerId"] = player_id
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/join  {name}
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "join":
                code = parts[3]
                name = (body.get("name") or "").strip()[:24] or "Player"
                with LOCK:
                    room = bj_require_room(code)
                    if room["phase"] != "lobby":
                        raise ApiError(409, "Game already started — ask the host for a new game")
                    if len(room["players"]) >= 12:
                        raise ApiError(409, "Room is full")
                    player_id = gen_player_id()
                    room["players"].append({"id": player_id, "name": name, "score": 0})
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                view["playerId"] = player_id
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/start  {playerId}  (host only)
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "start":
                code = parts[3]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_host(room, player_id)
                    if len(room["players"]) < BJ_MIN_PLAYERS:
                        raise ApiError(409, f"Need at least {BJ_MIN_PLAYERS} players")
                    room["bonusUsed"] = False
                    room["roundNumber"] = 0
                    for p in room["players"]:
                        p["score"] = 0
                    bj_start_round(room, bonus=False)
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/answer  {playerId, category, text}
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "answer":
                code = parts[3]
                player_id = body.get("playerId", "")
                category = body.get("category", "")
                text = (body.get("text") or "")[:60]
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_player(room, player_id)
                    bj_sync(room)
                    if room["phase"] != "playing":
                        raise ApiError(409, "Not in an active round right now")
                    if category not in room["categories"]:
                        raise ApiError(400, "Not a valid category this round")
                    if player_id in room.get("locked", set()):
                        raise ApiError(409, "You already locked in your answers")
                    room["answers"].setdefault(player_id, {})[category] = text
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/lockin  {playerId} -> lock in early; ends the round once everyone has
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "lockin":
                code = parts[3]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_player(room, player_id)
                    bj_sync(room)
                    if room["phase"] != "playing":
                        raise ApiError(409, "Not in an active round right now")
                    room.setdefault("locked", set()).add(player_id)
                    bj_sync(room)
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/next  {playerId}  (host only) -> advance from reveal to the next round
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "next":
                code = parts[3]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_host(room, player_id)
                    if room["phase"] != "reveal":
                        raise ApiError(409, "Not ready for the next round yet")
                    bj_advance(room)
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/again  {playerId}  (host only) -> new game, same players, scores reset
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "again":
                code = parts[3]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_host(room, player_id)
                    room["bonusUsed"] = False
                    room["roundNumber"] = 0
                    for p in room["players"]:
                        p["score"] = 0
                    bj_start_round(room, bonus=False)
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            # POST /api/bj/rooms/<code>/lobby  {playerId}  (host only) -> back to lobby
            if len(parts) == 5 and parts[0:3] == ["api", "bj", "rooms"] and parts[4] == "lobby":
                code = parts[3]
                player_id = body.get("playerId", "")
                with LOCK:
                    room = bj_require_room(code)
                    bj_require_host(room, player_id)
                    room["phase"] = "lobby"
                    room["lastActivity"] = now()
                    view = bj_room_view(room, player_id)
                return self.send_json(200, view)

            return self.send_json(404, {"error": "Not found"})
        except ApiError as e:
            return self.send_json(e.status, {"error": e.message})
        except Exception as e:
            return self.send_json(500, {"error": str(e)})


def main():
    load_leaderboard()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8934))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Imposter game server running on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
