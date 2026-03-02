# Shapes & States

A terrain-weighted Voronoi political map simulator. Players claim territory by placing cities through a Discord bot. Territory borders are computed server-side using multi-source Dijkstra's algorithm over a terrain cost raster, then streamed live to all connected web clients as SVG via WebSocket.

---

## What It Does

Each player founds exactly one city on the map. The server computes which pixels of the map belong to each city by running a weighted shortest-path flood from all city origins simultaneously. The result is rendered as an SVG overlay on the base map. Every connected browser sees border updates in real time the moment a city is placed.

The web frontend is read-only. City placement is exclusive to Discord slash commands.

---

## Architecture

```
Discord Bot  -->  POST /bot/city  -->  FastAPI Backend  -->  cities.json
                                               |
                                     Dijkstra + skimage
                                               |
                                         borders.svg
                                               |
                                      WebSocket broadcast
                                               |
                                      All connected browsers
```

### Backend (FastAPI + Python)

`backend/server.py` is the entire server. It handles:

- Discord OAuth2 login flow via Authlib
- City placement from both the web (`/web/city`) and bot (`/bot/city`)
- On-demand Voronoi/border recomputation
- Static file serving for the base map and generated SVG
- WebSocket connections for live border pushes
- PostgreSQL in production, SQLite fallback for local dev

**City storage:** `data/cities.json` is the source of truth. It is read and written directly on each placement. No ORM, no migrations. The PostgreSQL/SQLite database stores only player identity (discord_id, username, avatar).

**Database schema:**
```sql
CREATE TABLE players (
    discord_id TEXT PRIMARY KEY,
    username   TEXT,
    avatar     TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### Terrain Cost Raster

`backend/static/terrain_map.png` is a 4320x2160 PNG where each pixel's RGB value encodes a movement cost used by the Dijkstra flood:

| RGB | Terrain | Cost |
|-----|---------|------|
| (102, 234, 255) | Water | 999 (impassable) |
| (68, 61, 49) | Mountains | 10 |
| (89, 87, 124) | Hills | 7 |
| (78, 130, 76) | Forest | 4 |
| (190, 162, 226) | Tundra | 6 |
| (255, 255, 255) | Plains | 2 |
| anything else | Default | 3 |

The entire raster is loaded once at startup via Pillow, vectorized into a NumPy float32 array, and cached in memory for the lifetime of the process.

### Voronoi Border Generation

`generate_borders_svg()` runs every time a city is placed:

1. **Multi-source Dijkstra** -- all cities are seeded simultaneously into a min-heap as `(cost=0, row, col, city_index)`. The heap expands 4-connected (up/down/left/right). Water pixels (`cost >= 999`) are treated as impassable walls. A pixel is assigned to whichever city reaches it first at lowest cumulative terrain cost. Expansion stops when cumulative cost exceeds `MAX_COST = 160`.

2. **Contour tracing** -- for each city, the ownership mask (a boolean 2D array) is passed to `skimage.measure.find_contours()` at level 0.5. This returns sub-pixel contour coordinates as polyline segments. Each contour segment is serialized as an SVG `M ... L ... Z` path string.

3. **SVG assembly** -- all paths are written into a single `<svg viewBox="0 0 4320 2160">` document. Each `<path>` carries `id="{city_id}"` and `data-owner="{discord_id}"`. Fill color is derived deterministically from the city ID: `hue = int(md5(city_id)[:4], 16) % 360`, producing `hsla(hue, 65%, 52%, 0.55)`.

The SVG is written to `data/borders.svg` and then broadcast over all open WebSocket connections.

Because the Dijkstra runs in pure Python with a heapq, generation time scales with the number of reachable pixels. On a 4320x2160 map with MAX_COST=160 and plains cost=2, each city can expand up to roughly 80 pixels in any direction on flat terrain before being cut off.

### WebSocket Protocol

On connection, the server immediately sends the current borders:

```json
{ "type": "borders_update", "svg": "<svg>...</svg>" }
```

This same message is broadcast to all connected clients whenever a new city is placed. The client replaces the SVG innerHTML of each map instance and re-attaches click listeners.

### Bot (discord.py)

`bot/bot.py` runs as a separate process alongside the server (both launched from the `Procfile`). It communicates with the backend over HTTP, authenticating with a shared secret via the `X-Bot-Secret` header.

**Slash commands:**

- `/place-city name x y` -- founds a city at the given map coordinates. The bot POSTs to `/bot/city`, which validates water placement, enforces the one-city-per-player limit, writes to `cities.json`, and triggers border recomputation. The bot upserts the player's Discord identity into the database on every placement.
- `/my-city` -- retrieves the calling user's city from `GET /cities` and reports its name and coordinates.
- `/map` -- returns the URL to the live web frontend.

The bot does not hold any map state itself. Every operation is a synchronous HTTP call to the backend.

### Frontend (Astro)

`frontend/src/pages/index.astro` is a single-page application with no framework components. All logic is vanilla JS in an `is:inline` script block.

**Map rendering:** Three identical `<div class="map-instance">` elements are laid out horizontally inside a CSS-transformed container to implement infinite horizontal scroll. Each instance contains a base map `<img>` and two absolutely-positioned overlay layers: `borders-layer` (z-index 10) and `cities-layer` (z-index 5). The CSS transform (`translate + scale`) is animated every frame via `requestAnimationFrame` with linear interpolation (lerp factor 0.15) for smooth pan and zoom.

**CSS scoping caveat:** Astro scopes `<style>` blocks by default by injecting `data-astro-cid-xxx` attributes onto static template elements. SVG `<path>` elements injected at runtime via `innerHTML` never receive this attribute, so `<style is:global>` is required for CSS rules targeting those paths to apply.

**Border click detection:** After each `innerHTML` injection, `pointer-events: all` and `cursor: pointer` are set inline on every `<path>` element via JS, and a `click` listener is attached directly (event delegation is unreliable for dynamically injected SVG). Clicking a path calls `openCityPanel()`, which adds a `.selected` class to all matching paths across all three instances and slides in the settlement info panel.

**Coordinate system:** Map coordinates are integers in the range `[0, 4320] x [0, 2160]`, matching the terrain raster and SVG viewBox exactly. `getCoords(e)` converts a screen click to map coordinates by reading `getBoundingClientRect()` on the clicked map instance (which reflects the current CSS transform scale) and scaling by `mapWidth / rect.width`.

**Modes:**
- View mode: clicking territories opens the settlement info panel.
- Interact mode: clicking empty land opens a city placement form that POSTs to `/web/city`. Clicking a territory still opens the info panel.

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/auth/discord` | -- | Redirects to Discord OAuth2 |
| GET | `/auth/callback` | -- | OAuth2 callback, sets session cookie |
| GET | `/auth/me` | Session | Returns current user + has_city flag |
| GET | `/auth/logout` | Session | Clears session, redirects to frontend |
| GET | `/cities` | -- | Returns all cities with player metadata joined |
| POST | `/web/city` | Session cookie | Place a city from the web UI |
| POST | `/bot/city` | X-Bot-Secret header | Place a city from the Discord bot |
| WS | `/ws` | -- | WebSocket for live border updates |
| GET | `/static/*` | -- | Static files (base_map.png, terrain_map.png) |
| GET | `/data/*` | -- | Data files (borders.svg, cities.json) |

---

## Deployment

**Backend + Bot:** Railway. The `Procfile` runs both processes in the same dyno:
```
web: python bot/bot.py & python backend/server.py
```
The server binds to `$PORT` (Railway-injected). The bot process connects to Discord's gateway.

**Frontend:** Vercel. `vercel.json` specifies the Astro framework. The frontend reads `PUBLIC_API_URL` and `PUBLIC_WS_URL` at build time from Vercel environment variables.

**Session cookies:** `SameSite=None; Secure` is required because the frontend (Vercel) and backend (Railway) are on different origins. `SameSite=Lax` silently drops the session cookie on cross-origin redirects from the OAuth callback.

**Database:** PostgreSQL on Railway in production. The backend detects the `DATABASE_URL` environment variable; if absent or not a postgres URI, it falls back to a local SQLite file (`sns.db`).

---

## Environment Variables

| Variable | Used By | Description |
|----------|---------|-------------|
| `DISCORD_CLIENT_ID` | Backend | OAuth2 app client ID |
| `DISCORD_CLIENT_SECRET` | Backend | OAuth2 app client secret |
| `DISCORD_REDIRECT_URI` | Backend | OAuth2 callback URL |
| `BOT_TOKEN` | Bot | Discord bot token |
| `BOT_SECRET` | Backend + Bot | Shared secret for bot API auth |
| `DATABASE_URL` | Backend | PostgreSQL connection string |
| `SECRET_KEY` | Backend | Session middleware signing key |
| `WEB_URL` | Backend | Frontend origin (for CORS + redirects) |
| `BACKEND_URL` | Bot | Backend base URL |
| `DEBUG_MODE` | Backend | If true, bypasses one-city-per-player limit |
| `PUBLIC_API_URL` | Frontend | Backend URL (injected at build time) |
| `PUBLIC_WS_URL` | Frontend | WebSocket URL (injected at build time) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, uvicorn, Authlib, psycopg2, Pillow, NumPy, scikit-image |
| Bot | Python, discord.py 2.x, httpx |
| Frontend | Astro 5, vanilla JS, no framework components |
| Database | PostgreSQL (production), SQLite (development) |
| Deployment | Railway (backend + bot), Vercel (frontend) |
