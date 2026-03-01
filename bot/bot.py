"""
Shapes & States — Discord Bot
Slash commands let players interact with the map from Discord.
The bot calls the FastAPI backend's /bot/* endpoints.

Commands:
  /place-city name:<str> x:<int> y:<int>
  /my-city
  /map

Run:
  python bot/bot.py
"""

import os
import sys
from pathlib import Path

import discord
from discord import app_commands
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_SECRET  = os.getenv("BOT_SECRET", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
WEB_URL     = os.getenv("WEB_URL", "http://localhost:4321")

GOLD   = 0xC9A84C
RED    = 0x8B2020
GREEN  = 0x2D5A27

# --------------------------------------------------------------------------- #
# Bot setup
# --------------------------------------------------------------------------- #
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}  |  Slash commands synced")


# --------------------------------------------------------------------------- #
# Helper — call backend
# --------------------------------------------------------------------------- #
async def backend_post(path: str, payload: dict) -> tuple[int, dict]:
    """POST to the backend with the bot secret header. Returns (status, body)."""
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{BACKEND_URL}{path}",
            json=payload,
            headers={"X-Bot-Secret": BOT_SECRET},
            timeout=30,
        )
    try:
        body = r.json()
    except Exception:
        body = {"detail": r.text}
    return r.status_code, body


async def backend_get(path: str) -> tuple[int, dict | list]:
    async with httpx.AsyncClient() as http:
        r = await http.get(
            f"{BACKEND_URL}{path}",
            headers={"X-Bot-Secret": BOT_SECRET},
            timeout=10,
        )
    try:
        body = r.json()
    except Exception:
        body = {"detail": r.text}
    return r.status_code, body


# --------------------------------------------------------------------------- #
# /place-city
# --------------------------------------------------------------------------- #
@tree.command(name="place-city", description="Found a city and claim territory on the map")
@app_commands.describe(
    name="Name of your city (e.g. Ashport)",
    x="X coordinate — find it by hovering over the map",
    y="Y coordinate — find it by hovering over the map",
)
async def place_city(interaction: discord.Interaction, name: str, x: int, y: int):
    await interaction.response.defer(thinking=True)

    user = interaction.user
    avatar_hash = str(user.avatar) if user.avatar else None

    status, body = await backend_post("/bot/city", {
        "discord_id": str(user.id),
        "username":   user.name,
        "avatar":     avatar_hash,
        "name":       name,
        "x":          x,
        "y":          y,
    })

    if status == 200:
        embed = discord.Embed(
            title="⚔  City Founded",
            description=f"**{name}** has been established at `({x}, {y})`.",
            color=GOLD,
        )
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url,
        )
        embed.add_field(name="View the map", value=WEB_URL, inline=False)
        embed.set_footer(text="Borders are updating for all viewers now.")
        await interaction.followup.send(embed=embed)

    else:
        detail = body.get("detail", "Something went wrong.")
        embed = discord.Embed(
            title="Failed to place city",
            description=detail,
            color=RED,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# /my-city
# --------------------------------------------------------------------------- #
@tree.command(name="my-city", description="Show your current city on the map")
async def my_city(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    status, body = await backend_get("/cities")
    if status != 200:
        await interaction.followup.send("Couldn't reach the backend.", ephemeral=True)
        return

    cities = body if isinstance(body, list) else []
    city = next((c for c in cities if c.get("owner_id") == str(interaction.user.id)), None)

    if not city:
        embed = discord.Embed(
            description=f"You haven't founded a city yet.\nUse `/place-city` to claim your territory.",
            color=RED,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🏰  {city['name']}",
        color=GOLD,
    )
    embed.add_field(name="Coordinates", value=f"`{city['x']}, {city['y']}`", inline=True)
    embed.add_field(name="View the map", value=WEB_URL, inline=False)
    await interaction.followup.send(embed=embed)


# --------------------------------------------------------------------------- #
# /map
# --------------------------------------------------------------------------- #
@tree.command(name="map", description="Get a link to the live map")
async def show_map(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Shapes & States — Live Map",
        description=(
            f"[Open the map]({WEB_URL})\n\n"
            "Hover over the map to find coordinates, then use "
            "`/place-city` to claim your territory."
        ),
        color=GOLD,
    )
    await interaction.response.send_message(embed=embed)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in .env")
        sys.exit(1)
    client.run(BOT_TOKEN)
