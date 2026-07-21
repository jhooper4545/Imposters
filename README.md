# Imposter — Party Game

A multiplayer "find the imposter" party game. Everyone joins on their phone with
a room code or QR code; one player secretly gets a different word than everyone
else and has to bluff their way through 2-3 rounds of one-word clues before the
group votes on who they think the imposter is.

No build step, no database — pure Python standard library backend
(`server.py`) serving a single static HTML/CSS/JS client (`public/index.html`).
Game state lives in memory for the life of the process.

## Run locally

```
python3 server.py 8934
```

Then open http://localhost:8934 — or on another device on the same Wi-Fi,
http://<your-computer's-local-ip>:8934.

## Deploy for free (Render.com) — permanent link, no laptop required

1. Push this folder to a GitHub repo (public or private, either works).
2. Go to https://render.com and sign up for a free account (no credit card
   needed for the free web service tier).
3. Click **New +** → **Blueprint**, and point it at this repo. Render will
   read `render.yaml` in this folder and set everything up automatically.
   (If you'd rather do it manually: **New +** → **Web Service**, connect the
   repo, leave the build command as `true`, and set the start command to
   `python3 server.py`.)
4. Deploy. Render gives you a permanent URL like
   `https://imposter-game-xxxx.onrender.com` that stays up regardless of
   whether your computer is on.

Note: Render's free tier spins the service down after ~15 minutes of no
traffic and takes ~30-60 seconds to wake back up on the next request — the
first person to open the link before a game just needs to wait a moment for
it to spin up. Everything works normally once it's awake.
