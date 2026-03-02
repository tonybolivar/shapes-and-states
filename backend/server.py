"""
Shapes & States — Optimized FastAPI Backend
Primary Store: cities.json | Player Data: DB
Integrated Voronoi Preprocessor for high performance.
"""

import asyncio
import json
import os
import sys
import uuid
import sqlite3
import traceback
import hashlib
import heapq
from pathlib import Path
from typing import Optional, List, Dict, Any
from functools import partial

import httpx
import numpy as np
import psycopg2
import psycopg2.extras
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from PIL import Image

# ─── CONFIGURATION ───────────────────────────────────────────────────────── #
load_dotenv(Path(__file__).parent.parent / ".env")

ROOT         = Path(__file__).parent.parent
DATA_DIR     = ROOT / "data"
CITIES_FILE  = DATA_DIR / "cities.json"
BORDERS_SVG  = DATA_DIR / "borders.svg"
TERRAIN_MAP  = ROOT / "backend" / "static" / "terrain_map.png"

DATABASE_URL = os.getenv("DATABASE_URL", "")
SECRET_KEY   = os.getenv("SECRET_KEY", "sns-secret-12345")
WEB_URL      = os.getenv("WEB_URL", "http://localhost:4321")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
BOT_SECRET   = os.getenv("BOT_SECRET", "")
GUILD_ID     = "1477201832433549313"

DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
DEBUG_MODE            = os.getenv("DEBUG_MODE", "false").lower() == "true"

# ─── PREPROCESSOR CONSTANTS ─────────────────────────────────────────────── #
TERRAIN_COSTS = {
    (102, 234, 255): 999,  # water
    (68,  61,  49):  10,   # mountains
    (89,  87, 124):  7,    # hills
    (78, 130,  76):  4,    # forest
    (190, 162, 226): 6,    # tundra
    (255, 255, 255): 2,    # plains
}
DEFAULT_COST = 3
WATER_COST = 999
IMPASSABLE = float("inf")
MAX_COST = 160

# ─── DATABASE ENGINE (Players Only) ──────────────────────────────────────── #
class Database:
    def __init__(self):
        self.is_sqlite = not DATABASE_URL or not DATABASE_URL.startswith("postgres")
        self.init_db()

    def get_conn(self):
        if self.is_sqlite:
            conn = sqlite3.connect(ROOT / "sns.db", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=3)
        except Exception as e:
            print(f"Postgres failed ({e}), falling back to SQLite.")
            self.is_sqlite = True
            return self.get_conn()

    def init_db(self):
        with self.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    discord_id TEXT PRIMARY KEY,
                    username   TEXT,
                    avatar     TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def execute(self, query: str, params: tuple = (), fetch: bool = False):
        sql = query.replace("%s", "?") if self.is_sqlite else query
        with self.get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if not self.is_sqlite and fetch else conn.cursor()
            cur.execute(sql, params)
            if fetch:
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            conn.commit()
            return None

db = Database()

# ─── CITIES JSON HELPERS ─────────────────────────────────────────────────── #
def read_cities() -> List[Dict[str, Any]]:
    if not CITIES_FILE.exists(): return []
    try: return json.loads(CITIES_FILE.read_text(encoding="utf-8"))
    except: return []

def write_cities(cities: List[Dict[str, Any]]):
    CITIES_FILE.write_text(json.dumps(cities, indent=2, ensure_ascii=False), encoding="utf-8")

# ─── INTEGRATED PREPROCESSOR LOGIC ───────────────────────────────────────── #
_cost_grid_cache: Optional[np.ndarray] = None
_terrain_img_cache: Optional[Image.Image] = None

def get_cost_grid() -> np.ndarray:
    global _cost_grid_cache
    if _cost_grid_cache is not None: return _cost_grid_cache
    
    if not TERRAIN_MAP.exists():
        return np.full((1000, 1000), DEFAULT_COST, dtype=np.float32)

    img = Image.open(TERRAIN_MAP).convert("RGB")
    pixels = np.array(img, dtype=np.uint8)
    H, W = pixels.shape[:2]
    costs = np.full((H, W), DEFAULT_COST, dtype=np.float32)

    unique_colours = {rgb: (float(IMPASSABLE) if cost >= WATER_COST else float(cost)) 
                     for rgb, cost in TERRAIN_COSTS.items()}

    for rgb, cost in unique_colours.items():
        r, g, b = rgb
        mask = (pixels[:, :, 0] == r) & (pixels[:, :, 1] == g) & (pixels[:, :, 2] == b)
        costs[mask] = cost

    _cost_grid_cache = costs
    return costs

def is_water(x: int, y: int) -> bool:
    grid = get_cost_grid()
    H, W = grid.shape
    if x < 0 or x >= W or y < 0 or y >= H: return True
    return grid[y, x] >= WATER_COST

def city_color(city_id: str) -> str:
    hue = int(hashlib.md5(city_id.encode()).hexdigest()[:4], 16) % 360
    return f"hsla({hue},65%,52%,0.55)"

def generate_borders_svg(cities: List[Dict[str, Any]]) -> str:
    cost_grid = get_cost_grid()
    H, W = cost_grid.shape
    
    if not cities:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"></svg>'

    # Multi-source Dijkstra
    dist = np.full((H, W), np.inf, dtype=np.float64)
    owner = np.full((H, W), -1, dtype=np.int32)
    heap = []
    
    for idx, c in enumerate(cities):
        cx, cy = int(c["x"]), int(c["y"])
        if 0 <= cy < H and 0 <= cx < W and cost_grid[cy, cx] < WATER_COST:
            dist[cy, cx] = 0.0
            owner[cy, cx] = idx
            heapq.heappush(heap, (0.0, cy, cx, idx))

    neighbours = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    while heap:
        d, r, c, city_idx = heapq.heappop(heap)
        if d > dist[r, c]: continue
        for dr, dc in neighbours:
            nr, nc = r + dr, c + dc
            if 0 <= nr < H and 0 <= nc < W:
                cell_cost = cost_grid[nr, nc]
                if cell_cost < WATER_COST:
                    nd = d + cell_cost
                    if nd <= MAX_COST and nd < dist[nr, nc]:
                        dist[nr, nc] = nd
                        owner[nr, nc] = city_idx
                        heapq.heappush(heap, (nd, nr, nc, city_idx))

    # Trace borders
    try:
        from skimage import measure
        use_skimage = True
    except ImportError:
        use_skimage = False

    paths = []
    for idx, city in enumerate(cities):
        mask = (owner == idx).astype(np.uint8)
        if mask.sum() == 0: continue
        
        if use_skimage:
            contours = measure.find_contours(mask.astype(float), level=0.5)
            d_parts = []
            for contour in contours:
                coords = [(round(float(p[1]), 1), round(float(p[0]), 1)) for p in contour]
                d_parts.append("M " + " L ".join(f"{x},{y}" for x, y in coords) + " Z")
            d = " ".join(d_parts) if d_parts else ""
        else:
            rows, cols = np.where(mask)
            r0, r1, c0, c1 = rows.min(), rows.max(), cols.min(), cols.max()
            d = f"M {c0},{r0} L {c1},{r0} L {c1},{r1} L {c0},{r1} Z"

        if d:
            paths.append(f'  <path id="{city["id"]}" data-owner="{city.get("owner","")}" fill="{city_color(city["id"])}" d="{d}" />')

    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">\n' + "\n".join(paths) + '\n</svg>'

async def update_borders():
    cities = read_cities()
    svg = await asyncio.get_event_loop().run_in_executor(None, generate_borders_svg, cities)
    BORDERS_SVG.write_text(svg, encoding="utf-8")
    return svg

# ─── APP SETUP ───────────────────────────────────────────────────────────── #
app = FastAPI(title="Shapes & States API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4321", "http://127.0.0.1:4321", WEB_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="sns_session", same_site="lax")

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

app.mount("/static", StaticFiles(directory=str(ROOT / "backend" / "static")), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

class ConnectionManager:
    def __init__(self): self.active = []
    async def connect(self, ws): await ws.accept(); self.active.append(ws)
    def disconnect(self, ws): 
        if ws in self.active: self.active.remove(ws)
    async def broadcast(self, data):
        for ws in self.active:
            try: await ws.send_json(data)
            except: pass

manager = ConnectionManager()

# ─── ENDPOINTS ───────────────────────────────────────────────────────────── #
@app.get("/auth/discord")
async def auth_discord(request: Request):
    return await oauth.discord.authorize_redirect(request, DISCORD_REDIRECT_URI)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.discord.authorize_access_token(request)
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token['access_token']}"})
            user = resp.json()
        
        db.execute("INSERT INTO players (discord_id, username, avatar) VALUES (%s, %s, %s) ON CONFLICT (discord_id) DO UPDATE SET username=EXCLUDED.username, avatar=EXCLUDED.avatar" if not db.is_sqlite else "INSERT OR REPLACE INTO players (discord_id, username, avatar) VALUES (?, ?, ?)", 
                   (user["id"], user["username"], user.get("avatar", "")))

        request.session.update({"discord_id": user["id"], "username": user["username"], "avatar": user.get("avatar", ""), "is_member": True})
        
        if BOT_TOKEN:
            async with httpx.AsyncClient() as client:
                m_resp = await client.get(f"https://discord.com/api/guilds/{GUILD_ID}/members/{user['id']}", headers={"Authorization": f"Bot {BOT_TOKEN}"})
                request.session["is_member"] = (m_resp.status_code == 200)

        return RedirectResponse(WEB_URL)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/auth/me")
async def me(request: Request):
    uid = request.session.get("discord_id")
    if not uid: return {"authenticated": False}
    cities = read_cities()
    return {**request.session, "authenticated": True, "has_city": any(c.get("owner") == uid for c in cities), "debug_mode": DEBUG_MODE}

@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(WEB_URL)

@app.get("/cities")
async def get_cities():
    cities = read_cities()
    players = {p["discord_id"]: p for p in db.execute("SELECT * FROM players", fetch=True)}
    for c in cities:
        if c.get("owner") in players:
            c["username"] = players[c["owner"]]["username"]
            c["avatar"] = players[c["owner"]]["avatar"]
    return cities

class PlaceCityRequest(BaseModel):
    name: str
    x: int
    y: int
    discord_id: Optional[str] = None
    username: Optional[str] = None
    avatar: Optional[str] = None

async def handle_place_city(body: PlaceCityRequest, uid: str):
    if is_water(body.x, body.y): raise HTTPException(status_code=400, detail="CANNOT PLACE ON WATER")
    cities = read_cities()
    if not DEBUG_MODE and any(c.get("owner") == uid for c in cities):
        raise HTTPException(status_code=400, detail="YOU ALREADY HAVE A CITY")
    
    city_id = f"{body.name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    new_city = {"id": city_id, "name": body.name, "x": body.x, "y": body.y, "owner": uid}
    cities.append(new_city)
    write_cities(cities)
    
    svg = await update_borders()
    await manager.broadcast({"type": "borders_update", "svg": svg})
    return {"id": city_id}

@app.post("/web/city")
async def web_place_city(request: Request, body: PlaceCityRequest):
    uid = request.session.get("discord_id")
    if not uid: raise HTTPException(status_code=401, detail="Sign in first")
    return await handle_place_city(body, uid)

@app.post("/bot/city")
async def bot_place_city(request: Request, body: PlaceCityRequest):
    if request.headers.get("X-Bot-Secret") != BOT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid bot secret")
    if not body.discord_id: raise HTTPException(status_code=400, detail="Missing discord_id")
    
    db.execute("INSERT INTO players (discord_id, username, avatar) VALUES (%s, %s, %s) ON CONFLICT (discord_id) DO UPDATE SET username=EXCLUDED.username, avatar=EXCLUDED.avatar" if not db.is_sqlite else "INSERT OR REPLACE INTO players (discord_id, username, avatar) VALUES (?, ?, ?)", 
               (body.discord_id, body.username, body.avatar or ""))
    
    return await handle_place_city(body, body.discord_id)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        if BORDERS_SVG.exists():
            await websocket.send_json({"type": "borders_update", "svg": BORDERS_SVG.read_text(encoding="utf-8")})
        while True: await websocket.receive_text()
    except WebSocketDisconnect: manager.disconnect(websocket)
    except: pass

if __name__ == "__main__":
    import uvicorn
    # Initial border generation on startup
    asyncio.run(update_borders())
    uvicorn.run(app, host="0.0.0.0", port=8000)
