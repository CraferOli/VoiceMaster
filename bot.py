
import os
import json
import asyncio

from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import GUILD_ID, VOICE_SETUPS


# --------------------------------------------------
# KONFIGURATION
# --------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

STORAGE_FILE = Path("data/temporary_channels.json")


# --------------------------------------------------
# INTENTS
# --------------------------------------------------

intents = discord.Intents.default()

intents.guilds = True
intents.voice_states = True


# --------------------------------------------------
# BOT
# --------------------------------------------------

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# --------------------------------------------------
# TEMPORÄRE CHANNELS
# --------------------------------------------------

# Enthält alle vom Bot erstellten temporären Channel-IDs
managed_temporary_channels: set[int] = set()

# Verhindert gleichzeitiges Erstellen innerhalb einer Gruppe
creation_locks: dict[int, asyncio.Lock] = {}


# --------------------------------------------------
# SPEICHERN UND LADEN
# --------------------------------------------------

def load_temporary_channels():

    global managed_temporary_channels

    if not STORAGE_FILE.exists():
        return

    try:
        with STORAGE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        managed_temporary_channels = {
            int(channel_id)
            for channel_id in data.get("channels", [])
        }

        print(
            f"{len(managed_temporary_channels)} "
            "temporäre Channels geladen."
        )

    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Fehler beim Laden der JSON-Datei: {error}")


def save_temporary_channels():

    try:
        STORAGE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        data = {
            "channels": sorted(managed_temporary_channels)
        }

        temporary_file = STORAGE_FILE.with_suffix(".tmp")

        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                indent=4
            )

        temporary_file.replace(STORAGE_FILE)

    except OSError as error:
        print(f"Fehler beim Speichern: {error}")


# --------------------------------------------------
# HILFSMETHODEN
# --------------------------------------------------

def is_managed_temporary_channel(channel):

    return channel.id in managed_temporary_channels


def is_protected_channel(channel_id, setup):

    if channel_id == setup["standard_channel_id"]:
        return True

    return channel_id in setup["additional_channel_ids"]


def get_temporary_channels(guild, category, setup):

    temporary_channels = []

    for channel_id in list(managed_temporary_channels):

        channel = guild.get_channel(channel_id)

        # Channel existiert nicht mehr
        if channel is None:
            managed_temporary_channels.discard(channel_id)
            continue

        # Nur Voice-Channels verwalten
        if not isinstance(channel, discord.VoiceChannel):
            managed_temporary_channels.discard(channel_id)
            continue

        # Nur Channels der zugehörigen Kategorie verwalten
        if channel.category_id != category.id:
            continue

        # Zusätzliche Sicherheitsprüfung
        if is_protected_channel(channel.id, setup):
            managed_temporary_channels.discard(channel.id)
            continue

        if is_managed_temporary_channel(channel):
            temporary_channels.append(channel)

    save_temporary_channels()

    return temporary_channels


# --------------------------------------------------
# CHANNEL ERSTELLEN
# --------------------------------------------------

async def create_temporary_channel(
        standard_channel,
        category,
        setup
):

    category_id = setup["category_id"]

    lock = creation_locks.setdefault(
        category_id,
        asyncio.Lock()
    )

    async with lock:

        guild = standard_channel.guild

        # Nach dem Warten erneut prüfen:
        # Vielleicht wurde inzwischen bereits ein Channel erstellt.
        temporary_channels = get_temporary_channels(
            guild,
            category,
            setup
        )

        empty_channels = [
            channel
            for channel in temporary_channels
            if len(channel.members) == 0
        ]

        if empty_channels:
            return

        # Der Standard-Channel muss noch belegt sein
        if len(standard_channel.members) == 0:
            return

        try:

            channel = await category.create_voice_channel(
                name=standard_channel.name,
                overwrites=standard_channel.overwrites,
                bitrate=standard_channel.bitrate,
                user_limit=standard_channel.user_limit,
                reason="Temporärer Voice-Channel"
            )

            managed_temporary_channels.add(channel.id)

            save_temporary_channels()

            print(
                f"Temporärer Channel erstellt: {channel.name}"
            )

        except discord.Forbidden:
            print(
                "Keine Berechtigung zum Erstellen eines Voice-Channels!"
            )

        except discord.HTTPException as error:
            print(
                f"Fehler beim Erstellen des Channels: {error}"
            )


# --------------------------------------------------
# CHANNEL LÖSCHEN
# --------------------------------------------------

async def delete_temporary_channel(channel):

    # Niemals unbekannte Channels löschen
    if not is_managed_temporary_channel(channel):
        return

    # Niemals belegte Channels löschen
    if len(channel.members) > 0:
        return

    try:

        await channel.delete(
            reason="Leerer temporärer Voice-Channel"
        )

        managed_temporary_channels.discard(channel.id)

        save_temporary_channels()

        print(
            f"Temporärer Channel gelöscht: {channel.name}"
        )

    except discord.NotFound:

        managed_temporary_channels.discard(channel.id)
        save_temporary_channels()

    except discord.Forbidden:
        print(
            f"Keine Berechtigung zum Löschen: {channel.name}"
        )

    except discord.HTTPException as error:
        print(
            f"Fehler beim Löschen: {error}"
        )


# --------------------------------------------------
# CHANNEL-VERWALTUNG
# --------------------------------------------------

async def maintain_channels(guild, setup):

    standard_channel = guild.get_channel(
        setup["standard_channel_id"]
    )

    category = guild.get_channel(
        setup["category_id"]
    )

    if not isinstance(standard_channel, discord.VoiceChannel):
        print(
            f"Standard-Channel nicht gefunden: {setup['name']}"
        )
        return

    if not isinstance(category, discord.CategoryChannel):
        print(
            f"Kategorie nicht gefunden: {setup['name']}"
        )
        return

    # Standard-Channel muss in der konfigurierten Kategorie liegen
    if standard_channel.category_id != category.id:
        print(
            f"Der Standard-Channel von {setup['name']} "
            "liegt nicht in der angegebenen Kategorie!"
        )
        return

    temporary_channels = get_temporary_channels(
        guild,
        category,
        setup
    )

    empty_channels = [
        channel
        for channel in temporary_channels
        if len(channel.members) == 0
    ]

    standard_occupied = len(standard_channel.members) > 0

    # --------------------------------------------------
    # STANDARD-CHANNEL LEER
    # --------------------------------------------------

    if not standard_occupied:

        # Alle leeren temporären Channels entfernen
        for channel in empty_channels:
            await delete_temporary_channel(channel)

        # Belegte temporäre Channels bleiben bestehen
        return

    # --------------------------------------------------
    # STANDARD-CHANNEL BELEGT
    # --------------------------------------------------

    # Kein freier temporärer Channel vorhanden:
    # Neuen erstellen
    if len(empty_channels) == 0:

        await create_temporary_channel(
            standard_channel,
            category,
            setup
        )

        return

    # Mehrere freie Channels vorhanden:
    # Einen behalten, Rest löschen
    for channel in empty_channels[1:]:

        await delete_temporary_channel(channel)


# --------------------------------------------------
# VOICE EVENT
# --------------------------------------------------

@bot.event
async def on_voice_state_update(
        member,
        before,
        after
):

    if member.guild.id != GUILD_ID:
        return

    # Alle Gruppen prüfen.
    # Dadurch werden sowohl Standard- als auch Temp-Channel-
    # Änderungen berücksichtigt.
    for setup in VOICE_SETUPS:

        await maintain_channels(
            member.guild,
            setup
        )


# --------------------------------------------------
# BOT START
# --------------------------------------------------

@bot.event
async def on_ready():

    load_temporary_channels()

    guild = bot.get_guild(GUILD_ID)

    if guild is None:
        print("Discord-Server wurde nicht gefunden!")
        return

    print("--------------------------------")
    print(f"Bot online: {bot.user}")
    print(f"Server: {guild.name}")
    print("--------------------------------")

    # Vorhandene Gruppen beim Start überprüfen
    for setup in VOICE_SETUPS:

        await maintain_channels(
            guild,
            setup
        )


# --------------------------------------------------
# START
# --------------------------------------------------

if __name__ == "__main__":

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN wurde nicht in der .env gefunden!"
        )

    bot.run(TOKEN)
