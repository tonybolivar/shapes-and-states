"""
Dev seed script — insert test cities directly, bypassing the bot/limit.
Usage:  python seed.py
Edit the CITIES list below to place cities wherever you want.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT        = Path(__file__).parent
CITIES_FILE = ROOT / "data" / "cities.json"

load_dotenv(ROOT / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# -- Add your test cities here -----------------------------------------------
CITIES = [
    {"id": "ashport", "name": "Ashport", "x": 2314, "y": 735, "owner": "test_user_1"}
]
# ----------------------------------------------------------------------------

def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    # Upsert fake players
    for city in CITIES:
        cur.execute(
            "INSERT INTO players (discord_id, username, avatar) VALUES (%s, %s, %s)"
            " ON CONFLICT (discord_id) DO NOTHING",
            (city["owner"], city["name"] + "_player", ""),
        )

    # Clear existing cities and insert test set
    cur.execute("DELETE FROM cities")
    for city in CITIES:
        cur.execute(
            "INSERT INTO cities (id, name, x, y, owner_id) VALUES (%s, %s, %s, %s, %s)",
            (city["id"], city["name"], city["x"], city["y"], city["owner"]),
        )

    conn.commit()
    cur.close()
    conn.close()

    # Sync cities.json
    CITIES_FILE.write_text(json.dumps(
        [{"id": c["id"], "name": c["name"], "x": c["x"], "y": c["y"], "owner": c["owner"]}
         for c in CITIES],
        indent=2,
    ), encoding="utf-8")

    print(f"Inserted {len(CITIES)} cities. Running preprocessor...")
    result = subprocess.run([sys.executable, "preprocessor/process.py"], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
    else:
        print("Done — refresh the browser to see the borders.")

if __name__ == "__main__":
    main()
