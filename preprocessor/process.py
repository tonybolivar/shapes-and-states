"""
Terrain-Weighted Voronoi Preprocessor
Input:  ../assets/terrain_map.png  +  ../data/cities.json
Output: ../data/borders.svg
"""

import hashlib
import json
import heapq
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).parent.parent
TERRAIN_MAP = ROOT / "backend" / "static" / "terrain_map.png"
CITIES_FILE = ROOT / "data" / "cities.json"
BORDERS_SVG = ROOT / "data" / "borders.svg"

# --------------------------------------------------------------------------- #
# Terrain cost table  (R, G, B) → movement cost
# --------------------------------------------------------------------------- #
TERRAIN_COSTS = {
    (102, 234, 255): 999,  # #66EAFF — water (impassable)
    (68,  61,  49):  10,   # #443D31 — mountains
    (89,  87, 124):  7,    # #59577C — hills
    (78, 130,  76):  4,    # #4E824C — forest
    (190, 162, 226): 6,    # #BEA2E2 — tundra
    (255, 255, 255): 2,    # #FFFFFF — plains
}
DEFAULT_COST = 3  # fallback for any unrecognised pixel

WATER_COST = 999
IMPASSABLE = float("inf")

# Maximum cumulative travel cost a city's influence can reach.
# Raise to grow starting territories, lower to shrink them.
# At plains cost=2: MAX_COST=800 ≈ 400-pixel radius through open land.
MAX_COST = 160


def city_color(city_id: str) -> str:
    """Deterministic HSLA color from city ID — same city always gets same color."""
    hue = int(hashlib.md5(city_id.encode()).hexdigest()[:4], 16) % 360
    return f"hsla({hue},65%,52%,0.55)"


def build_cost_grid(img_path: Path) -> np.ndarray:
    """Return a float32 cost grid from the terrain PNG."""
    img = Image.open(img_path).convert("RGB")
    pixels = np.array(img, dtype=np.uint8)          # (H, W, 3)
    H, W = pixels.shape[:2]
    costs = np.full((H, W), DEFAULT_COST, dtype=np.float32)

    # Build cost grid using vectorised closest-colour lookup
    # For each unique colour in the image, map it to a cost
    unique_colours = {}
    for rgb, cost in TERRAIN_COSTS.items():
        unique_colours[rgb] = float(IMPASSABLE) if cost >= WATER_COST else float(cost)

    for rgb, cost in unique_colours.items():
        r, g, b = rgb
        mask = (pixels[:, :, 0] == r) & (pixels[:, :, 1] == g) & (pixels[:, :, 2] == b)
        costs[mask] = cost

    return costs


def dijkstra_all(cost_grid: np.ndarray, sources: list[tuple[int, int]]) -> np.ndarray:
    """
    Multi-source Dijkstra.
    Returns an integer ownership grid where each cell holds the index of the
    nearest source city (by terrain-weighted distance).
    -1 means unreachable (water / isolated).
    """
    H, W = cost_grid.shape
    dist = np.full((H, W), np.inf, dtype=np.float64)
    owner = np.full((H, W), -1, dtype=np.int32)

    heap = []  # (dist, row, col, city_index)
    for idx, (cx, cy) in enumerate(sources):
        # sources are (x, y) == (col, row)
        row, col = cy, cx
        if row < 0 or row >= H or col < 0 or col >= W:
            continue
        if cost_grid[row, col] >= WATER_COST:
            continue
        dist[row, col] = 0.0
        owner[row, col] = idx
        heapq.heappush(heap, (0.0, row, col, idx))

    neighbours = [(-1, 0), (1, 0), (0, -1), (0, 1)]  # 4-connected

    while heap:
        d, r, c, city_idx = heapq.heappop(heap)
        if d > dist[r, c]:
            continue
        for dr, dc in neighbours:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= H or nc < 0 or nc >= W:
                continue
            cell_cost = cost_grid[nr, nc]
            if cell_cost >= WATER_COST:
                continue
            nd = d + cell_cost
            if nd <= MAX_COST and nd < dist[nr, nc]:
                dist[nr, nc] = nd
                owner[nr, nc] = city_idx
                heapq.heappush(heap, (nd, nr, nc, city_idx))

    return owner


def ownership_to_svg(owner: np.ndarray, cities: list[dict], W: int, H: int) -> str:
    """
    Convert the ownership grid into SVG paths.
    Uses a marching-squares-style border trace via scikit-image contours,
    falling back to a simple bounding polygon if scikit-image is unavailable.
    """
    try:
        from skimage import measure
        use_skimage = True
    except ImportError:
        use_skimage = False

    paths = []

    for idx, city in enumerate(cities):
        mask = (owner == idx).astype(np.uint8)
        if mask.sum() == 0:
            continue

        city_id = city["id"]
        owner_id = city.get("owner", "")

        if use_skimage:
            # find_contours returns (row, col) coordinates
            contours = measure.find_contours(mask.astype(float), level=0.5)
            if not contours:
                continue

            # Merge all contour segments into one compound path
            d_parts = []
            for contour in contours:
                if len(contour) < 2:
                    continue
                pts = contour  # shape (N, 2) — (row, col)
                # Convert to x,y (col,row) and round to 1 decimal
                coords = [(round(float(p[1]), 1), round(float(p[0]), 1)) for p in pts]
                d_parts.append(
                    "M " + " L ".join(f"{x},{y}" for x, y in coords) + " Z"
                )
            if not d_parts:
                continue
            d = " ".join(d_parts)
        else:
            # Fallback: bounding box of owned pixels
            rows, cols = np.where(mask)
            r0, r1 = int(rows.min()), int(rows.max())
            c0, c1 = int(cols.min()), int(cols.max())
            d = f"M {c0},{r0} L {c1},{r0} L {c1},{r1} L {c0},{r1} Z"

        color = city_color(city_id)
        paths.append(
            f'  <path id="{city_id}" data-owner="{owner_id}" fill="{color}" d="{d}" />'
        )

    inner = "\n".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">\n'
        f'{inner}\n'
        f'</svg>\n'
    )


def main():
    # ------------------------------------------------------------------ #
    # Load cities
    # ------------------------------------------------------------------ #
    cities = json.loads(CITIES_FILE.read_text(encoding="utf-8"))
    if not cities:
        BORDERS_SVG.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n',
            encoding="utf-8",
        )
        print("No cities — wrote empty borders.svg")
        return

    # ------------------------------------------------------------------ #
    # Build cost grid
    # ------------------------------------------------------------------ #
    print(f"Loading terrain map: {TERRAIN_MAP}")
    cost_grid = build_cost_grid(TERRAIN_MAP)
    H, W = cost_grid.shape
    print(f"Map size: {W}×{H}")

    # ------------------------------------------------------------------ #
    # Multi-source Dijkstra
    # ------------------------------------------------------------------ #
    sources = [(c["x"], c["y"]) for c in cities]
    print(f"Running Dijkstra for {len(sources)} cities…")
    owner_grid = dijkstra_all(cost_grid, sources)

    # ------------------------------------------------------------------ #
    # Trace borders → SVG
    # ------------------------------------------------------------------ #
    print("Tracing borders…")
    svg = ownership_to_svg(owner_grid, cities, W, H)
    BORDERS_SVG.write_text(svg, encoding="utf-8")
    print(f"Written: {BORDERS_SVG}")


if __name__ == "__main__":
    main()
