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
import secrets
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

CATEGORIES = {
    "Animals": ["Elephant", "Giraffe", "Penguin", "Octopus", "Kangaroo", "Dolphin", "Cheetah", "Gorilla", "Peacock", "Hedgehog", "Flamingo", "Chameleon"],
    "Foods": ["Pizza", "Sushi", "Taco", "Pancake", "Lasagna", "Burrito", "Donut", "Ramen", "Waffle", "Curry", "Popcorn", "Meatball"],
    "Movies & Shows": ["Titanic", "Jaws", "Frozen", "Avatar", "Shrek", "Inception", "Friends", "The Office", "Batman", "Jurassic Park"],
    "Occupations": ["Firefighter", "Dentist", "Pilot", "Chef", "Plumber", "Teacher", "Lawyer", "Astronaut", "Photographer", "Electrician"],
    "Everyday Objects": ["Umbrella", "Toothbrush", "Backpack", "Flashlight", "Blender", "Wallet", "Mirror", "Ladder", "Candle", "Stapler"],
    "Places": ["Beach", "Library", "Airport", "Museum", "Casino", "Hospital", "Stadium", "Zoo", "Cruise Ship", "Amusement Park"],
    "Sports": ["Soccer", "Basketball", "Tennis", "Golf", "Boxing", "Surfing", "Bowling", "Hockey", "Volleyball", "Skiing"],
}
ALL_CATEGORY = "Random (all categories)"

ROOM_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous 0/O/1/I

# Tap-only reactions for the live room chat — no free typing allowed.
EXPRESSIONS = ["👍 Nice", "😂 Lol", "🤨 Sus", "🤔 Hmm", "🔥 Fire", "❓ Doesn't make sense", "😮 Whoa", "👏 Clap"]
CHAT_HISTORY_LIMIT = 100

LOCK = threading.Lock()
ROOMS = {}  # code -> room dict
STALE_SECONDS = 6 * 60 * 60  # gc rooms untouched for 6h


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
            view["categoryHint"] = room.get("wordCategory")
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


def start_round(room):
    ids = [p["id"] for p in room["players"]]
    random.shuffle(ids)
    n = max(1, min(room["numImposters"], max(1, len(ids) // 3), len(ids) - 1))
    room["imposterIds"] = set(ids[:n])
    room["word"], room["wordCategory"] = pick_word(room["category"])
    room["phase"] = "playing"
    room["timer"] = {"status": "stopped", "remaining": room["timerSeconds"], "endsAt": None}
    room["votes"] = {}
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
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "rooms":
                code = parts[2]
                player_id = (qs.get("playerId") or [""])[0]
                with LOCK:
                    room = require_room(code)
                    require_player(room, player_id)
                    room["lastActivity"] = now()
                    view = room_view(room, player_id)
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

            return self.send_json(404, {"error": "Not found"})
        except ApiError as e:
            return self.send_json(e.status, {"error": e.message})
        except Exception as e:
            return self.send_json(500, {"error": str(e)})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8934))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Imposter game server running on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
