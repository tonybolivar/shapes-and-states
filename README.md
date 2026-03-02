# Shapes & States

A terrain-weighted Voronoi political map simulator. Players claim territory by placing cities either through the web client or the Discord bot. Territory borders are computed server-side using multi-source Dijkstra's algorithm over a terrain cost raster, then streamed live to all connected web clients as SVG via WebSocket.

---

## What It Does

Each player founds exactly one city on the map. The server computes which pixels of the map belong to each city by running a weighted shortest-path flood from all city origins simultaneously. The result is rendered as an SVG overlay on the base map. Every connected browser sees border updates in real time the moment a city is placed.

City placement has two entry points: the web client (Interact mode, requires Discord login) and the Discord bot slash commands. Both hit the same backend logic and trigger the same border recomputation.

---

## Architecture

```
Web Client   -->  POST /web/city  -->  FastAPI Backend  -->  cities.json
Discord Bot  -->  POST /bot/city  --^        |
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
- Interact mode: clicking empty land opens a city placement form. The player names their city, confirms, and the client POSTs to `/web/city`. The server validates water placement and the one-city-per-player limit, writes to `cities.json`, recomputes borders, and broadcasts the update. Clicking a territory in interact mode still opens the info panel.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, uvicorn, Authlib, psycopg2, Pillow, NumPy, scikit-image |
| Bot | Python, discord.py 2.x, httpx |
| Frontend | Astro 5, vanilla JS, no framework components |
| Database | PostgreSQL (production), SQLite (development) |
| Deployment | Railway (backend + bot), Vercel (frontend) |
