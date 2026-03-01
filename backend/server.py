"""
Shapes & States — FastAPI Backend

City placement is handled exclusively by the Discord bot via POST /bot/city.
The web frontend is a read-only viewer (cities + live SVG borders via WebSocket).
Discord OAuth is kept so the web can show ownership identity.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT         = Path(__file__).parent.parent
CITIES_FILE  = ROOT / "data" / "cities.json"
BORDERS_SVG  = ROOT / "data" / "borders.svg"
TERRAIN_MAP  = ROOT / "terrain_map.png"
PREPROCESSOR = ROOT / "preprocessor" / "process.py"

DATABASE_URL              = os.getenv("DATABASE_URL", "")
DISCORD_CLIENT_ID         = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET     = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI      = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
SECRET_KEY                = os.getenv("SECRET_KEY", "change-me-in-production")
BOT_SECRET                = os.getenv("BOT_SECRET", "")

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4321",
        "http://localhost:3000",
        "https://shapes-and-states.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth = OAuth()
oauth.register(
    name="discord",
    client_id=DISCORD_CLIENT_ID,
    client_secret=DISCORD_CLIENT_SECRET,
    authorize_url="https://discord.com/api/oauth2/authorize",
    access_token_url="https://discord.com/api/oauth2/token",
    api_base_url="https://discord.com/api/",
    client_kwargs={"scope": "identify"},
)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            discord_id TEXT PRIMARY KEY,
            username   TEXT,
            avatar     TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            x          INTEGER NOT NULL,
            y          INTEGER NOT NULL,
            owner_id   TEXT REFERENCES players(discord_id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()

# Static files: /static/* → project root
app.mount("/static", StaticFiles(directory=str(ROOT)), name="static")

# --------------------------------------------------------------------------- #
# Terrain helpers
# --------------------------------------------------------------------------- #
_terrain_img: Optional[Image.Image] = None
_map_size: tuple[int, int] = (0, 0)

WATER_RGB = (102, 234, 255)


def load_terrain():
    global _terrain_img, _map_size
    if TERRAIN_MAP.exists():
        _terrain_img = Image.open(TERRAIN_MAP).convert("RGB")
        _map_size = _terrain_img.size


load_terrain()


def is_water(x: int, y: int) -> bool:
    if _terrain_img is None:
        return False
    W, H = _map_size
    if x < 0 or x >= W or y < 0 or y >= H:
        return True
    return _terrain_img.getpixel((x, y))[:3] == WATER_RGB

# --------------------------------------------------------------------------- #
# WebSocket manager
# --------------------------------------------------------------------------- #
class ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.remove(ws)


manager = ConnectionManager()

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def db_to_cities_json():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, x, y, owner_id FROM cities")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    cities = [
        {"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"], "owner": r["owner_id"]}
        for r in rows
    ]
    CITIES_FILE.write_text(json.dumps(cities, indent=2), encoding="utf-8")
    return cities


async def run_preprocessor() -> str:
    import subprocess
    from functools import partial

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        partial(
            subprocess.run,
            [sys.executable, str(PREPROCESSOR)],
            capture_output=True,
            text=True,
        ),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Preprocessor failed:\n{result.stderr}")
    return BORDERS_SVG.read_text(encoding="utf-8")


def require_bot_secret(x_bot_secret: str = Header(default="")):
    if not BOT_SECRET:
        raise HTTPException(status_code=500, detail="BOT_SECRET not configured on server")
    if x_bot_secret != BOT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid bot secret")


async def _do_place_city(discord_id: str, name: str, x: int, y: int) -> dict:
    W, H = _map_size
    if W > 0 and (x < 0 or x >= W or y < 0 or y >= H):
        raise HTTPException(status_code=400, detail="Coordinates out of bounds")

    if is_water(x, y):
        raise HTTPException(status_code=400, detail="Cannot place a city on water")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id FROM cities WHERE owner_id = %s", (discord_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="You already have a city")

    city_id = name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:6]

    cur.execute(
        "INSERT INTO cities (id, name, x, y, owner_id) VALUES (%s, %s, %s, %s, %s)",
        (city_id, name, x, y, discord_id),
    )

    # Flush to cities.json so preprocessor sees it, but don't commit yet
    cur.execute("SELECT id, name, x, y, owner_id FROM cities")
    rows = cur.fetchall()
    cities = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"], "owner": r["owner_id"]} for r in rows]
    CITIES_FILE.write_text(json.dumps(cities, indent=2), encoding="utf-8")

    try:
        svg = await run_preprocessor()
    except RuntimeError as e:
        conn.rollback()
        cur.close()
        conn.close()
        db_to_cities_json()
        raise HTTPException(status_code=500, detail=str(e))

    conn.commit()
    cur.close()
    conn.close()

    await manager.broadcast({"type": "borders_update", "svg": svg})

    return {"id": city_id, "name": name, "x": x, "y": y, "owner": discord_id}

# --------------------------------------------------------------------------- #
# Bot endpoints
# --------------------------------------------------------------------------- #
class BotPlaceCityRequest(BaseModel):
    discord_id: str
    username:   str
    avatar:     Optional[str] = None
    name:       str
    x:          int
    y:          int


@app.post("/bot/city")
async def bot_place_city(
    body: BotPlaceCityRequest,
    x_bot_secret: str = Header(default=""),
):
    require_bot_secret(x_bot_secret)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO players (discord_id, username, avatar) VALUES (%s, %s, %s)"
        " ON CONFLICT (discord_id) DO UPDATE SET username=EXCLUDED.username, avatar=EXCLUDED.avatar",
        (body.discord_id, body.username, body.avatar or ""),
    )
    conn.commit()
    cur.close()
    conn.close()

    return await _do_place_city(body.discord_id, body.name, body.x, body.y)

# --------------------------------------------------------------------------- #
# Discord OAuth
# --------------------------------------------------------------------------- #
@app.get("/auth/discord")
async def auth_discord(request: Request):
    return await oauth.discord.authorize_redirect(request, DISCORD_REDIRECT_URI)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.discord.authorize_access_token(request)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
    user = resp.json()
    discord_id = user["id"]
    username   = user["username"]
    avatar     = user.get("avatar", "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO players (discord_id, username, avatar) VALUES (%s, %s, %s)"
        " ON CONFLICT (discord_id) DO UPDATE SET username=EXCLUDED.username, avatar=EXCLUDED.avatar",
        (discord_id, username, avatar),
    )
    conn.commit()
    cur.close()
    conn.close()

    request.session["discord_id"] = discord_id
    request.session["username"]   = username
    request.session["avatar"]     = avatar
    return RedirectResponse("/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/auth/me")
async def me(request: Request):
    discord_id = request.session.get("discord_id")
    if not discord_id:
        return JSONResponse({"authenticated": False})
    return JSONResponse({
        "authenticated": True,
        "discord_id": discord_id,
        "username":   request.session.get("username"),
        "avatar":     request.session.get("avatar"),
    })

# --------------------------------------------------------------------------- #
# Cities
# --------------------------------------------------------------------------- #
@app.get("/cities")
async def get_cities():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT c.id, c.name, c.x, c.y, c.owner_id, p.username, p.avatar "
        "FROM cities c JOIN players p ON c.owner_id = p.discord_id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    if BORDERS_SVG.exists():
        await websocket.send_json({
            "type": "borders_update",
            "svg":  BORDERS_SVG.read_text(encoding="utf-8"),
        })
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
