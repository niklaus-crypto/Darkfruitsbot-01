"""
Dark Fruits Discord Bot — rewritten production version
======================================================
Fixes applied vs original:
- aiosqlite + single shared connection + asyncio.Lock (no event-loop blocking / lock storms)
- Real fruit-value scraper with fallback data
- Gemini-first AI with a bounded Gemini model fallback and optional Groq emergency fallback
- Live wiki lookup for Blox Fruits questions (not a fixed "database")
- asyncio.Lock around giveaway state + throttled embed updates
- Proper persistent views, economy safety, stock tracker, daily quests
- Config via environment variables

Required env:
  DISCORD_TOKEN

AI env:
  GEMINI_API_KEY    required — Google AI Studio
  GEMINI_MODEL      optional; defaults to gemini-3.8-flash

Optional env:
  STAFF_ROLE_ID, QUEST_CHANNEL_ID, QUEST_PROOF_CHANNEL_ID, PORT, DB_PATH, LOG_LEVEL
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import re
import sqlite3
import traceback
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
import aiosqlite
import discord
from aiohttp import web
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands, tasks

# Optional Gemini SDK — bot still runs if package/key missing
try:
    from google import genai
    from google.genai import types as genai_types

    _HAS_GENAI = True
except ImportError:
    genai = None  # type: ignore
    genai_types = None  # type: ignore
    _HAS_GENAI = False

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG & LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("darkfruits")

DARK_FRUITS_PURPLE = discord.Color.from_rgb(114, 53, 219)
SERVER_NAME = "Dark Fruits"
THUMBNAIL_URL = os.environ.get(
    "THUMBNAIL_URL",
    "https://cdn.discordapp.com/attachments/1490351057329590404/1560993894018322442/IMG_1033.jpg",
)
STAFF_ROLE_ID = int(os.environ.get("STAFF_ROLE_ID", "1492296310785835179"))
QUEST_CHANNEL_ID = int(os.environ.get("QUEST_CHANNEL_ID", "1555732644397064313"))
QUEST_PROOF_CHANNEL_ID = int(os.environ.get("QUEST_PROOF_CHANNEL_ID", "1555733599402066040"))
# Stock alerts
STOCK_CHANNEL_ID = int(os.environ.get("STOCK_CHANNEL_ID", "1489744139930173550"))
STOCK_PING_ROLE_ID = int(os.environ.get("STOCK_PING_ROLE_ID", "1495818903845343313"))
DB_PATH = os.environ.get("DB_PATH", "/var/data/giveaways.db" if os.path.isdir("/var/data") else "giveaways.db")
BLOXFRUITS_KNOWLEDGE_DB = os.environ.get("BLOXFRUITS_KNOWLEDGE_DB", "/var/data/bloxfruits_knowledge.db" if os.path.isdir("/var/data") else "bloxfruits_knowledge.db")

# Custom server fruit emojis
FRUIT_EMOJIS: dict[str, str] = {
    "Rocket": "<:Rocket:1489776145586065610>",
    "Spin": "<:Spin:1489776122152353962>",
    "Blade": "<:Blade:1489776099402579968>",
    "Spring": "<:Spring:1489776078523465949>",
    "Bomb": "<:Bomb:1489776047942664192>",
    "Smoke": "<:Smoke:1489776025553473718>",
    "Spike": "<:Spike:1489775987557400687>",
    "Flame": "<:Flame:1489767518720495777>",
    "Ice": "<:Ice:1489767706352943296>",
    "Sand": "<:Sand:1489775953121906879>",
    "Dark": "<:Dark:1489767429436346478>",
    "Diamond": "<:Diamond:1489775359074504745>",
    "Light": "<:Light:1489767249375002804>",
    "Rubber": "<:Rubber:1489775696602595488>",
    "Ghost": "<:Ghost:1489775508831866971>",
    "Magma": "<:Magma:1489767790671036657>",
    "Quake": "<:Quake:1489767946850013224>",
    "Buddha": "<:Buddha:1489768016802742546>",
    "Love": "<:Love:1489768114802659378>",
    "Spider": "<:Spider:1489775200311578735>",
    "Sound": "<:Sound:1489775045642293310>",
    "Phoenix": "<:Pheonix:1489774884069445753>",
    "Portal": "<:Portal:1489767034672648315>",
    "Lightning": "<:Lightning:1489767130386792661>",
    "Pain": "<:Pain:1489773733748150302>",
    "Blizzard": "<:Blizzard:1489774599963934923>",
    "Gravity": "<:Gravity:1489773919723589732>",
    "Mammoth": "<:Mammoth:1489774047515377754>",
    "T-Rex": "<:Trex:1489773834901913621>",
    "Dough": "<:Dough:1489765929268023337>",
    "Shadow": "<:Shadow:1489768214643736706>",
    "Venom": "<:Venom:1489766853344493668>",
    "Control": "<:Control:1489768311171317820>",
    "Spirit": "<:Spirit:1489766942892884079>",
    "Dragon": "<:Dragonwest:1489765807427813476>",
    "West Dragon": "<:Dragonwest:1489765807427813476>",
    "East Dragon": "<:Dragoneast:1489765845990375575>",
    "Kitsune": "<:Kitsune:1489772572038922270>",
    "Yeti": "<:Yeti:1489773005793132544>",
    "Gas": "<:Gas:1489773497659035710>",
    "Tiger": "<:Tiger:1489766725284266004>",
    "Eagle": "<:Eagle:1489775590876643380>",
    "Creation": "<:Creation:1489774636228018402>",
    "Magnet": "<:magnet:1561898897436188683>",
}

# Ping ONLY for high-value fruits (Mythicals + top Legendaries).
# Mid-tier like Quake / Magma / Sound / Spider do NOT ping.
GOOD_STOCK_FRUITS = {
    "Dough", "Shadow", "Venom", "Control", "Spirit",
    "Dragon", "West Dragon", "East Dragon",
    "Kitsune", "Yeti", "Gas", "Tiger", "Leopard", "Magnet",
    "T-Rex", "Mammoth", "Buddha", "Portal",
}

# Official in-game Beli shop prices (approx; used for stock embeds)
FRUIT_BELI_PRICES: dict[str, int] = {
    "Rocket": 5_000,
    "Spin": 7_500,
    "Blade": 30_000,
    "Spring": 60_000,
    "Bomb": 80_000,
    "Smoke": 100_000,
    "Spike": 180_000,
    "Flame": 250_000,
    "Ice": 350_000,
    "Sand": 420_000,
    "Dark": 500_000,
    "Eagle": 550_000,
    "Diamond": 600_000,
    "Light": 650_000,
    "Rubber": 750_000,
    "Ghost": 940_000,
    "Magma": 960_000,
    "Quake": 1_000_000,
    "Buddha": 1_200_000,
    "Love": 1_300_000,
    "Creation": 1_400_000,
    "Spider": 1_500_000,
    "Sound": 1_700_000,
    "Phoenix": 1_800_000,
    "Portal": 1_900_000,
    "Lightning": 2_100_000,
    "Pain": 2_300_000,
    "Blizzard": 2_400_000,
    "Gravity": 2_300_000,
    "Mammoth": 2_700_000,
    "T-Rex": 2_700_000,
    "Dough": 2_800_000,
    "Shadow": 2_900_000,
    "Venom": 3_000_000,
    "Control": 3_200_000,
    "Spirit": 3_400_000,
    "Dragon": 15_000_000,
    "West Dragon": 15_000_000,
    "East Dragon": 15_000_000,
    "Kitsune": 8_000_000,
    "Yeti": 5_000_000,
    "Gas": 3_200_000,
    "Tiger": 5_000_000,
    "Leopard": 5_000_000,
    "Magnet": 3_500_000,
}

# Economy safety
COINFLIP_MAX_BET = 50_000
PAY_MAX_AMOUNT = 1_000_000

# Gemini is always tried first. Groq is optional and only used if Gemini fails.
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_SECONDARY_MODEL = "gemini-3.6-flash"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# These model names are cross-referenced against the original 69-page bot.
GROQ_FALLBACK_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
GIVEAWAY_TICKET_ROLE_ID = 1555697494963523614

COMMON_FRUITS = ["Rocket", "Spin", "Blade", "Spring", "Bomb", "Spike", "Smoke"]
UNCOMMON_FRUITS = ["Flame", "Ice", "Sand", "Dark", "Diamond", "Eagle"]
RARE_FRUITS = ["Ghost", "Light", "Magma", "Rubber"]
LEGENDARY_FRUITS = [
    "Quake", "Buddha", "Creation", "Spider", "Sound", "Portal",
    "Lightning", "Pain", "Blizzard", "Phoenix", "Love",
]
MYTHICAL_FRUITS = [
    "Gravity", "T-Rex", "Mammoth", "Dough", "Shadow", "Venom", "Gas",
    "Control", "Spirit", "Yeti", "Tiger", "Kitsune", "Dragon", "Magnet",
]

# Fallback market data used when scraping fails
FALLBACK_FRUIT_VALUES = [
    ("West Dragon", "3.68B", "10/10"),
    ("East Dragon", "3.12B", "10/10"),
    ("Kitsune", "584M", "10/10"),
    ("Magnet", "675M", "8/10"),
    ("Yeti", "110M", "7/10"),
    ("Dough", "31.9M", "9/10"),
    ("T-Rex", "21.3M", "8/10"),
    ("Venom", "25.6M", "9/10"),
    ("Spirit", "10M", "7/10"),
    ("Control", "139M", "8/10"),
    ("Buddha", "10.6M", "10/10"),
    ("Portal", "10.6M", "10/10"),
    ("Tiger", "125M", "7/10"),
    ("Gas", "63.8M", "9/10"),
]

def _clean_env_key(name: str) -> str:
    """Strip whitespace and accidental quotes from env secrets."""
    val = os.environ.get(name, "") or ""
    val = val.strip().strip('"').strip("'").strip()
    return val


gemini_api_key = _clean_env_key("GEMINI_API_KEY")
groq_api_key = _clean_env_key("GROQ_API_KEY")
gemini_client = None
if gemini_api_key and _HAS_GENAI:
    try:
        gemini_client = genai.Client(api_key=gemini_api_key)
        log.info(
            "GEMINI_API_KEY loaded (starts %s… ends …%s)",
            gemini_api_key[:4],
            gemini_api_key[-4:],
        )
    except Exception as e:
        log.warning("Gemini client init failed: %s", e)
elif gemini_api_key and not _HAS_GENAI:
    log.warning("GEMINI_API_KEY set but google-genai package missing in build")
if not gemini_client:
    log.warning("Gemini is not configured — set GEMINI_API_KEY and install google-genai")
if groq_api_key:
    log.info("Optional Groq emergency fallback is configured")

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE LAYER
# ═══════════════════════════════════════════════════════════════════════════════

class Database:
    """Single shared aiosqlite connection with a lock for safe concurrent use."""

    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._init_schema()
        await self._seed_store()
        log.info("Database ready (%s)", self.path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def cursor(self):
        if not self._db:
            raise RuntimeError("Database not connected")
        async with self._lock:
            cur = await self._db.cursor()
            try:
                yield cur
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
            finally:
                await cur.close()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return cur

    async def executemany(self, sql: str, seq) -> None:
        async with self.cursor() as cur:
            await cur.executemany(sql, seq)

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _init_schema(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS active_giveaways (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                prize2 TEXT,
                winners_count INTEGER NOT NULL DEFAULT 1,
                host_id INTEGER NOT NULL,
                custom_end TEXT,
                role_multipliers TEXT DEFAULT '{}',
                required_role_id INTEGER,
                end_time TEXT NOT NULL,
                paused INTEGER DEFAULT 0,
                remaining_seconds INTEGER NOT NULL,
                entrants TEXT DEFAULT '{}'
            )""",
            """CREATE TABLE IF NOT EXISTS ended_giveaways (
                message_id INTEGER PRIMARY KEY,
                prize TEXT,
                winners_count INTEGER,
                entrants TEXT,
                ended_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS blacklisted_users (
                user_id INTEGER PRIMARY KEY
            )""",
            """CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                tournaments_won INTEGER DEFAULT 0,
                quests_completed INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS user_inventory (
                user_id INTEGER,
                item_name TEXT,
                quantity INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, item_name)
            )""",
            """CREATE TABLE IF NOT EXISTS df_giveaway_tickets (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS df_giveaway_ticket_usage (
                giveaway_message_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (giveaway_message_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS user_cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_work TEXT,
                last_daily TEXT,
                last_fish TEXT,
                last_role_claim TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS temp_roles (
                user_id INTEGER,
                role_id INTEGER,
                expires_at TEXT,
                PRIMARY KEY (user_id, role_id)
            )""",
            """CREATE TABLE IF NOT EXISTS role_daily_rewards (
                role_id INTEGER PRIMARY KEY,
                daily_amount INTEGER
            )""",
            """CREATE TABLE IF NOT EXISTS sticky_messages (
                channel_id INTEGER PRIMARY KEY,
                title TEXT,
                description TEXT,
                interval INTEGER,
                message_count INTEGER DEFAULT 0,
                last_msg_id INTEGER
            )""",
            """CREATE TABLE IF NOT EXISTS custom_store_items (
                item_key TEXT PRIMARY KEY,
                name TEXT,
                price INTEGER,
                description TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS user_ai_memory (
                user_id INTEGER PRIMARY KEY,
                conversation_history TEXT,
                custom_notes TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS user_chat_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                archived_at TEXT,
                chat_chunk TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS live_fruit_values (
                fruit_name TEXT PRIMARY KEY,
                trade_value TEXT,
                demand TEXT,
                beli_price TEXT,
                last_updated TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS channel_command_restrictions (
                command_name TEXT,
                channel_id INTEGER,
                restriction_type TEXT,
                PRIMARY KEY (command_name, channel_id)
            )""",
        ]
        for stmt in statements:
            await self._db.execute(stmt)
        await self._db.commit()

    async def _seed_store(self) -> None:
        row = await self.fetchone("SELECT COUNT(*) AS c FROM custom_store_items")
        if row and row["c"] == 0:
            defaults = [
                ("tournament_ticket", "⚔️ Tournament ticket", 100,
                 "PvP challenge ticket. Can earn 5000 DC + tournament winner role (800 DC/day). Expires in a week."),
                ("giveaway_entry", "🎟️ Giveaway entry", 1000,
                 "Extra giveaway entry for future giveaways."),
                ("fruit_roll", "🎰 Fruit roll", 5000,
                 "Free fruit role — open a support ticket to claim."),
                ("dark_supporter", "👹 Dark supporter role", 10000,
                 "Earns 250 DC per day. Member role."),
                ("dark_vip", "👑 Dark VIP role", 25000,
                 "Royalty rank in the server."),
                ("mythical_fruit", "🐉 Random Mythical fruit", 50000,
                 "Random mythical fruit — open a support ticket to claim."),
            ]
            await self.executemany(
                "INSERT OR REPLACE INTO custom_store_items (item_key, name, price, description) VALUES (?, ?, ?, ?)",
                defaults,
            )


db = Database(DB_PATH)

# ═══════════════════════════════════════════════════════════════════════════════
# CACHES (reduce DB hits)
# ═══════════════════════════════════════════════════════════════════════════════

BLACKLISTED_USERS: set[int] = set()
# command_name -> {"disabled": set[channel_id], "only_here": set[channel_id]}
_restriction_cache: dict[str, dict[str, set[int]]] = {}
_sticky_cache: dict[int, dict] = {}  # channel_id -> sticky row dict
_cache_lock = asyncio.Lock()


async def refresh_blacklist() -> None:
    rows = await db.fetchall("SELECT user_id FROM blacklisted_users")
    BLACKLISTED_USERS.clear()
    BLACKLISTED_USERS.update(r["user_id"] for r in rows)


async def refresh_restriction_cache() -> None:
    rows = await db.fetchall(
        "SELECT command_name, channel_id, restriction_type FROM channel_command_restrictions"
    )
    async with _cache_lock:
        _restriction_cache.clear()
        for r in rows:
            entry = _restriction_cache.setdefault(
                r["command_name"], {"disabled": set(), "only_here": set()}
            )
            entry[r["restriction_type"]].add(r["channel_id"])


async def refresh_sticky_cache() -> None:
    rows = await db.fetchall("SELECT * FROM sticky_messages")
    async with _cache_lock:
        _sticky_cache.clear()
        for r in rows:
            _sticky_cache[r["channel_id"]] = dict(r)


# ═══════════════════════════════════════════════════════════════════════════════
# ECONOMY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_user_data(user_id: int) -> dict:
    row = await db.fetchone(
        "SELECT balance, tournaments_won, quests_completed FROM user_wallets WHERE user_id = ?",
        (user_id,),
    )
    if row:
        return {
            "balance": row["balance"],
            "tournaments_won": row["tournaments_won"],
            "quests_completed": row["quests_completed"],
        }
    return {"balance": 0, "tournaments_won": 0, "quests_completed": 0}


async def update_user_balance(user_id: int, amount: int) -> int:
    data = await get_user_data(user_id)
    new_bal = max(0, data["balance"] + amount)
    await db.execute(
        """INSERT INTO user_wallets (user_id, balance, tournaments_won, quests_completed)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance""",
        (user_id, new_bal, data["tournaments_won"], data["quests_completed"]),
    )
    return new_bal


async def add_inventory_item(user_id: int, item_name: str, qty: int = 1) -> None:
    row = await db.fetchone(
        "SELECT quantity FROM user_inventory WHERE user_id = ? AND item_name = ?",
        (user_id, item_name),
    )
    if row:
        await db.execute(
            "UPDATE user_inventory SET quantity = ? WHERE user_id = ? AND item_name = ?",
            (row["quantity"] + qty, user_id, item_name),
        )
    else:
        await db.execute(
            "INSERT INTO user_inventory (user_id, item_name, quantity) VALUES (?, ?, ?)",
            (user_id, item_name, qty),
        )


async def grant_giveaway_ticket(guild: discord.Guild | None, user: discord.abc.User) -> int:
    """Persist a purchased ticket and grant the ticket role in this server."""
    if guild is None:
        return 0
    row = await db.fetchone(
        "SELECT quantity FROM df_giveaway_tickets WHERE guild_id = ? AND user_id = ?",
        (guild.id, user.id),
    )
    quantity = (int(row["quantity"]) if row else 0) + 1
    await db.execute(
        "INSERT INTO df_giveaway_tickets (guild_id, user_id, quantity) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET quantity = excluded.quantity",
        (guild.id, user.id, quantity),
    )
    role = guild.get_role(GIVEAWAY_TICKET_ROLE_ID)
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    if role and member and role not in member.roles:
        try:
            await member.add_roles(role, reason="Purchased giveaway entry ticket")
        except discord.Forbidden:
            log.warning("Cannot assign giveaway ticket role %s; check bot role hierarchy/permissions", role.id)
        except discord.HTTPException as exc:
            log.warning("Could not assign giveaway ticket role: %s", exc)
    elif not role:
        log.warning("Giveaway ticket role %s was not found in guild %s", GIVEAWAY_TICKET_ROLE_ID, guild.id)
    return quantity


async def consume_giveaway_tickets_for_entrants(message_id: int, guild: discord.Guild, entrants: dict[int, int]) -> None:
    """Consume one purchased ticket per entrant, at most once per giveaway."""
    for user_id in entrants:
        # The primary key prevents duplicate consumption if a conclusion is retried.
        try:
            await db.execute(
                "INSERT INTO df_giveaway_ticket_usage (giveaway_message_id, guild_id, user_id) VALUES (?, ?, ?)",
                (message_id, guild.id, user_id),
            )
        except Exception:
            # Already consumed for this giveaway (or DB issue); don't double-charge.
            continue
        row = await db.fetchone(
            "SELECT quantity FROM df_giveaway_tickets WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        if not row or int(row["quantity"]) <= 0:
            continue
        remaining = max(0, int(row["quantity"]) - 1)
        await db.execute(
            "UPDATE df_giveaway_tickets SET quantity = ? WHERE guild_id = ? AND user_id = ?",
            (remaining, guild.id, user_id),
        )
        member = guild.get_member(user_id)
        role = guild.get_role(GIVEAWAY_TICKET_ROLE_ID)
        if not member or not role:
            continue
        try:
            if remaining > 0:
                if role not in member.roles:
                    await member.add_roles(role, reason="Member has more unused giveaway tickets")
            elif role in member.roles:
                await member.remove_roles(role, reason="Giveaway ticket used; no unused tickets remain")
        except discord.Forbidden:
            log.warning("Cannot update giveaway ticket role for member %s; check bot role hierarchy/permissions", user_id)
        except discord.HTTPException as exc:
            log.warning("Could not update giveaway ticket role for member %s: %s", user_id, exc)


async def get_user_inventory(user_id: int) -> list[tuple[str, int]]:
    rows = await db.fetchall(
        "SELECT item_name, quantity FROM user_inventory WHERE user_id = ?", (user_id,)
    )
    return [(r["item_name"], r["quantity"]) for r in rows]


async def add_tournament_win(user_id: int, count: int = 1) -> int:
    data = await get_user_data(user_id)
    new_wins = data["tournaments_won"] + count
    await db.execute(
        """INSERT INTO user_wallets (user_id, balance, tournaments_won, quests_completed)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET tournaments_won = excluded.tournaments_won""",
        (user_id, data["balance"], new_wins, data["quests_completed"]),
    )
    return new_wins


async def get_cooldowns(user_id: int) -> tuple:
    row = await db.fetchone(
        "SELECT last_work, last_daily, last_fish, last_role_claim FROM user_cooldowns WHERE user_id = ?",
        (user_id,),
    )
    if row:
        return row["last_work"], row["last_daily"], row["last_fish"], row["last_role_claim"]
    return None, None, None, None


async def update_cooldown(
    user_id: int,
    last_work=None,
    last_daily=None,
    last_fish=None,
    last_role_claim=None,
) -> None:
    w, d, f, r = await get_cooldowns(user_id)
    await db.execute(
        """INSERT OR REPLACE INTO user_cooldowns
           (user_id, last_work, last_daily, last_fish, last_role_claim)
           VALUES (?, ?, ?, ?, ?)""",
        (
            user_id,
            last_work if last_work is not None else w,
            last_daily if last_daily is not None else d,
            last_fish if last_fish is not None else f,
            last_role_claim if last_role_claim is not None else r,
        ),
    )


async def get_user_memory(user_id: int) -> tuple[list, str, list]:
    row = await db.fetchone(
        "SELECT conversation_history, custom_notes FROM user_ai_memory WHERE user_id = ?",
        (user_id,),
    )
    archives = await db.fetchall(
        "SELECT chat_chunk FROM user_chat_archives WHERE user_id = ? ORDER BY id DESC LIMIT 3",
        (user_id,),
    )
    history: list = []
    notes = ""
    if row:
        try:
            history = json.loads(row["conversation_history"] or "[]")
            if not isinstance(history, list):
                history = []
        except json.JSONDecodeError:
            history = []
        notes = row["custom_notes"] or ""
    archive_list = []
    for a in archives:
        try:
            archive_list.append(json.loads(a["chat_chunk"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return history, notes, archive_list


async def save_user_memory(user_id: int, history: list, notes: str) -> None:
    if len(history) > 20:
        older = history[:10]
        history = history[10:]
        await db.execute(
            "INSERT INTO user_chat_archives (user_id, archived_at, chat_chunk) VALUES (?, ?, ?)",
            (user_id, datetime.datetime.now(datetime.timezone.utc).isoformat(), json.dumps(older)),
        )
    await db.execute(
        """INSERT INTO user_ai_memory (user_id, conversation_history, custom_notes)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             conversation_history = excluded.conversation_history,
             custom_notes = excluded.custom_notes""",
        (user_id, json.dumps(history), notes),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════════════════════════

def is_staff(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    return any(role.id == STAFF_ROLE_ID for role in member.roles)


def parse_duration(duration_str: str) -> int:
    duration_str = (duration_str or "").lower().strip()
    if not duration_str:
        return 0
    try:
        unit = duration_str[-1]
        if unit == "s":
            return int(duration_str[:-1])
        if unit == "m":
            return int(duration_str[:-1]) * 60
        if unit == "h":
            return int(duration_str[:-1]) * 3600
        if unit == "d":
            return int(duration_str[:-1]) * 86400
        return int(duration_str)
    except ValueError:
        return 0


def parse_extra_entries(text: str | None) -> dict:
    multipliers: dict = {}
    if not text:
        return multipliers
    for entry in re.split(r"[,;]", text):
        entry = entry.strip()
        if not entry:
            continue
        match = re.search(r"(?:<@&?(\d+)>|([^\d\s]+))\s+(\d+)", entry)
        if match:
            target = match.group(1) or match.group(2)
            val = int(match.group(3))
            if target.isdigit():
                multipliers[int(target)] = val
            else:
                multipliers[target] = val
    return multipliers


def draw_winners(entrants_dict: dict[int, int], num_winners: int) -> list[int]:
    if not entrants_dict:
        return []
    ticket_pool: list[int] = []
    for uid, weight in entrants_dict.items():
        ticket_pool.extend([uid] * max(1, int(weight)))
    winners: list[int] = []
    actual = min(num_winners, len(entrants_dict))
    pool = ticket_pool[:]
    while len(winners) < actual and pool:
        pick = random.choice(pool)
        if pick not in winners:
            winners.append(pick)
        pool = [u for u in pool if u != pick]
    return winners


def check_channel_restriction():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.command:
            return True
        if isinstance(interaction.user, discord.Member) and is_staff(interaction.user):
            return True

        cmd_name = interaction.command.name
        channel_id = interaction.channel_id

        async with _cache_lock:
            entry = _restriction_cache.get(cmd_name)
            if not entry:
                return True
            if channel_id in entry["disabled"]:
                await interaction.response.send_message(
                    "❌ This command has been disabled in this channel.", ephemeral=True
                )
                return False
            if entry["only_here"] and channel_id not in entry["only_here"]:
                mentions = ", ".join(f"<#{c}>" for c in entry["only_here"])
                await interaction.response.send_message(
                    f"❌ This command can only be used in: {mentions}", ephemeral=True
                )
                return False
        return True

    return app_commands.check(predicate)


# ═══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class GiveawayManager:
    def __init__(self):
        self.active: dict[int, dict] = {}
        self.lock = asyncio.Lock()
        self._embed_update_tasks: dict[int, asyncio.Task] = {}

    async def save(self, msg_id: int, data: dict) -> None:
        view: DynamicGiveawayView = data["view"]
        await db.execute(
            """INSERT OR REPLACE INTO active_giveaways
               (message_id, channel_id, prize, prize2, winners_count, host_id, custom_end,
                role_multipliers, required_role_id, end_time, paused, remaining_seconds, entrants)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_id,
                data["channel"].id,
                data["prize"],
                data.get("prize2"),
                data["winners_count"],
                data["host_user"].id if hasattr(data["host_user"], "id") else 0,
                data.get("custom_end", ""),
                json.dumps({str(k): v for k, v in data.get("role_multipliers", {}).items()}),
                view.required_role.id if view.required_role else None,
                data["end_time"].isoformat(),
                1 if data.get("paused") else 0,
                data["remaining_seconds"],
                json.dumps({str(k): v for k, v in view.entrants.items()}),
            ),
        )

    async def update_entrants(self, msg_id: int, entrants: dict) -> None:
        await db.execute(
            "UPDATE active_giveaways SET entrants = ? WHERE message_id = ?",
            (json.dumps({str(k): v for k, v in entrants.items()}), msg_id),
        )

    async def remove(self, msg_id: int) -> None:
        await db.execute("DELETE FROM active_giveaways WHERE message_id = ?", (msg_id,))

    async def archive_ended(self, msg_id: int, prize: str, winners_count: int, entrants: dict) -> None:
        await db.execute(
            """INSERT OR REPLACE INTO ended_giveaways (message_id, prize, winners_count, entrants, ended_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                msg_id,
                prize,
                winners_count,
                json.dumps({str(k): v for k, v in entrants.items()}),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )

    async def get_ended_entrants(self, msg_id: int) -> Optional[dict[int, int]]:
        row = await db.fetchone(
            "SELECT entrants FROM ended_giveaways WHERE message_id = ?", (msg_id,)
        )
        if not row:
            return None
        try:
            raw = json.loads(row["entrants"] or "{}")
            return {int(k): int(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError):
            return None

    def schedule_embed_update(self, msg_id: int, view: "DynamicGiveawayView") -> None:
        """Debounce embed edits so rapid joins don't hammer the API."""
        existing = self._embed_update_tasks.get(msg_id)
        if existing and not existing.done():
            existing.cancel()

        async def _do_update():
            await asyncio.sleep(1.5)  # coalesce bursts
            if msg_id not in self.active:
                return
            if not view.embed or not view.message_ref:
                return
            total_tickets = sum(view.entrants.values())
            total_users = len(view.entrants)
            stats = f"🎟️ Total Tickets: `{total_tickets}`\n👥 Participants: `{total_users} Users`"
            found = False
            for i, field in enumerate(view.embed.fields):
                if field.name == "📊 Live Entry Stats":
                    view.embed.set_field_at(i, name="📊 Live Entry Stats", value=stats, inline=False)
                    found = True
                    break
            if not found:
                view.embed.add_field(name="📊 Live Entry Stats", value=stats, inline=False)
            try:
                await view.message_ref.edit(embed=view.embed)
            except discord.HTTPException as e:
                log.debug("Giveaway embed update failed for %s: %s", msg_id, e)

        self._embed_update_tasks[msg_id] = asyncio.create_task(_do_update())


giveaways = GiveawayManager()


# ═══════════════════════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

class LeaveGiveawayView(discord.ui.View):
    def __init__(self, parent: "DynamicGiveawayView", message_id: int):
        super().__init__(timeout=120)
        self.parent = parent
        self.message_id = message_id

    @discord.ui.button(label="🚪 Yes, Leave Giveaway", style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        async with giveaways.lock:
            if user_id not in self.parent.entrants:
                await interaction.response.edit_message(
                    content="❌ You are not in this giveaway.", view=None
                )
                return
            del self.parent.entrants[user_id]
            await giveaways.update_entrants(self.message_id, self.parent.entrants)

        giveaways.schedule_embed_update(self.message_id, self.parent)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ You have left this giveaway.", view=self
        )


class DynamicGiveawayView(discord.ui.View):
    """Persistent enter button. State lives in GiveawayManager + DB."""

    def __init__(
        self,
        message_id: int | None = None,
        embed: discord.Embed | None = None,
        role_multipliers: dict | None = None,
        required_role: discord.Role | None = None,
    ):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.entrants: dict[int, int] = {}
        self.embed = embed
        self.role_multipliers = role_multipliers or {}
        self.required_role = required_role
        self.message_ref: Optional[discord.Message] = None

    @discord.ui.button(
        label="🎉 Enter Giveaway",
        style=discord.ButtonStyle.success,
        emoji="🍇",
        custom_id="df:giveaway:enter",
    )
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Resolve the concrete view instance from ACTIVE giveaways when possible
        msg_id = interaction.message.id if interaction.message else self.message_id
        view = self
        if msg_id and msg_id in giveaways.active:
            view = giveaways.active[msg_id]["view"]
            view.message_ref = interaction.message

        user_id = interaction.user.id

        if user_id in BLACKLISTED_USERS:
            await interaction.response.send_message(
                "❌ You are blacklisted from giveaways.", ephemeral=True
            )
            return

        if view.required_role and isinstance(interaction.user, discord.Member):
            if view.required_role not in interaction.user.roles:
                await interaction.response.send_message(
                    f"❌ You need {view.required_role.mention} to enter.", ephemeral=True
                )
                return

        async with giveaways.lock:
            if user_id in view.entrants:
                leave = LeaveGiveawayView(view, msg_id)
                await interaction.response.send_message(
                    "⚠️ You are already entered. Leave?",
                    view=leave,
                    ephemeral=True,
                )
                return

            entry_count = 1
            active_name = None
            if isinstance(interaction.user, discord.Member):
                highest = 1
                if user_id in view.role_multipliers:
                    highest = view.role_multipliers[user_id]
                    active_name = interaction.user.display_name
                for role in interaction.user.roles:
                    if role.id in view.role_multipliers:
                        m = view.role_multipliers[role.id]
                        if m > highest:
                            highest = m
                            active_name = role.name
                entry_count = highest

            bonus_entries = 0
            if interaction.guild:
                try:
                    bonus_row = await db.fetchone("SELECT entries FROM df_bonus_entries WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, user_id))
                    bonus_entries = max(0, int(bonus_row["entries"])) if bonus_row else 0
                except Exception:
                    bonus_entries = 0
            entry_count += bonus_entries
            view.entrants[user_id] = entry_count
            await giveaways.update_entrants(msg_id, view.entrants)
            if bonus_entries and interaction.guild:
                await db.execute("UPDATE df_bonus_entries SET entries = MAX(0, entries - ?) WHERE guild_id = ? AND user_id = ?", (bonus_entries, interaction.guild.id, user_id))

        giveaways.schedule_embed_update(msg_id, view)

        bonus = (
            f" (⚡ **{entry_count} tickets**" + (f" including {bonus_entries} bonus entries" if bonus_entries else (f" via {active_name}" if active_name else "")) + ")"
            if entry_count > 1
            else ""
        )
        leave = LeaveGiveawayView(view, msg_id)
        await interaction.response.send_message(
            f"🔮 **Entered!**{bonus}\n*Changed your mind? Use the button below.*",
            view=leave,
            ephemeral=True,
        )


class StoreBuyButton(discord.ui.Button):
    def __init__(self, item_key: str, item_name: str, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"{price:,}",
            emoji="🪙",
            custom_id=f"df:store:buy:{item_key}",
        )
        self.item_key = item_key
        self.item_name = item_name
        self.price = price

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        bal = (await get_user_data(user_id))["balance"]
        if bal < self.price:
            await interaction.response.send_message(
                f"❌ Need `🪙 {self.price:,}` but you have `🪙 {bal:,}`.",
                ephemeral=True,
            )
            return
        await update_user_balance(user_id, -self.price)
        await add_inventory_item(user_id, self.item_name, 1)
        ticket_count = None
        if self.item_key == "giveaway_entry":
            ticket_count = await grant_giveaway_ticket(interaction.guild, interaction.user)
        embed = discord.Embed(
            title="🛍️ Purchase Successful",
            description=(
                f"Acquired **{self.item_name}** for `🪙 {self.price:,}`!"
                + (f"\n🎟️ You now have **{ticket_count} unused giveaway ticket(s)** saved. Your giveaway ticket role has been assigned." if ticket_count is not None else "")
            ),
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.add_field(
            name="Next steps",
            value="Open a support ticket with proof to claim your reward.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StoreView(discord.ui.View):
    def __init__(self, items: list[tuple]):
        super().__init__(timeout=300)
        for item_key, name, price, _desc in items:
            self.add_item(StoreBuyButton(item_key, name, price))


class RerollPromptView(discord.ui.View):
    def __init__(
        self,
        message_id: int,
        entrants: dict,
        prize: str,
        winners_count: int,
        channel: discord.abc.Messageable,
    ):
        super().__init__(timeout=86400)
        self.message_id = message_id
        self.entrants = entrants
        self.prize = prize
        self.winners_count = winners_count
        self.channel = channel

    @discord.ui.button(label="🔄 Reroll Winners", style=discord.ButtonStyle.primary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.entrants:
            await interaction.response.send_message("❌ No entrants.", ephemeral=True)
            return
        new_winners = draw_winners(self.entrants, self.winners_count)
        for uid in new_winners:
            try:
                user = await bot.fetch_user(uid)
                await user.send(
                    f"🎉 You were selected in the 24h **reroll** for **{self.prize}** in **{SERVER_NAME}**! "
                    "Open a support ticket to claim."
                )
            except Exception:
                pass
        if len(new_winners) == 1:
            ann = f"🔄 **REROLL** 🔄\n🎊 Congrats <@{new_winners[0]}>! You won **{self.prize}**!"
        else:
            mentions = ", ".join(f"<@{u}>" for u in new_winners)
            ann = f"🔄 **REROLL** 🔄\n🎊 Congrats {mentions}!"
        try:
            await self.channel.send(ann)
        except Exception:
            pass
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Reroll done.", ephemeral=True)

    @discord.ui.button(label="✅ Keep Current Winners", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Finalized.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════════════════

class DarkFruitsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="df!", intents=intents)

    async def setup_hook(self) -> None:
        await db.connect()
        await refresh_blacklist()
        await refresh_restriction_cache()
        await refresh_sticky_cache()
        # Register a blank persistent enter view so custom_id is known
        self.add_view(DynamicGiveawayView())
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception as e:
            log.warning("Command sync failed: %s", e)


bot = DarkFruitsBot()

# Discord permits at most 100 global top-level slash commands.
# Keep lightweight entertainment utilities under /fun to stay within that limit.
fun_group = app_commands.Group(name="fun", description="Fun and utility commands")
bot.tree.add_command(fun_group)


# ═══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY TIMER & CONCLUSION
# ═══════════════════════════════════════════════════════════════════════════════

async def run_giveaway_timer(msg_id: int) -> None:
    while True:
        async with giveaways.lock:
            data = giveaways.active.get(msg_id)
            if not data:
                return
            if data["paused"]:
                pass
            else:
                data["remaining_seconds"] -= 1
                if data["remaining_seconds"] <= 0:
                    break
        await asyncio.sleep(1)

    async with giveaways.lock:
        data = giveaways.active.pop(msg_id, None)
    if not data:
        return
    await conclude_giveaway(msg_id, data, early=False)


async def conclude_giveaway(msg_id: int, data: dict, early: bool = False) -> None:
    await giveaways.remove(msg_id)
    view: DynamicGiveawayView = data["view"]
    channel = data["channel"]
    host_user = data["host_user"]
    prize = data["prize"]
    prize2 = data.get("prize2")
    winners_count = data["winners_count"]
    custom_end = data.get("custom_end") or "Open a support ticket to claim your prize!"

    try:
        giveaway_msg = await channel.fetch_message(msg_id)
    except Exception:
        log.warning("Could not fetch giveaway message %s", msg_id)
        return

    if not view.entrants:
        embed = discord.Embed(
            title="🛑 Giveaway Expired",
            description="No entries recorded.",
            color=discord.Color.red(),
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        await giveaway_msg.edit(content="🛑 **EXPIRED**", embed=embed, view=None)
        return

    label = f"{prize} & {prize2}" if prize2 else prize
    await giveaways.archive_ended(msg_id, label, winners_count, view.entrants)
    if channel.guild:
        await consume_giveaway_tickets_for_entrants(msg_id, channel.guild, view.entrants)
    bot.loop.create_task(
        schedule_reroll_reminder(host_user, msg_id, view.entrants, label, winners_count, channel)
    )

    winners = draw_winners(view.entrants, winners_count)
    prefix = "ENDED EARLY" if early else "CONCLUDED"

    if winners_count == 1 or not prize2:
        wid = winners[0]
        try:
            u = await bot.fetch_user(wid)
            await u.send(
                f"🎉 **Congratulations!** You won **{prize}** in **{SERVER_NAME}**!\n🔮 {custom_end}"
            )
        except Exception:
            pass
        embed = discord.Embed(
            title=f"🏆 GIVEAWAY {prefix}: {prize}",
            description="The raffle has finished!",
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.add_field(name="👑 Winner", value=f"<@{wid}>", inline=False)
        embed.add_field(name="📢 Host", value=data.get("host_mention", "—"), inline=False)
        embed.add_field(name="🔮 Claim", value=custom_end, inline=False)
        embed.add_field(
            name="📊 Final Pool",
            value=f"🎟️ Tickets: `{sum(view.entrants.values())}`\n👥 Users: `{len(view.entrants)}`",
            inline=False,
        )
        embed.set_footer(text=f"Hosted by {getattr(host_user, 'display_name', 'Staff')}")
        await giveaway_msg.edit(content="🏆 **WINNERS ANNOUNCED**", embed=embed, view=None)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass
    else:
        w1 = winners[0] if winners else None
        w2 = winners[1] if len(winners) > 1 else None
        for wid, p in ((w1, prize), (w2, prize2)):
            if not wid:
                continue
            try:
                u = await bot.fetch_user(wid)
                await u.send(
                    f"🎉 **Congratulations!** You won **{p}** in **{SERVER_NAME}**!\n🔮 {custom_end}"
                )
            except Exception:
                pass
        embed = discord.Embed(
            title=f"🏆 SPLIT GIVEAWAY {prefix}",
            description="Dual raffle finished!",
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.add_field(name=f"🥇 Winner 1 ({prize})", value=f"<@{w1}>" if w1 else "None", inline=False)
        embed.add_field(name=f"🥈 Winner 2 ({prize2})", value=f"<@{w2}>" if w2 else "None", inline=False)
        embed.add_field(name="📢 Host", value=data.get("host_mention", "—"), inline=False)
        embed.add_field(name="🔮 Claim", value=custom_end, inline=False)
        await giveaway_msg.edit(content="🏆 **WINNERS ANNOUNCED**", embed=embed, view=None)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass


async def schedule_reroll_reminder(
    host: discord.User,
    message_id: int,
    entrants: dict,
    prize: str,
    winners_count: int,
    channel,
) -> None:
    await asyncio.sleep(86400)
    try:
        view = RerollPromptView(message_id, entrants, prize, winners_count, channel)
        embed = discord.Embed(
            title="⏰ 24-Hour Giveaway Reroll Check",
            description=(
                f"It has been **24 hours** since your giveaway for **{prize}** ended.\n"
                "Have winners claimed, or would you like to reroll?"
            ),
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        await host.send(embed=embed, view=view)
    except Exception as e:
        log.debug("Reroll reminder DM failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════════

@tasks.loop(hours=24)
async def refresh_bloxfruits_knowledge():
    """Seed the database immediately, then refresh it from the public wiki."""
    import sys
    from pathlib import Path
    # Always load the bundled curated baseline before attempting internet sync.
    seeder = Path(__file__).with_name("seed_bloxfruits_database.py")
    if seeder.exists():
        try:
            proc_seed = await asyncio.create_subprocess_exec(
                sys.executable, str(seeder),
                env={**os.environ, "BLOXFRUITS_KNOWLEDGE_DB": BLOXFRUITS_KNOWLEDGE_DB},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            seed_output, _ = await asyncio.wait_for(proc_seed.communicate(), timeout=30)
            for line in seed_output.decode("utf-8", errors="replace").splitlines():
                log.info("Knowledge seed: %s", line)
            if proc_seed.returncode:
                log.error("Knowledge seed failed with code %s", proc_seed.returncode)
        except Exception as e:
            log.exception("Knowledge seed failed: %s", e)
    script = Path(__file__).with_name("bloxfruits_knowledge_sync.py")
    if not script.exists():
        log.warning("Knowledge sync script missing at %s", script)
        return
    env = os.environ.copy()
    env["BLOXFRUITS_KNOWLEDGE_DB"] = BLOXFRUITS_KNOWLEDGE_DB
    cmd = [sys.executable, str(script), "--db", BLOXFRUITS_KNOWLEDGE_DB,
           "--max-pages", os.environ.get("KNOWLEDGE_MAX_PAGES", "2000")]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=60 * 60)
        for line in output.decode("utf-8", errors="replace").splitlines()[-25:]:
            log.info("Knowledge sync: %s", line)
        if proc.returncode:
            log.warning("Knowledge sync exited with code %s; next scheduled retry in 24h", proc.returncode)
    except asyncio.TimeoutError:
        log.warning("Knowledge sync timed out; terminating process")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except Exception:
        log.exception("Knowledge sync failed; existing database retained and next run will retry")


@refresh_bloxfruits_knowledge.before_loop
async def before_refresh_bloxfruits_knowledge():
    await bot.wait_until_ready()


@tasks.loop(hours=6)
async def update_fruit_values():
    """Refresh trading values from the user's requested Blox Fruits Values page.

    Source: https://bloxfruitsvalues.com/values
    Never replace good cached data with made-up fallback values when scraping fails.
    """
    url = "https://bloxfruitsvalues.com/values"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Referer": "https://bloxfruitsvalues.com/",
    }

    def amount_to_text(value: str) -> str:
        return re.sub(r"\s+", "", value.strip())

    def parse_values(html: str) -> list[tuple[str, str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, tuple[str, str, str, str]] = {}
        # The live page is card-based rather than a simple HTML table. Find each
        # item heading and inspect its nearest card-like ancestor for value data.
        for heading in soup.find_all(["h2", "h3", "h4"]):
            name = heading.get_text(" ", strip=True)
            if not name or len(name) > 60:
                continue
            if name.lower() in {"blox fruits value list", "about blox fruits trading and values"}:
                continue
            node = heading
            card_text = ""
            for _ in range(7):
                if node is None or getattr(node, "parent", None) is None:
                    break
                node = node.parent
                text = node.get_text("\n", strip=True)
                # Avoid grabbing the whole page; a card usually contains these labels.
                if len(text) < 5000 and ("Demand" in text or "Beli Price" in text or "Robux Price" in text):
                    card_text = text
                    if len(text) < 1800:
                        break
            if not card_text:
                continue
            lines = [re.sub(r"\s+", " ", x).strip() for x in card_text.splitlines() if x.strip()]
            # The trading value is generally the first standalone amount after the
            # item heading and controls; exclude Beli/Robux prices and demand scores.
            name_index = next((i for i, line in enumerate(lines) if line.casefold() == name.casefold()), -1)
            tail = lines[name_index + 1:] if name_index >= 0 else lines
            value = ""
            demand = "N/A"
            beli = "N/A"
            status = "Unknown"
            for i, line in enumerate(tail):
                dm = re.search(r"Demand\s*(\d{1,2}/10)", line, re.I)
                if dm:
                    demand = dm.group(1)
                elif re.fullmatch(r"\d{1,2}/10", line):
                    demand = line
                bm = re.search(r"Beli Price\s*[:\n]?\s*([\d.,]+\s*[KMB]?)", line, re.I)
                if bm:
                    beli = amount_to_text(bm.group(1))
                if line.casefold() in {"stable", "overpaid", "underpaid", "unstable", "fluctuating"}:
                    status = line
                # Values displayed by the site use e.g. 3.48B, 570M, 2.5M.
                if not value and re.fullmatch(r"[\d,.]+\s*[KMB]", line, re.I):
                    previous = " ".join(tail[max(0, i-2):i]).casefold()
                    if "beli price" not in previous and "robux price" not in previous:
                        value = amount_to_text(line)
            if value:
                found.setdefault(name.casefold(), (name, value, demand, beli))

        # Fallback parser for JSON-LD or embedded client data, if the card DOM changes.
        if not found:
            for script in soup.find_all("script"):
                raw = script.string or script.get_text() or ""
                if not any(k in raw.lower() for k in ("tradevalue", "demand", "beli price")):
                    continue
                patterns = [
                    r'\{[^{}]{0,1000}?["\'](?:name|fruit|title)["\']\s*:\s*["\']([^"\']+)["\'][^{}]{0,1000}?["\'](?:tradeValue|trade_value|value)["\']\s*:\s*["\']?([\d,.]+\s*[KMB]?)["\']?[^{}]{0,1000}?["\']demand["\']\s*:\s*["\']?(\d{1,2}/10|\d{1,2})',
                ]
                for pat in patterns:
                    for m in re.finditer(pat, raw, re.I):
                        name, value, demand = m.groups()
                        found.setdefault(name.casefold(), (name, amount_to_text(value), demand, "N/A"))
        return list(found.values())

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                if resp.status != 200:
                    log.warning("Blox Fruits Values source returned HTTP %s; retaining existing cached values", resp.status)
                    return
                html = await resp.text()
        scraped = parse_values(html)
        if len(scraped) < 20:
            log.warning("Blox Fruits Values parser found only %d entries (need at least 20); retaining existing cached values", len(scraped))
            return
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Replace the cache only after a sufficiently large successful scrape. This
        # removes stale names from earlier snapshots without risking an empty table
        # when the source changes layout or blocks the request.
        await db.execute("DELETE FROM live_fruit_values")
        await db.executemany(
            """INSERT OR REPLACE INTO live_fruit_values
               (fruit_name, trade_value, demand, beli_price, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            [(name, value, demand, beli, now) for name, value, demand, beli in scraped],
        )
        log.info("Updated %d Blox Fruits trading values from %s", len(scraped), url)
    except Exception as e:
        log.warning("Blox Fruits Values update failed; retaining existing cached values: %s", e)


@update_fruit_values.before_loop
async def before_fruit_values():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
# BLOX FRUITS STOCK (Normal + Mirage)
# ═══════════════════════════════════════════════════════════════════════════════

# Cached last posted stock so we only announce on change
_last_stock_signature: str | None = None
# Live stock cache for AI (avoid network fetch on every mention)
_cached_stock: tuple[list[str], list[str]] = ([], [])
_cached_stock_at: float = 0.0


def _next_stock_reset(kind: str) -> datetime.datetime:
    """Normal = every 4h at 0/4/8/12/16/20 UTC; Mirage = every 2h on even hours."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if kind == "normal":
        hours = [0, 4, 8, 12, 16, 20]
    else:
        hours = list(range(0, 24, 2))
    for h in hours:
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)


def _format_countdown(target: datetime.datetime) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    secs = max(0, int((target - now).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    if h > 0:
        return f"{h} hour{'s' if h != 1 else ''}" if m == 0 else f"{h}h {m}m"
    return f"{m} minute{'s' if m != 1 else ''}"


def _fruit_line(name: str) -> str:
    emoji = FRUIT_EMOJIS.get(name, "🍇")
    price = FRUIT_BELI_PRICES.get(name)
    if price is not None:
        return f"{emoji} **{name}** — `$ {price:,}`"
    return f"{emoji} **{name}**"


def _stock_embed(title: str, fruits: list[str], kind: str) -> discord.Embed:
    nxt = _next_stock_reset(kind)
    countdown = _format_countdown(nxt)
    local_hint = nxt.strftime("%-I:%M %p").lstrip("0") if hasattr(nxt, "strftime") else nxt.strftime("%H:%M")
    # Portable time format
    local_hint = nxt.strftime("%H:%M UTC")
    lines = [_fruit_line(f) for f in fruits] if fruits else ["*No fruits reported*"]
    embed = discord.Embed(
        title=title,
        description=(
            f"**NEXT** update in **{countdown}** · at `{local_hint}`\n\n"
            + "\n".join(lines)
        ),
        color=DARK_FRUITS_PURPLE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_footer(text=f"{SERVER_NAME} Stock Tracker", icon_url=THUMBNAIL_URL)
    return embed


def _items_from_rotation(entry) -> list[str]:
    """Extract fruit names from one rotation object."""
    if not isinstance(entry, dict):
        return []
    items = entry.get("items") or entry.get("fruits") or []
    names = []
    for it in items:
        if isinstance(it, dict):
            n = it.get("name") or it.get("fruit") or it.get("item")
            if n:
                names.append(str(n).strip())
        elif isinstance(it, str):
            names.append(it.strip())
    return names


async def _fetch_stock_from_api(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """bloxfruit.io JSON — current rotation is index 0 of each list."""
    url = "https://bloxfruit.io/api/stock"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DarkFruitsBot/2.0)",
        "Accept": "application/json",
    }
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            return [], []
        data = await resp.json(content_type=None)

    normal: list[str] = []
    mirage: list[str] = []
    n_list = data.get("normal") or []
    m_list = data.get("mirage") or []
    if isinstance(n_list, list) and n_list:
        normal = _items_from_rotation(n_list[0] if isinstance(n_list[0], dict) else {"items": n_list})
    if isinstance(m_list, list) and m_list:
        mirage = _items_from_rotation(m_list[0] if isinstance(m_list[0], dict) else {"items": m_list})
    return normal, mirage


async def _fetch_stock_from_wiki(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """Parse blox-fruits-wiki current Normal / Mirage sections (includes Rocket/Spin)."""
    url = "https://blox-fruits-wiki.com/wiki/stock/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DarkFruitsBot/2.0)"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status != 200:
            return [], []
        html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = text.splitlines()

    known = sorted(
        set(FRUIT_BELI_PRICES.keys()) | set(FRUIT_EMOJIS.keys()),
        key=len,
        reverse=True,
    )

    def collect(section_lines: list[str]) -> list[str]:
        found: list[str] = []
        blob = "\n".join(section_lines)
        for name in known:
            if re.search(rf"(?i)(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", blob):
                if name not in found:
                    found.append(name)
        return found

    normal_lines: list[str] = []
    mirage_lines: list[str] = []
    mode = None
    for line in lines:
        low = line.lower()
        if "current mirage" in low:
            mode = "mirage"
            continue
        if low.strip() == "current stock" or (
            "current stock" in low and "mirage" not in low and "last" not in low
        ):
            mode = "normal"
            continue
        if "last stock" in low or "last mirage" in low:
            mode = None
            continue
        if "normal stock changes at" in low:
            break
        if mode == "normal":
            normal_lines.append(line)
        elif mode == "mirage":
            mirage_lines.append(line)

    # Limit section size so history doesn't leak in
    normal = collect(normal_lines[:40])
    mirage = collect(mirage_lines[:60])
    return normal, mirage


async def fetch_live_stock() -> tuple[list[str], list[str]]:
    """
    Fetch current Normal + Mirage stock.

    Prefer the wiki (updates closer to real rotations). API is often lagging.
    Rocket & Spin are ALWAYS in stock in-game — always prepended.
    """
    ALWAYS_IN_STOCK = ["Rocket", "Spin"]

    normal: list[str] = []
    mirage: list[str] = []

    try:
        async with aiohttp.ClientSession() as session:
            # 1) Wiki first — usually correct right after a restock
            try:
                n, m = await _fetch_stock_from_wiki(session)
                if n:
                    normal = list(n)
                if m:
                    mirage = list(m)
                log.info("Stock wiki — Normal=%s Mirage=%s", normal, mirage)
            except Exception as e:
                log.warning("Wiki stock failed: %s", e)

            # 2) API fill-in only if a side is still empty
            if not normal or not mirage:
                try:
                    n2, m2 = await _fetch_stock_from_api(session)
                    if not normal and n2:
                        normal = list(n2)
                    if not mirage and m2:
                        mirage = list(m2)
                    log.info("Stock API fill — Normal=%s Mirage=%s", normal, mirage)
                except Exception as e:
                    log.warning("API stock failed: %s", e)
    except Exception as e:
        log.warning("Stock fetch network error: %s", e)

    def with_always(fruits: list[str]) -> list[str]:
        out = list(ALWAYS_IN_STOCK)
        for f in fruits:
            if f not in out:
                out.append(f)
        return out

    normal = with_always(normal)
    # Rocket and Spin are regular dealer staples, not evidence of Mirage stock.
    # Keep Mirage strictly based on the retrieved Mirage rotation.
    mirage = list(dict.fromkeys(mirage))

    if len(normal) > 10:
        normal = normal[:10]
    if len(mirage) > 12:
        mirage = mirage[:12]

    log.info("Final stock — Normal=%s Mirage=%s", normal, mirage)

    global _cached_stock, _cached_stock_at
    _cached_stock = (list(normal), list(mirage))
    _cached_stock_at = datetime.datetime.now(datetime.timezone.utc).timestamp()

    return normal, mirage


async def post_stock_alert(force: bool = False) -> bool:
    """Fetch stock and post TWO messages (Normal, then Mirage) if changed."""
    global _last_stock_signature

    if not STOCK_CHANNEL_ID:
        log.debug("STOCK_CHANNEL_ID not set — skipping stock post")
        return False

    channel = bot.get_channel(STOCK_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(STOCK_CHANNEL_ID)
        except Exception:
            log.warning("Could not fetch stock channel %s", STOCK_CHANNEL_ID)
            return False

    normal, mirage = await fetch_live_stock()
    if not normal and not mirage:
        log.warning("Stock fetch returned empty — not posting")
        return False

    sig = f"N:{','.join(sorted(normal))}|M:{','.join(sorted(mirage))}"
    if not force and sig == _last_stock_signature:
        return False
    _last_stock_signature = sig

    # Ping only when a good fruit is in either stock
    combined = set(normal) | set(mirage)
    good_hits = sorted(combined & GOOD_STOCK_FRUITS)
    content = None
    if STOCK_PING_ROLE_ID and good_hits:
        content = f"<@&{STOCK_PING_ROLE_ID}>"
        log.info("Good fruits in stock → pinging: %s", good_hits)

    embed_n = _stock_embed("Current Stock — Normal", normal, "normal")
    embed_m = _stock_embed("Current Stock — Mirage", mirage, "mirage")

    try:
        # Two separate messages (matches Voltix style)
        await channel.send(content=content, embed=embed_n)
        await channel.send(embed=embed_m)
        log.info("Stock alerts posted as 2 messages (N=%s M=%s)", normal, mirage)
        return True
    except Exception as e:
        log.warning("Failed to post stock alert: %s", e)
        return False


@tasks.loop(minutes=2)
async def poll_fruit_stock():
    """Poll stock trackers every 2 min; post when Normal/Mirage stock changes."""
    try:
        await post_stock_alert(force=False)
    except Exception as e:
        log.warning("Stock poll error: %s", e)


@poll_fruit_stock.before_loop
async def before_stock_poll():
    await bot.wait_until_ready()
    await asyncio.sleep(20)


# Posts once per day at 00:00 GMT (midnight UTC) — does NOT fire on bot restart
@tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc))
async def publish_daily_quests():
    channel = bot.get_channel(QUEST_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(QUEST_CHANNEL_ID)
        except Exception:
            return
    if not channel:
        return

    proof_mention = f"<#{QUEST_PROOF_CHANNEL_ID}>"
    embed = discord.Embed(
        title="🌑 DARK FRUITS — DAILY QUESTS 🌑",
        description=(
            f"Complete the quests below and submit your proof in {proof_mention} "
            "to claim your Dark Coins!"
        ),
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(
        name="🏰 Haunted Mansion Assault",
        value=(
            "• **Task:** Defeat the Soul Reaper or Cake Queen in the Third Sea Haunted Castle.\n"
            f"• **📸 Proof:** Screenshot of the boss defeat banner or drop confirmation → {proof_mention}\n"
            "• **💰 Reward:** `800 DC`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌊 Terror Shark Hunt",
        value=(
            "• **Task:** Defeat a Terror Shark in danger level 5 or 6 waters.\n"
            f"• **📸 Proof:** Screenshot of the victory screen or loot drop → {proof_mention}\n"
            "• **💰 Reward:** `850 DC`"
        ),
        inline=False,
    )
    embed.add_field(
        name="💬 Trade Spotlight",
        value=(
            "• **Task:** Share your current fruit wishlist or availability in the server trade channel.\n"
            f"• **📸 Proof:** Screenshot of your message → {proof_mention}\n"
            "• **💰 Reward:** `350 DC`"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Resets 12:00 AM GMT • {SERVER_NAME} Economy System", icon_url=THUMBNAIL_URL)
    try:
        await channel.send(
            content=(
                f"🔔 @everyone\n✨ **NEW DAILY QUESTS ARE LIVE!** ✨\n"
                f"Submit proof in {proof_mention}"
            ),
            embed=embed,
        )
        log.info("Daily quests published (12:00 AM GMT)")
    except Exception as e:
        log.warning("Failed to publish daily quests: %s", e)


@publish_daily_quests.before_loop
async def before_quests():
    await bot.wait_until_ready()
    # tasks.loop(time=00:00 UTC) waits for the next midnight GMT by itself —
    # it will NOT post on restart, only at 12:00 AM GMT each day.
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    log.info(
        "Daily quests armed for 12:00 AM GMT (next: %s UTC)",
        target.isoformat(),
    )


@tasks.loop(minutes=1)
async def check_temp_roles():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = await db.fetchall(
        "SELECT user_id, role_id FROM temp_roles WHERE expires_at <= ?", (now,)
    )
    for row in rows:
        uid, rid = row["user_id"], row["role_id"]
        for guild in bot.guilds:
            member = guild.get_member(uid)
            role = guild.get_role(rid)
            if member and role:
                try:
                    await member.remove_roles(role, reason="Temp role expired")
                except Exception:
                    pass
        await db.execute(
            "DELETE FROM temp_roles WHERE user_id = ? AND role_id = ?", (uid, rid)
        )


@check_temp_roles.before_loop
async def before_temp_roles():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info("%s is online as %s", SERVER_NAME, bot.user)
    if not refresh_bloxfruits_knowledge.is_running():
        refresh_bloxfruits_knowledge.start()

    # Restore active giveaways
    rows = await db.fetchall("SELECT * FROM active_giveaways")
    for row in rows:
        msg_id = row["message_id"]
        channel = bot.get_channel(row["channel_id"])
        if not channel:
            try:
                channel = await bot.fetch_channel(row["channel_id"])
            except Exception:
                await giveaways.remove(msg_id)
                continue
        try:
            target_msg = await channel.fetch_message(msg_id)
        except Exception:
            await giveaways.remove(msg_id)
            continue

        try:
            role_multipliers = {
                int(k): v for k, v in json.loads(row["role_multipliers"] or "{}").items()
            }
            entrants = {
                int(k): v for k, v in json.loads(row["entrants"] or "{}").items()
            }
        except (json.JSONDecodeError, ValueError):
            role_multipliers, entrants = {}, {}

        guild = channel.guild
        required_role = (
            guild.get_role(row["required_role_id"]) if row["required_role_id"] else None
        )
        host_user = guild.get_member(row["host_id"])
        if not host_user:
            try:
                host_user = await bot.fetch_user(row["host_id"])
            except Exception:
                host_user = bot.user

        embed = target_msg.embeds[0] if target_msg.embeds else None
        view = DynamicGiveawayView(
            message_id=msg_id,
            embed=embed,
            role_multipliers=role_multipliers,
            required_role=required_role,
        )
        view.entrants = entrants
        view.message_ref = target_msg
        bot.add_view(view, message_id=msg_id)

        try:
            end_time = datetime.datetime.fromisoformat(row["end_time"])
        except Exception:
            end_time = datetime.datetime.now(datetime.timezone.utc)

        giveaways.active[msg_id] = {
            "view": view,
            "prize": row["prize"],
            "prize2": row["prize2"],
            "winners_count": row["winners_count"],
            "host_user": host_user,
            "host_mention": getattr(host_user, "mention", str(host_user)),
            "custom_end": row["custom_end"],
            "role_multipliers": role_multipliers,
            "end_time": end_time,
            "paused": bool(row["paused"]),
            "remaining_seconds": row["remaining_seconds"],
            "channel": channel,
        }
        bot.loop.create_task(run_giveaway_timer(msg_id))

    if not update_fruit_values.is_running():
        update_fruit_values.start()
    if not publish_daily_quests.is_running():
        publish_daily_quests.start()
    if not check_temp_roles.is_running():
        check_temp_roles.start()
    if not poll_fruit_stock.is_running():
        poll_fruit_stock.start()

    # One-shot refresh on boot
    bot.loop.create_task(update_fruit_values())

    # Force-post current stock shortly after ready (gives guild cache time to load)
    async def _startup_stock():
        await asyncio.sleep(8)
        try:
            ok = await post_stock_alert(force=True)
            log.info("Startup stock post: %s", "OK" if ok else "failed/empty")
        except Exception as e:
            log.warning("Startup stock post error: %s", e)

    if STOCK_CHANNEL_ID:
        bot.loop.create_task(_startup_stock())

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="with Mythical Fruits 🍇",
        )
    )


@bot.event
async def on_member_join(member: discord.Member):
    if not giveaways.active:
        return
    # Only DM about the first active giveaway the member can see
    for data in list(giveaways.active.values())[:1]:
        channel = data.get("channel")
        if not channel:
            continue
        try:
            perms = channel.permissions_for(member)
            if not perms.view_channel:
                continue
        except Exception:
            continue
        p1 = data.get("prize", "Mystery Prize")
        p2 = data.get("prize2")
        prize = f"{p1} & {p2}" if p2 else p1
        try:
            embed = discord.Embed(
                title=f"🎉 Welcome to {SERVER_NAME}!",
                description=(
                    f"There is an active giveaway for **{prize}**!\n"
                    f"Head to {channel.mention} to enter."
                ),
                color=DARK_FRUITS_PURPLE,
            )
            embed.set_thumbnail(url=THUMBNAIL_URL)
            await member.send(embed=embed)
        except Exception:
            pass
        break


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id or not message.guild:
        await bot.process_commands(message)
        return

    # ── AI mention handler ──────────────────────────────────────────────────
    bot_mentions = {f"<@{bot.user.id}>", f"<@!{bot.user.id}>"} if bot.user else set()
    is_mentioned = (
        (bot.user and bot.user in message.mentions)
        or any(m in message.content for m in bot_mentions)
    ) and not message.mention_everyone

    if is_mentioned:
        async with message.channel.typing():
            try:
                await _handle_ai_mention(message)
            except Exception:
                log.error("AI handler error:\n%s", traceback.format_exc())
                try:
                    await message.reply(
                        "🤖 **Dark Fruits AI:** The seas are rough right now — try again in a moment!"
                    )
                except Exception:
                    pass

    # ── Sticky messages (cached) ────────────────────────────────────────────
    async with _cache_lock:
        sticky = _sticky_cache.get(message.channel.id)
    if sticky:
        new_count = sticky["message_count"] + 1
        if new_count >= sticky["interval"]:
            if sticky.get("last_msg_id"):
                try:
                    old = await message.channel.fetch_message(sticky["last_msg_id"])
                    await old.delete()
                except Exception:
                    pass
            embed = discord.Embed(
                title=f"📌 {sticky['title']}",
                description=sticky["description"],
                color=DARK_FRUITS_PURPLE,
            )
            embed.set_thumbnail(url=THUMBNAIL_URL)
            try:
                new_msg = await message.channel.send(embed=embed)
                await db.execute(
                    "UPDATE sticky_messages SET message_count = 0, last_msg_id = ? WHERE channel_id = ?",
                    (new_msg.id, message.channel.id),
                )
                sticky["message_count"] = 0
                sticky["last_msg_id"] = new_msg.id
            except Exception:
                pass
        else:
            await db.execute(
                "UPDATE sticky_messages SET message_count = ? WHERE channel_id = ?",
                (new_count, message.channel.id),
            )
            sticky["message_count"] = new_count

    await bot.process_commands(message)


def _ai_needs_fruit_data(text: str) -> bool:
    """True if the question is about Blox Fruits game data / builds."""
    t = (text or "").lower()
    keywords = (
        "stock", "value", "values", "demand", "price", "worth", "trade", "trading",
        "dealer", "mirage", "normal stock", "in stock", "beli", "market",
        "fruit", "fruits", "perm", "permanent",
        "pvp", "pve", "grind", "grinding", "meta", "combo", "build", "setup",
        "sword", "gun", "race", "fighting style", "godhuman", "sanguine",
        "awakening", "v4", "blox", "bloxfruit", "blox fruits",
    )
    if any(k in t for k in keywords):
        return True
    for name in FRUIT_EMOJIS:
        if name.lower() in t:
            return True
    return False


async def _lookup_blox_wiki(query: str) -> str:
    """Pull a short live snippet from the Blox Fruits wiki (not a fixed DB)."""
    # Prefer a fruit page if a known fruit is named
    fruit_hit = None
    qlow = query.lower()
    for name in sorted(FRUIT_EMOJIS.keys(), key=len, reverse=True):
        if name.lower() in qlow:
            fruit_hit = name
            break

    urls = []
    if fruit_hit:
        slug = fruit_hit.replace(" ", "_")
        urls.append(f"https://blox-fruits.fandom.com/wiki/{quote(slug)}")
    if any(k in qlow for k in ("pvp", "meta", "build", "setup", "combo")):
        urls.append("https://blox-fruits.fandom.com/wiki/Blox_Fruits_Wiki")
    urls.append("https://blox-fruits-wiki.com/wiki/stock/")

    snippets: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DarkFruitsBot/3.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            for url in urls[:2]:
                try:
                    async with session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    text = soup.get_text(" ", strip=True)
                    text = re.sub(r"\s+", " ", text)[:1200]
                    if text:
                        snippets.append(f"From {url}: {text}")
                except Exception as e:
                    log.debug("Wiki lookup fail %s: %s", url, e)
    except Exception as e:
        log.debug("Wiki session error: %s", e)

    return "\n".join(snippets)[:2500] if snippets else ""


def _search_blox_knowledge_sync(query: str, limit: int = 5) -> str:
    """Search the locally synced Blox Fruits wiki database; never blocks the event loop."""
    path = BLOXFRUITS_KNOWLEDGE_DB
    if not os.path.exists(path):
        return ""
    stop = {"what", "when", "where", "which", "does", "how", "can", "the", "and", "for", "with", "from", "about", "into", "have", "has", "are", "is", "you", "your", "tell", "me", "please", "blox", "fruits"}
    terms = [w for w in re.findall(r"[a-zA-Z0-9'-]{2,}", query.lower()) if w not in stop]
    if not terms:
        return ""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT title, url, content FROM knowledge_documents").fetchall()
        con.close()
    except (sqlite3.Error, OSError) as e:
        log.debug("Blox knowledge DB unavailable: %s", e)
        return ""
    scored = []
    for row in rows:
        title = (row["title"] or "")
        content = (row["content"] or "")
        title_low, content_low = title.lower(), content.lower()
        score = sum(4 if t in title_low else 0 for t in terms)
        score += sum(min(content_low.count(t), 4) for t in terms)
        if score:
            scored.append((score, title, row["url"], content))
    scored.sort(key=lambda x: x[0], reverse=True)
    parts = []
    for score, title, url, content in scored[:limit]:
        # Extract a relevant window around the first matching term rather than dumping the whole article.
        low = content.lower()
        positions = [low.find(t) for t in terms if low.find(t) >= 0]
        start = max(0, (min(positions) - 220) if positions else 0)
        snippet = re.sub(r"\s+", " ", content[start:start + 850]).strip()
        parts.append(f"[{title}] ({url})\n{snippet}")
    return "\n\n".join(parts)[:4200]


async def _search_blox_knowledge(query: str) -> str:
    return await asyncio.to_thread(_search_blox_knowledge_sync, query)


async def _gemini_chat(messages: list[dict]) -> str | None:
    """Try the configured Gemini model, then one known-good secondary model."""
    if not gemini_client:
        return None

    prompt_parts = []
    for item in messages:
        role = str(item.get("role", "user")).upper()
        content = str(item.get("content", ""))
        prompt_parts.append(f"{role}:\n{content}")
    prompt = "\n\n".join(prompt_parts)
    config = genai_types.GenerateContentConfig(
        temperature=0.35,
        max_output_tokens=320,
    ) if genai_types else None
    configured_model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
    timeout = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "12"))
    models_to_try = [configured_model]
    if configured_model != GEMINI_SECONDARY_MODEL:
        models_to_try.append(GEMINI_SECONDARY_MODEL)

    for model_name in models_to_try:
        attempts = 2 if model_name == configured_model else 1
        for attempt in range(attempts):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        gemini_client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=timeout,
                )
                reply_text = getattr(response, "text", None)
                if reply_text and reply_text.strip():
                    if model_name != configured_model:
                        log.warning("Gemini primary model failed; secondary model %s succeeded", model_name)
                    return reply_text.strip()
                log.warning("Gemini model %s returned empty response (attempt %d/%d)", model_name, attempt + 1, attempts)
            except asyncio.TimeoutError:
                log.warning("Gemini model %s timed out (attempt %d/%d)", model_name, attempt + 1, attempts)
            except Exception as e:
                log.warning("Gemini model %s failed (attempt %d/%d): %s: %s", model_name, attempt + 1, attempts, type(e).__name__, e)
            if attempt == 0 and attempts > 1:
                await asyncio.sleep(1.0)
    return None


async def _groq_fallback_chat(messages: list[dict]) -> tuple[str | None, str | None]:
    """Optional emergency fallback using the original bot's Groq chat-completions API."""
    if not groq_api_key:
        return None, None
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=12)
    for model_name in GROQ_FALLBACK_MODELS:
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": 450,
            "temperature": 0.35,
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(GROQ_API_URL, headers=headers, json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        log.warning("Groq fallback model=%s status=%s body=%s", model_name, resp.status, body[:300])
                        continue
                    data = json.loads(body)
                    choices = data.get("choices") or []
                    if choices:
                        content = str((choices[0].get("message") or {}).get("content") or "").strip()
                        if content:
                            return content, model_name
                    log.warning("Groq fallback model=%s returned empty content", model_name)
        except Exception as e:
            log.warning("Groq fallback model %s failed: %s: %s", model_name, type(e).__name__, e)
    return None, None


async def _handle_ai_mention(message: discord.Message) -> None:
    """Gemini-only AI. Optional stock/wiki context for game questions."""
    user_id = message.author.id

    cleaned = message.content
    for m in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        cleaned = cleaned.replace(m, "")
    cleaned = cleaned.strip() or "Hello"

    needs_data = _ai_needs_fruit_data(cleaned)

    try:
        history, custom_notes, _ = await get_user_memory(user_id)
    except Exception:
        history, custom_notes = [], None

    clean_hist = []
    for h in (history or [])[-4:]:
        if not isinstance(h, dict):
            continue
        t = str(h.get("content", "")).strip()
        if not t:
            continue
        low = t.lower()
        if any(
            x in low
            for x in (
                "treasure maps",
                "seas are choppy",
                "archives are lagging",
                "ai is offline",
                "both ai routes",
                "couldn't reach",
                "choice:*",
            )
        ):
            continue
        clean_hist.append(h)

    extra_data = ""
    wiki_data = ""
    knowledge_data = ""
    if needs_data:
        try:
            value_rows = await db.fetchall(
                "SELECT fruit_name, trade_value, demand FROM live_fruit_values ORDER BY fruit_name"
            )
        except Exception:
            value_rows = []
        market_text = (
            " | ".join(
                f"{r['fruit_name']}:{r['trade_value']}/{r['demand']}"
                for r in value_rows
            )
            if value_rows
            else "updating"
        )
        sn, sm = _cached_stock
        stock_text = (
            f"Normal: {', '.join(sn) or '?'} | Mirage: {', '.join(sm) or '?'}"
            if (sn or sm)
            else "updating"
        )
        extra_data = f"LIVE STOCK: {stock_text}\nLIVE VALUES: {market_text}\n"
        # Run local retrieval and live wiki lookup concurrently. The live wiki is
        # best-effort and can never hold up the answer for several serial seconds.
        results = await asyncio.gather(
            asyncio.wait_for(_search_blox_knowledge(cleaned), timeout=1.8),
            asyncio.wait_for(_lookup_blox_wiki(cleaned), timeout=1.8),
            return_exceptions=True,
        )
        knowledge_data = results[0] if isinstance(results[0], str) else ""
        wiki_data = results[1] if isinstance(results[1], str) else ""

    system_prompt = (
        f"You are Dark Fruits AI for '{SERVER_NAME}', a Blox Fruits (Roblox) Discord.\n"
        "Write complete, useful answers. Pirate tone OK. Use 2-5 emojis 🍇⚔️🐉🔥💎.\n"
        "For PvP/builds always name: Fruit + Fighting Style + Sword (and Race if useful).\n"
        "Real examples: Kitsune, Dough, Dragon, Portal, Buddha, Gas, Yeti; "
        "Godhuman, Sanguine Art, Electric Claw; Tushita, Yama, CDK, Hallow Scythe.\n"
        "Never invent fake fruits. Never mention internal AI providers.\n"
        "Aim for under 120 words. ALWAYS finish the last sentence — never cut off mid-word.\n"
        "Do not output markdown like 'choice:*' or incomplete bullet stubs.\n"
        "For game facts, prioritize the supplied knowledge database and live data. Treat wiki content as community-maintained, not developer-official. "
        "Never invent NPCs, islands, items, unlock requirements, codes, or mechanics. If the provided sources do not support an answer, say you are not certain and ask what detail is needed. "
        "Cite a source page title or URL briefly when using database facts."
    )
    if extra_data:
        system_prompt += f"\n{extra_data}"
    if knowledge_data:
        system_prompt += f"\nSEARCHABLE BLOX FRUITS KNOWLEDGE DATABASE (prioritize these sourced excerpts):\n{knowledge_data}"
    if wiki_data:
        system_prompt += f"\nLIVE WIKI LOOKUP (secondary source): {wiki_data[:600]}"
    if custom_notes:
        system_prompt += f"\nUser notes: {custom_notes}"

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for h in clean_hist[-2:]:
        role = "user" if h.get("role") == "user" else "assistant"
        txt = str(h.get("content", "")).strip()
        if txt:
            messages.append({"role": role, "content": txt})
    messages.append({"role": "user", "content": cleaned})

    if not gemini_client and not groq_api_key:
        await message.reply(
            "🤖 **Dark Fruits AI:**\n"
            "AI isn't configured. Add `GEMINI_API_KEY` in Render Environment and redeploy."
        )
        return

    used = None
    async with message.channel.typing():
        ai_reply = await _gemini_chat(messages) if gemini_client else None
        if ai_reply:
            used = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)
        elif groq_api_key:
            log.warning("Gemini unavailable; trying optional Groq emergency fallback")
            ai_reply, groq_model = await _groq_fallback_chat(messages)
            if ai_reply:
                used = f"groq/{groq_model}"

    if not ai_reply:
        log.error("All configured AI providers failed (Gemini=%s, Groq fallback=%s)", bool(gemini_client), bool(groq_api_key))
        await message.reply(
            "🤖 **Dark Fruits AI:**\n"
            "AI services couldn't answer just now. Please try again shortly."
        )
        return


    log.info("AI success via %s (%d chars)", used, len(ai_reply))

    # Soft trim only if wildly long — never chop mid-word into garbage
    words = ai_reply.split()
    if len(words) > 130:
        ai_reply = " ".join(words[:120])
        if not ai_reply.endswith((".", "!", "?", "…")):
            ai_reply += "…"

    clean_hist.append({"role": "user", "content": cleaned})
    clean_hist.append({"role": "model", "content": ai_reply})
    try:
        await save_user_memory(user_id, clean_hist[-8:], custom_notes)
    except Exception as e:
        log.debug("Memory save failed: %s", e)

    # Discord hard limit is 2000 chars — split long replies safely
    full = f"🤖 **Dark Fruits AI:**\n{ai_reply}"
    if len(full) <= 2000:
        await message.reply(full)
    else:
        # First chunk as reply, rest as follow-ups
        chunks = []
        remaining = full
        while remaining:
            if len(remaining) <= 2000:
                chunks.append(remaining)
                break
            # Split on last newline before 2000 if possible
            cut = remaining.rfind("\n", 0, 1900)
            if cut < 500:
                cut = 1900
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


# ═══════════════════════════════════════════════════════════════════════════════
# ECONOMY COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="balance", description="Check your Dark Coins balance.")
@check_channel_restriction()
@app_commands.describe(member="Optional member to check")
async def balance_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = await get_user_data(target.id)
    embed = discord.Embed(
        title=f"🪙 {target.display_name}'s Vault",
        description=f"### Balance\n```yaml\n{data['balance']:,} Dark Coins (DC)\n```",
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="profile", description="View pirate profile card.")
@check_channel_restriction()
@app_commands.describe(member="Optional member")
async def profile_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = await get_user_data(target.id)
    t_row = await db.fetchone(
        "SELECT expires_at FROM temp_roles WHERE user_id = ? LIMIT 1", (target.id,)
    )
    role_status = "✨ None Active"
    if t_row:
        try:
            exp = datetime.datetime.fromisoformat(t_row["expires_at"])
            role_status = f"⏳ Active until <t:{int(exp.timestamp())}:R>"
        except Exception:
            role_status = "⏳ Active"

    embed = discord.Embed(
        title=f"🏴‍☠️ {target.display_name}'s Dossier",
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(name="🪙 Vault", value=f"```yaml\n{data['balance']:,} DC\n```", inline=True)
    embed.add_field(name="⚔️ Tournament Wins", value=f"```yaml\n{data['tournaments_won']}\n```", inline=True)
    embed.add_field(name="📜 Quests", value=f"```yaml\n{data['quests_completed']}\n```", inline=True)
    embed.add_field(name="👑 Temp Role", value=role_status, inline=False)
    embed.set_footer(text=f"ID: {target.id}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="inventory", description="View your inventory.")
@check_channel_restriction()
async def inventory_cmd(interaction: discord.Interaction):
    items = await get_user_inventory(interaction.user.id)
    embed = discord.Embed(
        title=f"🎒 {interaction.user.display_name}'s Cargo",
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    if not items:
        embed.add_field(name="Empty", value="Use `/fish` to catch fruits!", inline=False)
    else:
        text = "\n".join(f"• **{n}** → `x{q}`" for n, q in items)
        embed.add_field(name="Stored", value=text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="coinflip", description="Gamble Dark Coins on a coin flip.")
@check_channel_restriction()
@app_commands.describe(amount="Wager amount", choice="Heads or tails")
@app_commands.choices(
    choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ]
)
async def coinflip_cmd(interaction: discord.Interaction, amount: int, choice: str):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True)
        return
    if amount > COINFLIP_MAX_BET:
        await interaction.response.send_message(
            f"❌ Max bet is `🪙 {COINFLIP_MAX_BET:,}`.", ephemeral=True
        )
        return
    bal = (await get_user_data(interaction.user.id))["balance"]
    if bal < amount:
        await interaction.response.send_message(
            f"❌ Insufficient funds (`{bal:,}` DC).", ephemeral=True
        )
        return

    outcome = random.choice(["heads", "tails"])
    won = choice.lower() == outcome
    delta = amount if won else -amount
    new_bal = await update_user_balance(interaction.user.id, delta)

    color = discord.Color.green() if won else discord.Color.red()
    title = "Victory!" if won else "Defeat"
    embed = discord.Embed(
        title=f"🪙 Coinflip — {title}",
        description=(
            f"Landed on **{outcome.capitalize()}**.\n"
            f"{'💰 +' if won else '💸 -'}{amount:,} DC\n"
            f"💎 Balance: `{new_bal:,}` DC"
        ),
        color=color,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="fish", description="Fish for Blox Fruits (10 min cooldown).")
@check_channel_restriction()
async def fish_cmd(interaction: discord.Interaction):
    user_id = interaction.user.id
    _, _, last_fish, _ = await get_cooldowns(user_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    if last_fish:
        try:
            last = datetime.datetime.fromisoformat(last_fish)
            diff = (now - last).total_seconds()
            if diff < 600:
                rem = int(600 - diff)
                m, s = divmod(rem, 60)
                await interaction.response.send_message(
                    f"⏳ Cooldown: **{m}m {s}s**.", ephemeral=True
                )
                return
        except Exception:
            pass

    roll = random.random()
    if roll < 0.50:
        fruit, rarity, earned = random.choice(COMMON_FRUITS), "⚪ Common", random.randint(20, 60)
    elif roll < 0.78:
        fruit, rarity, earned = random.choice(UNCOMMON_FRUITS), "🟢 Uncommon", random.randint(70, 150)
    elif roll < 0.92:
        fruit, rarity, earned = random.choice(RARE_FRUITS), "🔵 Rare", random.randint(180, 350)
    elif roll < 0.985:
        fruit, rarity, earned = random.choice(LEGENDARY_FRUITS), "🟡 Legendary", random.randint(500, 1200)
    else:
        fruit, rarity, earned = random.choice(MYTHICAL_FRUITS), "🔴 Mythical", random.randint(2000, 4500)

    await update_user_balance(user_id, earned)
    await add_inventory_item(user_id, fruit, 1)
    await update_cooldown(user_id, last_fish=now.isoformat())
    new_bal = (await get_user_data(user_id))["balance"]

    embed = discord.Embed(
        title="🎣 Catch!",
        description=(
            f"**{fruit}** ({rarity})\n"
            f"💰 +{earned:,} DC\n💎 Balance: `{new_bal:,}` DC"
        ),
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="work", description="Work for Dark Coins (1 hour cooldown).")
@check_channel_restriction()
async def work_cmd(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_work, _, _, _ = await get_cooldowns(user_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    if last_work:
        try:
            last = datetime.datetime.fromisoformat(last_work)
            if (now - last).total_seconds() < 3600:
                rem = int(3600 - (now - last).total_seconds())
                m, s = divmod(rem, 60)
                await interaction.response.send_message(
                    f"⏳ Rest for **{m}m {s}s**.", ephemeral=True
                )
                return
        except Exception:
            pass

    jobs = [
        ("hunted Sea Beasts", random.randint(80, 200)),
        ("scavenged rare fruits", random.randint(70, 180)),
        ("brewed pirate potions", random.randint(90, 220)),
        ("traded at the floating turtle", random.randint(100, 250)),
    ]
    desc, earned = random.choice(jobs)
    await update_user_balance(user_id, earned)
    await update_cooldown(user_id, last_work=now.isoformat())
    new_bal = (await get_user_data(user_id))["balance"]
    embed = discord.Embed(
        title="⛏️ Shift Complete",
        description=f"You {desc} and earned **+{earned:,} DC**.\n💎 `{new_bal:,}` DC",
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="pay", description="Transfer Dark Coins to another member.")
@check_channel_restriction()
@app_commands.describe(member="Recipient", amount="Amount")
async def pay_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True)
        return
    if amount > PAY_MAX_AMOUNT:
        await interaction.response.send_message(
            f"❌ Max transfer is `🪙 {PAY_MAX_AMOUNT:,}`.", ephemeral=True
        )
        return
    if interaction.user.id == member.id:
        await interaction.response.send_message("❌ Cannot pay yourself.", ephemeral=True)
        return
    bal = (await get_user_data(interaction.user.id))["balance"]
    if bal < amount:
        await interaction.response.send_message(
            f"❌ Insufficient funds (`{bal:,}`).", ephemeral=True
        )
        return
    await update_user_balance(interaction.user.id, -amount)
    await update_user_balance(member.id, amount)
    await interaction.response.send_message(
        f"✅ Sent `🪙 {amount:,}` to {member.mention}.", ephemeral=True
    )


@bot.tree.command(name="stock", description="Show current Normal & Mirage Blox Fruits dealer stock.")
@check_channel_restriction()
async def stock_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    normal, mirage = await fetch_live_stock()
    if not normal and not mirage:
        await interaction.followup.send(
            "❌ Couldn't fetch live stock right now. Try again in a minute.",
            ephemeral=True,
        )
        return
    embed_n = _stock_embed("Current Stock — Normal", normal, "normal")
    embed_m = _stock_embed("Current Stock — Mirage", mirage, "mirage")
    await interaction.followup.send(embeds=[embed_n, embed_m], ephemeral=True)


@bot.tree.command(name="stock_force", description="Staff: force-post current stock to the stock channel.")
@check_channel_restriction()
async def stock_force_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if not STOCK_CHANNEL_ID:
        await interaction.response.send_message(
            "❌ `STOCK_CHANNEL_ID` is not configured.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    ok = await post_stock_alert(force=True)
    if ok:
        await interaction.followup.send("✅ Stock alert posted.", ephemeral=True)
    else:
        await interaction.followup.send(
            "⚠️ Fetch/post failed (check logs / channel perms).", ephemeral=True
        )


@bot.tree.command(name="leaderboard", description="Top 10 richest pirates.")
@check_channel_restriction()
async def leaderboard_cmd(interaction: discord.Interaction):
    rows = await db.fetchall(
        "SELECT user_id, balance FROM user_wallets ORDER BY balance DESC LIMIT 10"
    )
    if not rows:
        await interaction.response.send_message("No economy data yet.", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []
    for i, r in enumerate(rows):
        user = bot.get_user(r["user_id"])
        name = user.display_name if user else f"<@{r['user_id']}>"
        lines.append(f"{medals[i]} **{name}** → `🪙 {r['balance']:,}`")
    embed = discord.Embed(
        title=f"🏆 {SERVER_NAME} Leaderboard",
        description="\n".join(lines),
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="claim_daily_role", description="Claim daily DC from your roles.")
@check_channel_restriction()
async def claim_daily_role_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    user_id = interaction.user.id
    _, _, _, last_claim = await get_cooldowns(user_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    if last_claim:
        try:
            last = datetime.datetime.fromisoformat(last_claim)
            if (now - last).total_seconds() < 86400:
                rem = int(86400 - (now - last).total_seconds())
                h, rem = divmod(rem, 3600)
                m, _ = divmod(rem, 60)
                await interaction.response.send_message(
                    f"⏳ Already claimed. Back in **{h}h {m}m**.", ephemeral=True
                )
                return
        except Exception:
            pass

    rewards = await db.fetchall("SELECT role_id, daily_amount FROM role_daily_rewards")
    user_roles = {r.id for r in interaction.user.roles}
    total = 0
    claimed = []
    for r in rewards:
        if r["role_id"] in user_roles:
            total += r["daily_amount"]
            claimed.append(f"<@&{r['role_id']}> (`+{r['daily_amount']:,}`)")
    if total <= 0:
        await interaction.response.send_message(
            "❌ No roles with daily rewards.", ephemeral=True
        )
        return
    await update_user_balance(user_id, total)
    await update_cooldown(user_id, last_role_claim=now.isoformat())
    new_bal = (await get_user_data(user_id))["balance"]
    embed = discord.Embed(
        title="🌟 Role Daily Claimed",
        description="\n".join(claimed) + f"\n\n💰 +{total:,} DC → `{new_bal:,}` DC",
        color=DARK_FRUITS_PURPLE,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STORE
# ═══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="store", description="Browse the Dark Fruits store.")
@check_channel_restriction()
async def store_cmd(interaction: discord.Interaction):
    rows = await db.fetchall(
        "SELECT item_key, name, price, description FROM custom_store_items"
    )
    embed = discord.Embed(
        title="Store",
        description="Click a button to buy, or use `/buy`. Details: `/item_info` if available.",
        color=DARK_FRUITS_PURPLE,
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    if not rows:
        embed.add_field(name="Empty", value="Staff can add items with `/add_store_item`.")
    else:
        for r in rows:
            embed.add_field(
                name=r["name"],
                value=f"{r['description']}\n🏷️ `🪙 {r['price']:,}` · key `{r['item_key']}`",
                inline=False,
            )
    view = StoreView([(r["item_key"], r["name"], r["price"], r["description"]) for r in rows])
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="buy", description="Buy a store item by key.")
@check_channel_restriction()
@app_commands.describe(item_key="Item key (see /store)")
async def buy_cmd(interaction: discord.Interaction, item_key: str):
    item_key = item_key.lower().strip()
    row = await db.fetchone(
        "SELECT name, price FROM custom_store_items WHERE item_key = ?", (item_key,)
    )
    if not row:
        await interaction.response.send_message("❌ Unknown item key.", ephemeral=True)
        return
    bal = (await get_user_data(interaction.user.id))["balance"]
    if bal < row["price"]:
        await interaction.response.send_message(
            f"❌ Need `{row['price']:,}`, have `{bal:,}`.", ephemeral=True
        )
        return
    await update_user_balance(interaction.user.id, -row["price"])
    await add_inventory_item(interaction.user.id, row["name"], 1)
    ticket_count = None
    if item_key == "giveaway_entry":
        ticket_count = await grant_giveaway_ticket(interaction.guild, interaction.user)
    description = f"**{row['name']}** for `🪙 {row['price']:,}`."
    if ticket_count is not None:
        description += f"\n🎟️ You now have **{ticket_count} unused giveaway ticket(s)** saved. Your giveaway ticket role has been assigned."
    else:
        description += "\nOpen a ticket to claim."
    embed = discord.Embed(
        title="🛍️ Purchased",
        description=description,
        color=DARK_FRUITS_PURPLE,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="add_store_item", description="Staff: add store item.")
@check_channel_restriction()
@app_commands.describe(item_key="Unique key", name="Display name", price="Price", description="Desc")
async def add_store_item_cmd(
    interaction: discord.Interaction, item_key: str, name: str, price: int, description: str
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if price < 0:
        await interaction.response.send_message("❌ Price ≥ 0.", ephemeral=True)
        return
    item_key = item_key.lower().strip()
    await db.execute(
        "INSERT OR REPLACE INTO custom_store_items (item_key, name, price, description) VALUES (?, ?, ?, ?)",
        (item_key, name, price, description),
    )
    await interaction.response.send_message(
        f"✅ Added **{name}** (`{item_key}`) for `{price:,}` DC.", ephemeral=True
    )


@bot.tree.command(name="remove_store_item", description="Staff: remove store item.")
@check_channel_restriction()
@app_commands.describe(item_key="Item key")
async def remove_store_item_cmd(interaction: discord.Interaction, item_key: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    item_key = item_key.lower().strip()
    await db.execute("DELETE FROM custom_store_items WHERE item_key = ?", (item_key,))
    await interaction.response.send_message(f"✅ Removed `{item_key}`.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STAFF / UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="restrict_command", description="Staff: restrict a command to a channel.")
@check_channel_restriction()
@app_commands.describe(
    command_name="Slash command name",
    restriction_type="disabled or only_here",
    channel="Target channel",
)
@app_commands.choices(
    restriction_type=[
        app_commands.Choice(name="Disabled in this channel", value="disabled"),
        app_commands.Choice(name="Only work in this channel", value="only_here"),
    ]
)
async def restrict_command_cmd(
    interaction: discord.Interaction,
    command_name: str,
    restriction_type: str,
    channel: discord.TextChannel = None,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    target = channel or interaction.channel
    cmd = command_name.lower().strip()
    await db.execute(
        """INSERT OR REPLACE INTO channel_command_restrictions
           (command_name, channel_id, restriction_type) VALUES (?, ?, ?)""",
        (cmd, target.id, restriction_type),
    )
    await refresh_restriction_cache()
    msg = (
        f"🚫 Disabled `/{cmd}` in {target.mention}."
        if restriction_type == "disabled"
        else f"🔒 `/{cmd}` only works in {target.mention}."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="unrestrict_command", description="Staff: remove channel restrictions.")
@check_channel_restriction()
@app_commands.describe(command_name="Command name")
async def unrestrict_command_cmd(interaction: discord.Interaction, command_name: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    cmd = command_name.lower().strip()
    await db.execute(
        "DELETE FROM channel_command_restrictions WHERE command_name = ?", (cmd,)
    )
    await refresh_restriction_cache()
    await interaction.response.send_message(f"✅ Cleared restrictions for `/{cmd}`.", ephemeral=True)


@bot.tree.command(name="sticky", description="Staff: set sticky message.")
@check_channel_restriction()
@app_commands.describe(title="Title", description="Body", interval="Repost every N messages")
async def sticky_cmd(
    interaction: discord.Interaction, title: str, description: str, interval: int = 5
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if interval < 1:
        await interaction.response.send_message("❌ Interval ≥ 1.", ephemeral=True)
        return
    row = await db.fetchone(
        "SELECT last_msg_id FROM sticky_messages WHERE channel_id = ?",
        (interaction.channel.id,),
    )
    if row and row["last_msg_id"]:
        try:
            old = await interaction.channel.fetch_message(row["last_msg_id"])
            await old.delete()
        except Exception:
            pass
    embed = discord.Embed(title=f"📌 {title}", description=description, color=DARK_FRUITS_PURPLE)
    embed.set_thumbnail(url=THUMBNAIL_URL)
    msg = await interaction.channel.send(embed=embed)
    await db.execute(
        """INSERT OR REPLACE INTO sticky_messages
           (channel_id, title, description, interval, message_count, last_msg_id)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (interaction.channel.id, title, description, interval, msg.id),
    )
    await refresh_sticky_cache()
    await interaction.response.send_message(
        f"✅ Sticky every **{interval}** messages.", ephemeral=True
    )


@bot.tree.command(name="remove_sticky", description="Staff: remove sticky.")
@check_channel_restriction()
async def remove_sticky_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    row = await db.fetchone(
        "SELECT last_msg_id FROM sticky_messages WHERE channel_id = ?",
        (interaction.channel.id,),
    )
    if row and row["last_msg_id"]:
        try:
            old = await interaction.channel.fetch_message(row["last_msg_id"])
            await old.delete()
        except Exception:
            pass
    await db.execute(
        "DELETE FROM sticky_messages WHERE channel_id = ?", (interaction.channel.id,)
    )
    await refresh_sticky_cache()
    await interaction.response.send_message("✅ Sticky removed.", ephemeral=True)


@bot.tree.command(name="remove_dc", description="Staff: remove DC from a user.")
@check_channel_restriction()
@app_commands.describe(member="Target", amount="Amount")
async def remove_dc_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount > 0.", ephemeral=True)
        return
    new_bal = await update_user_balance(member.id, -amount)
    await interaction.response.send_message(
        f"✅ Removed `{amount:,}` from {member.mention}. Balance: `{new_bal:,}`.",
        ephemeral=True,
    )


@bot.tree.command(name="add_tournament", description="Staff: add tournament wins.")
@check_channel_restriction()
@app_commands.describe(member="Target", wins="Wins to add")
async def add_tournament_cmd(
    interaction: discord.Interaction, member: discord.Member, wins: int = 1
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if wins <= 0:
        await interaction.response.send_message("❌ Wins > 0.", ephemeral=True)
        return
    total = await add_tournament_win(member.id, wins)
    try:
        await member.send(f"🏆 Awarded **{wins}** tournament win(s) in **{SERVER_NAME}**!")
    except Exception:
        pass
    await interaction.response.send_message(
        f"✅ {member.mention} now has `{total}` wins.", ephemeral=True
    )


@bot.tree.command(name="grant_temp_role", description="Staff: temporary role (stacks).")
@check_channel_restriction()
@app_commands.describe(member="Target", role="Role", duration="e.g. 2d, 12h, 30m")
async def grant_temp_role_cmd(
    interaction: discord.Interaction, member: discord.Member, role: discord.Role, duration: str
):
    if not is_staff(interaction.user) and not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    secs = parse_duration(duration)
    if secs <= 0:
        await interaction.response.send_message("❌ Invalid duration (s/m/h/d).", ephemeral=True)
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    row = await db.fetchone(
        "SELECT expires_at FROM temp_roles WHERE user_id = ? AND role_id = ?",
        (member.id, role.id),
    )
    if row:
        try:
            current = datetime.datetime.fromisoformat(row["expires_at"])
            base = max(now, current)
        except Exception:
            base = now
        new_exp = base + datetime.timedelta(seconds=secs)
    else:
        new_exp = now + datetime.timedelta(seconds=secs)
    await db.execute(
        "INSERT OR REPLACE INTO temp_roles (user_id, role_id, expires_at) VALUES (?, ?, ?)",
        (member.id, role.id, new_exp.isoformat()),
    )
    try:
        await member.add_roles(role, reason=f"Temp by {interaction.user}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Role assign failed: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ {role.mention} → {member.mention} until <t:{int(new_exp.timestamp())}:R>.",
        ephemeral=True,
    )


@bot.tree.command(name="set_role_daily", description="Staff: set role daily DC.")
@check_channel_restriction()
@app_commands.describe(role="Role", amount="Daily DC")
async def set_role_daily_cmd(interaction: discord.Interaction, role: discord.Role, amount: int):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if amount < 0:
        await interaction.response.send_message("❌ Amount ≥ 0.", ephemeral=True)
        return
    await db.execute(
        "INSERT OR REPLACE INTO role_daily_rewards (role_id, daily_amount) VALUES (?, ?)",
        (role.id, amount),
    )
    await interaction.response.send_message(
        f"✅ {role.mention} earns **{amount:,} DC**/day.", ephemeral=True
    )


@bot.tree.command(name="approve_quest", description="Staff: reward quest completion.")
@check_channel_restriction()
@app_commands.describe(member="User", amount="DC reward", quest_name="Quest name")
async def approve_quest_cmd(
    interaction: discord.Interaction, member: discord.Member, amount: int, quest_name: str
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount > 0.", ephemeral=True)
        return
    await update_user_balance(member.id, amount)
    data = await get_user_data(member.id)
    new_q = data["quests_completed"] + 1
    await db.execute(
        """INSERT INTO user_wallets (user_id, balance, tournaments_won, quests_completed)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET quests_completed = excluded.quests_completed""",
        (member.id, data["balance"], data["tournaments_won"], new_q),
    )
    try:
        embed = discord.Embed(
            title="🌟 Quest Approved",
            description=f"**{quest_name}** verified.\n💰 +{amount:,} DC",
            color=DARK_FRUITS_PURPLE,
        )
        await member.send(embed=embed)
    except Exception:
        pass
    await interaction.response.send_message(
        f"✅ Awarded `{amount:,}` to {member.mention} for **{quest_name}**.",
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="giveaway_blacklist", description="Staff: blacklist from giveaways.")
@check_channel_restriction()
@app_commands.describe(action="add or remove", member="User")
@app_commands.choices(
    action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
    ]
)
async def giveaway_blacklist_cmd(
    interaction: discord.Interaction, action: str, member: discord.Member
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if action == "add":
        BLACKLISTED_USERS.add(member.id)
        await db.execute(
            "INSERT OR IGNORE INTO blacklisted_users (user_id) VALUES (?)", (member.id,)
        )
        await interaction.response.send_message(
            f"🚫 Blacklisted {member.mention}.", ephemeral=True
        )
    else:
        BLACKLISTED_USERS.discard(member.id)
        await db.execute("DELETE FROM blacklisted_users WHERE user_id = ?", (member.id,))
        await interaction.response.send_message(
            f"✅ Unblacklisted {member.mention}.", ephemeral=True
        )


async def _launch_giveaway(
    interaction: discord.Interaction,
    prize: str,
    prize2: str | None,
    winners_count: int,
    duration: str,
    channel: discord.TextChannel | None,
    extra_entries: str | None,
    required_role: discord.Role | None,
    custom_end: str,
    youtube_url: str | None,
    content_prefix: str,
) -> None:
    seconds = parse_duration(duration)
    if seconds <= 0:
        await interaction.response.send_message("❌ Invalid duration (s/m/h/d).", ephemeral=True)
        return
    target = channel or interaction.channel
    await interaction.response.send_message(
        f"🔮 Launching giveaway in {target.mention}...", ephemeral=True
    )

    host = interaction.user
    end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    ts = f"<t:{int(end_time.timestamp())}:R>"
    role_multipliers = parse_extra_entries(extra_entries)
    multi_desc = "".join(f"• <@&{rid}> → **{m}x**\n" for rid, m in role_multipliers.items())

    if winners_count == 1 or not prize2:
        title = "🎁  DARK FRUITS GIVEAWAY"
        desc = f"### ✨ Prize: **{prize}**"
        winner_text = "` 1 Lucky Winner `"
    else:
        title = "🥇🥈 DARK FRUITS SPLIT GIVEAWAY"
        desc = f"### ✨ Dual Treasures:\n• 🥇 **{prize}**\n• 🥈 **{prize2}**"
        winner_text = "` 2 Distinct Winners `"

    embed = discord.Embed(title=title, description=desc, color=DARK_FRUITS_PURPLE)
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(name="🏆 Winners", value=winner_text, inline=True)
    embed.add_field(name="⏳ Time Remaining", value=ts, inline=True)
    if required_role:
        embed.add_field(name="🔒 Required Role", value=required_role.mention, inline=False)
    if multi_desc:
        embed.add_field(name="⚡ Multipliers", value=multi_desc, inline=False)
    embed.add_field(
        name="📊 Live Entry Stats",
        value="🎟️ Total Tickets: `0`\n👥 Participants: `0 Users`",
        inline=False,
    )
    if youtube_url:
        embed.add_field(
            name="🔗 Task",
            value=f"[YouTube]({youtube_url})",
            inline=False,
        )
    embed.set_footer(
        text=f"Hosted by {host.display_name}",
        icon_url=getattr(host.display_avatar, "url", THUMBNAIL_URL),
    )

    giveaway_msg = await target.send(content=content_prefix, embed=embed)
    view = DynamicGiveawayView(
        message_id=giveaway_msg.id,
        embed=embed,
        role_multipliers=role_multipliers,
        required_role=required_role,
    )
    view.message_ref = giveaway_msg
    await giveaway_msg.edit(view=view)
    bot.add_view(view, message_id=giveaway_msg.id)

    data = {
        "view": view,
        "prize": prize,
        "prize2": prize2,
        "winners_count": winners_count,
        "host_user": host,
        "host_mention": host.mention,
        "custom_end": custom_end,
        "role_multipliers": role_multipliers,
        "end_time": end_time,
        "paused": False,
        "remaining_seconds": seconds,
        "channel": target,
    }
    async with giveaways.lock:
        giveaways.active[giveaway_msg.id] = data
    await giveaways.save(giveaway_msg.id, data)
    bot.loop.create_task(run_giveaway_timer(giveaway_msg.id))


@bot.tree.command(name="giveaway_single", description="Launch a single-prize giveaway.")
@check_channel_restriction()
@app_commands.describe(
    prize="Prize",
    duration="e.g. 30m, 2h, 1d",
    channel="Channel",
    extra_entries="e.g. @VIP 5, @Boosters 3",
    required_role="Optional role gate",
    custom_end_message="Message to winner",
    youtube_url="Optional YouTube link",
)
async def giveaway_single_cmd(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    channel: discord.TextChannel = None,
    extra_entries: str = None,
    required_role: discord.Role = None,
    custom_end_message: str = "Open a support ticket to claim your prize!",
    youtube_url: str = None,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await _launch_giveaway(
        interaction,
        prize,
        None,
        1,
        duration,
        channel,
        extra_entries,
        required_role,
        custom_end_message,
        youtube_url,
        "🔔 @everyone\n🎉 **NEW GIVEAWAY LIVE** 🎉",
    )


@bot.tree.command(name="giveaway_multi", description="Launch a dual-prize giveaway.")
@check_channel_restriction()
@app_commands.describe(
    prize1="First prize",
    prize2="Second prize",
    duration="Duration",
    channel="Channel",
    extra_entries="Role multipliers",
    required_role="Optional role",
    custom_end_message="End message",
    youtube_url="Optional link",
)
async def giveaway_multi_cmd(
    interaction: discord.Interaction,
    prize1: str,
    prize2: str,
    duration: str,
    channel: discord.TextChannel = None,
    extra_entries: str = None,
    required_role: discord.Role = None,
    custom_end_message: str = "Open a support ticket to claim your rewards!",
    youtube_url: str = None,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await _launch_giveaway(
        interaction,
        prize1,
        prize2,
        2,
        duration,
        channel,
        extra_entries,
        required_role,
        custom_end_message,
        youtube_url,
        "🔔 @everyone\n🎉 **SPLIT GIVEAWAY LIVE** 🎉",
    )


@bot.tree.command(name="giveaway_schedule", description="Schedule a giveaway after a delay.")
@check_channel_restriction()
@app_commands.describe(
    prize="Prize",
    start_in="Delay before start",
    duration="Giveaway length",
    channel="Channel",
    extra_entries="Multipliers",
    required_role="Role gate",
)
async def giveaway_schedule_cmd(
    interaction: discord.Interaction,
    prize: str,
    start_in: str,
    duration: str,
    channel: discord.TextChannel = None,
    extra_entries: str = None,
    required_role: discord.Role = None,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    delay = parse_duration(start_in)
    if delay <= 0:
        await interaction.response.send_message("❌ Invalid start delay.", ephemeral=True)
        return
    target = channel or interaction.channel
    await interaction.response.send_message(
        f"⏱️ Scheduled **{prize}** in {target.mention} after **{start_in}**.",
        ephemeral=True,
    )

    async def delayed():
        await asyncio.sleep(delay)
        # Re-create a fake interaction-like launch is awkward; call internal launch
        # by constructing minimal context via a synthetic path:
        class _Fake:
            user = interaction.user
            channel = target
            async def response(self):
                pass
        # Use a simplified launch without interaction.response
        seconds = parse_duration(duration)
        if seconds <= 0:
            return
        host = interaction.user
        end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
        ts = f"<t:{int(end_time.timestamp())}:R>"
        role_multipliers = parse_extra_entries(extra_entries)
        multi_desc = "".join(f"• <@&{rid}> → **{m}x**\n" for rid, m in role_multipliers.items())
        embed = discord.Embed(
            title="🎁  DARK FRUITS GIVEAWAY",
            description=f"### ✨ Prize: **{prize}**",
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.add_field(name="🏆 Winners", value="` 1 Lucky Winner `", inline=True)
        embed.add_field(name="⏳ Time Remaining", value=ts, inline=True)
        if required_role:
            embed.add_field(name="🔒 Required Role", value=required_role.mention, inline=False)
        if multi_desc:
            embed.add_field(name="⚡ Multipliers", value=multi_desc, inline=False)
        embed.add_field(
            name="📊 Live Entry Stats",
            value="🎟️ Total Tickets: `0`\n👥 Participants: `0 Users`",
            inline=False,
        )
        embed.set_footer(text=f"Hosted by {host.display_name}")
        giveaway_msg = await target.send(
            content="🔔 @everyone\n🎉 **SCHEDULED GIVEAWAY LIVE** 🎉", embed=embed
        )
        view = DynamicGiveawayView(
            message_id=giveaway_msg.id,
            embed=embed,
            role_multipliers=role_multipliers,
            required_role=required_role,
        )
        view.message_ref = giveaway_msg
        await giveaway_msg.edit(view=view)
        bot.add_view(view, message_id=giveaway_msg.id)
        data = {
            "view": view,
            "prize": prize,
            "prize2": None,
            "winners_count": 1,
            "host_user": host,
            "host_mention": host.mention,
            "custom_end": "Open a support ticket to claim your prize!",
            "role_multipliers": role_multipliers,
            "end_time": end_time,
            "paused": False,
            "remaining_seconds": seconds,
            "channel": target,
        }
        async with giveaways.lock:
            giveaways.active[giveaway_msg.id] = data
        await giveaways.save(giveaway_msg.id, data)
        bot.loop.create_task(run_giveaway_timer(giveaway_msg.id))

    bot.loop.create_task(delayed())


@bot.tree.command(name="giveaway_list", description="List active giveaways.")
@check_channel_restriction()
async def giveaway_list_cmd(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if not giveaways.active:
        await interaction.response.send_message("📂 No active giveaways.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Active Giveaways", color=DARK_FRUITS_PURPLE)
    for msg_id, data in giveaways.active.items():
        status = "⏸️ Paused" if data["paused"] else "🟢 Active"
        prize = (
            f"{data['prize']} & {data['prize2']}"
            if data.get("prize2")
            else data["prize"]
        )
        embed.add_field(
            name=prize,
            value=(
                f"**ID:** `{msg_id}`\n"
                f"**Channel:** {data['channel'].mention}\n"
                f"**Status:** {status}\n"
                f"**Entrants:** `{len(data['view'].entrants)}`"
            ),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="giveaway_pause", description="Pause a giveaway timer.")
@check_channel_restriction()
@app_commands.describe(message_id="Giveaway message ID")
async def giveaway_pause_cmd(interaction: discord.Interaction, message_id: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        return
    async with giveaways.lock:
        if mid not in giveaways.active:
            await interaction.response.send_message("❌ Not found.", ephemeral=True)
            return
        giveaways.active[mid]["paused"] = True
    await db.execute(
        "UPDATE active_giveaways SET paused = 1 WHERE message_id = ?", (mid,)
    )
    await interaction.response.send_message(f"⏸️ Paused `{mid}`.", ephemeral=True)


@bot.tree.command(name="giveaway_resume", description="Resume a paused giveaway.")
@check_channel_restriction()
@app_commands.describe(message_id="Giveaway message ID")
async def giveaway_resume_cmd(interaction: discord.Interaction, message_id: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        return
    async with giveaways.lock:
        if mid not in giveaways.active:
            await interaction.response.send_message("❌ Not found.", ephemeral=True)
            return
        giveaways.active[mid]["paused"] = False
    await db.execute(
        "UPDATE active_giveaways SET paused = 0 WHERE message_id = ?", (mid,)
    )
    await interaction.response.send_message(f"▶️ Resumed `{mid}`.", ephemeral=True)


@bot.tree.command(name="end_giveaway", description="End a giveaway early and pick winners.")
@check_channel_restriction()
@app_commands.describe(message_id="Giveaway message ID")
async def end_giveaway_cmd(interaction: discord.Interaction, message_id: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.followup.send("❌ Invalid ID.", ephemeral=True)
        return
    async with giveaways.lock:
        data = giveaways.active.pop(mid, None)
    if not data:
        await interaction.followup.send("❌ Not found in active giveaways.", ephemeral=True)
        return
    await conclude_giveaway(mid, data, early=True)
    await interaction.followup.send("✅ Ended early.", ephemeral=True)


@bot.tree.command(name="giveaway_reroll", description="Reroll winners from a concluded giveaway.")
@check_channel_restriction()
@app_commands.describe(message_id="Message ID", winners_count="How many winners")
async def giveaway_reroll_cmd(
    interaction: discord.Interaction, message_id: str, winners_count: int = 1
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.followup.send("❌ Invalid ID.", ephemeral=True)
        return
    entrants = await giveaways.get_ended_entrants(mid)
    if not entrants:
        await interaction.followup.send(
            "❌ No archived entrants for this ID (maybe pre-rewrite giveaway).",
            ephemeral=True,
        )
        return
    new_winners = draw_winners(entrants, winners_count)
    for uid in new_winners:
        try:
            u = await bot.fetch_user(uid)
            await u.send(
                f"🎉 You were chosen in a **reroll** in **{SERVER_NAME}**!"
            )
        except Exception:
            pass
    if len(new_winners) == 1:
        await interaction.channel.send(
            f"🔄 **REROLL** 🔄\n🎊 Congrats <@{new_winners[0]}>!"
        )
    else:
        mentions = ", ".join(f"<@{u}>" for u in new_winners)
        await interaction.channel.send(f"🔄 **REROLL** 🔄\n🎊 Congrats {mentions}!")
    await interaction.followup.send("✅ Reroll done.", ephemeral=True)


@bot.tree.command(name="edit_giveaway", description="Edit an active giveaway.")
@check_channel_restriction()
@app_commands.describe(
    message_id="Message ID",
    new_prize="New prize",
    winners_count="Winners",
    new_duration="New remaining time (optional)",
    extra_entries="New multipliers (optional)",
    custom_end_message="New end message (optional)",
)
async def edit_giveaway_cmd(
    interaction: discord.Interaction,
    message_id: str,
    new_prize: str,
    winners_count: int = 1,
    new_duration: str = None,
    extra_entries: str = None,
    custom_end_message: str = None,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.followup.send("❌ Invalid ID.", ephemeral=True)
        return
    async with giveaways.lock:
        data = giveaways.active.get(mid)
        if not data:
            await interaction.followup.send("❌ Not active.", ephemeral=True)
            return
        data["prize"] = new_prize
        data["winners_count"] = winners_count
        if custom_end_message:
            data["custom_end"] = custom_end_message
        if extra_entries is not None:
            data["role_multipliers"] = parse_extra_entries(extra_entries)
            data["view"].role_multipliers = data["role_multipliers"]
        if new_duration:
            secs = parse_duration(new_duration)
            if secs > 0:
                data["remaining_seconds"] = secs
                data["end_time"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                    seconds=secs
                )
        view = data["view"]
        total_t = sum(view.entrants.values())
        total_u = len(view.entrants)
        multi_desc = "".join(
            f"• <@&{rid}> → **{m}x**\n" for rid, m in data["role_multipliers"].items()
        )
        ts = f"<t:{int(data['end_time'].timestamp())}:R>"
        embed = discord.Embed(
            title="🎁  DARK FRUITS GIVEAWAY",
            description=f"### ✨ Prize: **{new_prize}**",
            color=DARK_FRUITS_PURPLE,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.add_field(
            name="🏆 Winners",
            value=f"` {winners_count} Winner{'s' if winners_count > 1 else ''} `",
            inline=True,
        )
        embed.add_field(name="⏳ Time Remaining", value=ts, inline=True)
        if multi_desc:
            embed.add_field(name="⚡ Multipliers", value=multi_desc, inline=False)
        embed.add_field(
            name="📊 Live Entry Stats",
            value=f"🎟️ Total Tickets: `{total_t}`\n👥 Participants: `{total_u} Users`",
            inline=False,
        )
        host = data["host_user"]
        embed.set_footer(text=f"Hosted by {getattr(host, 'display_name', 'Staff')}")
        view.embed = embed
        await giveaways.save(mid, data)

    try:
        msg = await data["channel"].fetch_message(mid)
        await msg.edit(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Saved but edit failed: {e}", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Updated to **{new_prize}**.", ephemeral=True)



# ═══════════════════════════════════════════════════════════════════════════════
# DARK FRUITS MEGA-SUITE ADD-ON COMMANDS
# Added as an extension: original systems above are intentionally preserved.
# ═══════════════════════════════════════════════════════════════════════════════

MEGA_SUITE_VERSION = "2.0"
MEGA_SUITE_STARTED_AT = datetime.datetime.now(datetime.timezone.utc)


def premium_embed(title: str, description: str = "", *, color: discord.Color = DARK_FRUITS_PURPLE) -> discord.Embed:
    """Central premium Dark Fruits embed style for new commands and features."""
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now(datetime.timezone.utc))
    if THUMBNAIL_URL:
        embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_footer(text=f"{SERVER_NAME} • Pirate tools")
    return embed


# Internal alias keeps the extension readable while exposing the documented helper.
_suite_embed = premium_embed


def _suite_staff(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return is_staff(member) or member.guild_permissions.administrator


@bot.tree.command(name="help", description="Browse the Dark Fruits command directory.")
@app_commands.describe(category="Optional category to browse")
@app_commands.choices(category=[
    app_commands.Choice(name="All commands", value="all"),
    app_commands.Choice(name="Blox Fruits", value="blox"),
    app_commands.Choice(name="Economy", value="economy"),
    app_commands.Choice(name="Giveaways", value="giveaways"),
    app_commands.Choice(name="Staff and moderation", value="staff"),
    app_commands.Choice(name="Server utilities", value="utilities"),
    app_commands.Choice(name="Fun and games", value="fun"),
])
async def mega_help(interaction: discord.Interaction, category: str = "all"):
    groups = {
        "blox": ("🍎 Blox Fruits", "`/stock` · `/stock_force` · mention the bot for Blox Fruits questions and guides. Fruit values and live stock depend on source availability."),
        "economy": ("💰 Dark Coins", "`/balance` · `/profile` · `/inventory` · `/coinflip` · `/fish` · `/work` · `/pay` · `/leaderboard` · `/claim_daily_role` · `/store` · `/buy`"),
        "giveaways": ("🎁 Giveaways", "`/giveaway_single` · `/giveaway_multi` · `/giveaway_schedule` · `/giveaway_list` · `/giveaway_pause` · `/giveaway_resume` · `/end_giveaway` · `/giveaway_reroll` · `/edit_giveaway` · `/giveaway_blacklist`"),
        "staff": ("🛡️ Staff tools", "`/stock_force` · `/add_store_item` · `/remove_store_item` · `/restrict_command` · `/unrestrict_command` · `/sticky` · `/remove_sticky` · `/remove_dc` · `/add_tournament` · `/grant_temp_role` · `/set_role_daily` · `/approve_quest` · plus Discord permission-based moderation commands."),
        "utilities": ("🧰 Server utilities", "`/ping` · `/botinfo` · `/serverinfo` · `/userinfo` · `/avatar` · `/banner` · `/roleinfo` · `/channelinfo` · `/invite` · `/poll` · `/membercount` · `/emojis` · `/roles` · `/remindme`"),
        "fun": ("🎲 Fun and games", "`/dice` · `/choose` · `/eightball` · `/ship` · `/compliment` · `/roast` · `/reverse` · `/calculate`"),
    }
    embed = _suite_embed("🌑 DARK FRUITS • COMMAND CENTRE", "Your all-in-one pirate toolkit. Pick a category or use the Discord `/` menu to discover commands.")
    if category == "all":
        for key in ("blox", "economy", "giveaways", "staff", "utilities", "fun"):
            title, desc = groups[key]
            embed.add_field(name=title, value=desc, inline=False)
    else:
        title, desc = groups.get(category, groups["utilities"])
        embed.add_field(name=title, value=desc, inline=False)
    embed.add_field(name="💡 Tip", value="Slash commands appear in Discord's command picker. Commands requiring staff permissions will only work for authorised staff.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot's response latency.")
async def mega_ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = _suite_embed("🏓 Pong!", f"**Gateway latency:** `{latency} ms`\n**Status:** {'🟢 Smooth sailing' if latency < 180 else '🟡 A little choppy' if latency < 400 else '🔴 High latency'}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="botinfo", description="Show Dark Fruits bot status and runtime information.")
async def mega_botinfo(interaction: discord.Interaction):
    uptime = datetime.datetime.now(datetime.timezone.utc) - MEGA_SUITE_STARTED_AT
    total_members = sum(g.member_count or 0 for g in bot.guilds)
    embed = _suite_embed("🌑 Dark Fruits • System Status", "A single control centre for the Dark Fruits community.")
    embed.add_field(name="Version", value=f"`{MEGA_SUITE_VERSION}`", inline=True)
    embed.add_field(name="Latency", value=f"`{round(bot.latency * 1000)} ms`", inline=True)
    embed.add_field(name="Uptime", value=f"`{str(uptime).split('.')[0]}`", inline=True)
    embed.add_field(name="Servers", value=f"`{len(bot.guilds):,}`", inline=True)
    embed.add_field(name="Members (approx.)", value=f"`{total_members:,}`", inline=True)
    embed.add_field(name="Commands", value=f"`{len(bot.tree.get_commands())}` registered slash commands", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="View detailed information about this server.")
async def mega_serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
    embed = _suite_embed(f"🏰 {guild.name}", guild.description or "Server information and community statistics.")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>" if guild.owner_id else "Unknown", inline=True)
    embed.add_field(name="Members", value=f"`{guild.member_count:,}`", inline=True)
    embed.add_field(name="Boosts", value=f"`{guild.premium_subscription_count or 0}` (level {guild.premium_tier})", inline=True)
    embed.add_field(name="Channels", value=f"`{len(guild.channels)}` total\n`{len(guild.text_channels)}` text · `{len(guild.voice_channels)}` voice", inline=True)
    embed.add_field(name="Roles", value=f"`{len(guild.roles)}`", inline=True)
    embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:D>\n<t:{int(guild.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Verification", value=str(guild.verification_level).replace('_', ' ').title(), inline=True)
    embed.add_field(name="Preferred locale", value=str(guild.preferred_locale), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Inspect a member's server profile.")
@app_commands.describe(member="Member to inspect; defaults to you")
async def mega_userinfo(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    if not isinstance(member, discord.Member):
        return await interaction.response.send_message("I couldn't load that member.", ephemeral=True)
    embed = _suite_embed(f"👤 {member.display_name}", f"Account: {member.mention}")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Username", value=f"`{member.name}`", inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Bot account", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="Joined server", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
    embed.add_field(name=f"Roles ({len(roles)})", value=(" ".join(roles[:25]) or "No extra roles")[:1024], inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Show a member's avatar in full resolution.")
@app_commands.describe(member="Member; defaults to you")
async def mega_avatar(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    asset = member.display_avatar
    embed = _suite_embed(f"🖼️ {member.display_name}'s avatar", f"[Open full resolution]({asset.url})")
    embed.set_image(url=asset.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="banner", description="Show a member's profile banner, if available.")
@app_commands.describe(member="Member; defaults to you")
async def mega_banner(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    try:
        user = await bot.fetch_user(member.id)
    except discord.HTTPException:
        user = member
    if not getattr(user, "banner", None):
        return await interaction.response.send_message("That user doesn't have a visible Discord banner.", ephemeral=True)
    embed = _suite_embed(f"🎨 {member.display_name}'s banner", f"[Open full resolution]({user.banner.url})")
    embed.set_image(url=user.banner.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roleinfo", description="Show details about a server role.")
@app_commands.describe(role="Role to inspect")
async def mega_roleinfo(interaction: discord.Interaction, role: discord.Role):
    embed = _suite_embed(f"🎭 Role: {role.name}", f"Mention: {role.mention}\nID: `{role.id}`")
    embed.add_field(name="Members", value=f"`{len(role.members)}`", inline=True)
    embed.add_field(name="Position", value=f"`{role.position}`", inline=True)
    embed.add_field(name="Colour", value=f"`#{role.color.value:06X}`", inline=True)
    embed.add_field(name="Hoisted", value=str(role.hoist), inline=True)
    embed.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
    embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="channelinfo", description="Show details about a text channel.")
@app_commands.describe(channel="Channel to inspect; defaults to this channel")
async def mega_channelinfo(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    embed = _suite_embed(f"📚 #{channel.name}", f"ID: `{channel.id}`\nCategory: {channel.category.mention if channel.category else 'None'}")
    embed.add_field(name="Created", value=f"<t:{int(channel.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Slowmode", value=f"`{channel.slowmode_delay}s`", inline=True)
    embed.add_field(name="NSFW", value=str(channel.nsfw), inline=True)
    embed.add_field(name="Topic", value=(channel.topic or "No topic set")[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="membercount", description="Show the current server member count.")
async def mega_membercount(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    humans = sum(not m.bot for m in guild.members)
    bots = sum(m.bot for m in guild.members)
    embed = _suite_embed("👥 Crew Manifest", f"**Total:** `{guild.member_count or len(guild.members):,}`\n**Humans cached:** `{humans:,}`\n**Bots cached:** `{bots:,}`")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="emojis", description="List the server's custom emojis.")
async def mega_emojis(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    if not guild.emojis:
        return await interaction.response.send_message("This server has no custom emojis.", ephemeral=True)
    chunks = []
    current = []
    for emoji in guild.emojis:
        item = f"{str(emoji)} `:{emoji.name}:`"
        if sum(map(len, current)) + len(item) > 900:
            chunks.append("\n".join(current)); current = []
        current.append(item)
    if current:
        chunks.append("\n".join(current))
    embed = _suite_embed(f"😀 Server emojis ({len(guild.emojis)})")
    for i, chunk in enumerate(chunks[:5], 1):
        embed.add_field(name=f"Page {i}", value=chunk, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="roles", description="List server roles from highest to lowest.")
async def mega_roles(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    lines = [f"{role.mention} — `{len(role.members)} members`" for role in reversed(guild.roles) if role != guild.default_role]
    embed = _suite_embed(f"🎭 Server roles ({len(lines)})", "\n".join(lines[:35]) or "No roles found.")
    if len(lines) > 35:
        embed.set_footer(text=f"Showing 35 of {len(lines)} roles")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="invite", description="Create an invite link for this channel.")
@app_commands.describe(max_uses="Maximum uses (0 means unlimited)", temporary="Grant temporary membership")
@app_commands.checks.has_permissions(create_instant_invite=True)
async def mega_invite(interaction: discord.Interaction, max_uses: app_commands.Range[int, 0, 100] = 0, temporary: bool = False):
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
        return await interaction.response.send_message("This channel cannot create invites.", ephemeral=True)
    invite = await channel.create_invite(max_age=86400, max_uses=max_uses, temporary=temporary, reason=f"Invite created by {interaction.user}")
    await interaction.response.send_message(f"🔗 **Your invite:** {invite.url}\nExpires in 24 hours.", ephemeral=True)


@bot.tree.command(name="poll", description="Create a quick reaction poll.")
@app_commands.describe(question="Poll question", option_a="First option", option_b="Second option", option_c="Optional third option", option_d="Optional fourth option")
async def mega_poll(interaction: discord.Interaction, question: str, option_a: str, option_b: str, option_c: str | None = None, option_d: str | None = None):
    options = [option_a[:80], option_b[:80]] + [x[:80] for x in (option_c, option_d) if x]
    question = question[:250]
    emojis = ["🇦", "🇧", "🇨", "🇩"]
    embed = _suite_embed("📊 Crew Poll", f"**{question}**\n\n" + "\n".join(f"{emojis[i]}  {option}" for i, option in enumerate(options)))
    embed.add_field(name="Vote", value="React below to cast your vote. One reaction per person is recommended.", inline=False)
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for emoji in emojis[:len(options)]:
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass


@bot.tree.command(name="announce", description="Post a polished server announcement (staff only).")
@app_commands.describe(title="Announcement title", message="Announcement text", channel="Destination channel", ping_everyone="Mention @everyone (use sparingly)")
@app_commands.checks.has_permissions(manage_messages=True)
async def mega_announce(interaction: discord.Interaction, title: str, message: str, channel: discord.TextChannel | None = None, ping_everyone: bool = False):
    destination = channel or interaction.channel
    if not isinstance(destination, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    embed = _suite_embed(f"📢 {title[:256]}", message[:3500])
    embed.add_field(name="Posted by", value=interaction.user.mention, inline=False)
    await destination.send(content="@everyone" if ping_everyone else None, embed=embed, allowed_mentions=discord.AllowedMentions(everyone=ping_everyone, users=False, roles=False))
    await interaction.response.send_message(f"✅ Announcement posted in {destination.mention}.", ephemeral=True)


@bot.tree.command(name="custom_embed", description="Create a custom branded embed (staff only).")
@app_commands.describe(title="Embed title", description="Embed description", channel="Destination channel", footer="Optional footer text")
@app_commands.checks.has_permissions(manage_messages=True)
async def mega_custom_embed(interaction: discord.Interaction, title: str, description: str, channel: discord.TextChannel | None = None, footer: str | None = None):
    destination = channel or interaction.channel
    if not isinstance(destination, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    embed = _suite_embed(title[:256], description[:3500])
    if footer:
        embed.set_footer(text=footer[:200])
    await destination.send(embed=embed)
    await interaction.response.send_message(f"✅ Embed posted in {destination.mention}.", ephemeral=True)


@bot.tree.command(name="say", description="Make the bot post a message (staff only).")
@app_commands.describe(message="Message to post", channel="Destination channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def mega_say(interaction: discord.Interaction, message: str, channel: discord.TextChannel | None = None):
    destination = channel or interaction.channel
    if not isinstance(destination, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    await destination.send(message, allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message("✅ Message posted.", ephemeral=True)


@bot.tree.command(name="clear", description="Delete recent messages from a channel (staff only).")
@app_commands.describe(amount="Number of messages to delete (1–100)", channel="Channel to clear")
@app_commands.checks.has_permissions(manage_messages=True)
async def mega_clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = await target.purge(limit=amount, reason=f"Clear requested by {interaction.user}")
    await interaction.followup.send(f"🧹 Deleted **{len(deleted)}** messages from {target.mention}.", ephemeral=True)


@bot.tree.command(name="set_slowmode", description="Set channel slowmode (staff only).")
@app_commands.describe(seconds="Seconds between messages (0–21600)", channel="Target text channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def mega_set_slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600], channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    await target.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {interaction.user}")
    await interaction.response.send_message(f"🐢 Slowmode in {target.mention} set to **{seconds} seconds**.", ephemeral=True)


@bot.tree.command(name="lockdown", description="Lock a text channel for @everyone (staff only).")
@app_commands.describe(channel="Channel to lock", reason="Reason for the lockdown")
@app_commands.checks.has_permissions(manage_channels=True)
async def mega_lockdown(interaction: discord.Interaction, channel: discord.TextChannel | None = None, reason: str = "Staff lockdown"):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"🔒 {target.mention} is now locked. Reason: {reason}", ephemeral=True)


@bot.tree.command(name="unlockdown", description="Unlock a text channel for @everyone (staff only).")
@app_commands.describe(channel="Channel to unlock")
@app_commands.checks.has_permissions(manage_channels=True)
async def mega_unlockdown(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
    await interaction.response.send_message(f"🔓 {target.mention} has been unlocked.", ephemeral=True)


@bot.tree.command(name="set_nickname", description="Change a member's nickname (staff only).")
@app_commands.describe(member="Member", nickname="New nickname; leave blank to reset")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def mega_set_nickname(interaction: discord.Interaction, member: discord.Member, nickname: str | None = None):
    await member.edit(nick=nickname, reason=f"Nickname changed by {interaction.user}")
    await interaction.response.send_message(f"✅ Updated nickname for {member.mention}.", ephemeral=True)


@bot.tree.command(name="add_role", description="Give a role to a member (staff only).")
@app_commands.describe(member="Member", role="Role to add")
@app_commands.checks.has_permissions(manage_roles=True)
async def mega_add_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I can't manage a role that is equal to or above my highest role.", ephemeral=True)
    await member.add_roles(role, reason=f"Role added by {interaction.user}")
    await interaction.response.send_message(f"✅ Added {role.mention} to {member.mention}.", ephemeral=True)


@bot.tree.command(name="remove_role", description="Remove a role from a member (staff only).")
@app_commands.describe(member="Member", role="Role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
async def mega_remove_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I can't manage a role that is equal to or above my highest role.", ephemeral=True)
    await member.remove_roles(role, reason=f"Role removed by {interaction.user}")
    await interaction.response.send_message(f"✅ Removed {role.mention} from {member.mention}.", ephemeral=True)


@bot.tree.command(name="timeout_member", description="Timeout a member for a chosen number of minutes (staff only).")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def mega_timeout_member(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason supplied"):
    if member == interaction.user or member.guild_permissions.administrator:
        return await interaction.response.send_message("You can't timeout yourself or an administrator.", ephemeral=True)
    await member.timeout(datetime.timedelta(minutes=minutes), reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"⏳ Timed out {member.mention} for **{minutes} minutes**. Reason: {reason}", ephemeral=True)


@fun_group.command(name="calculate", description="Calculate a basic arithmetic expression safely.")
@app_commands.describe(expression="Example: (125 * 4) / 2")
async def mega_calculate(interaction: discord.Interaction, expression: str):
    import ast
    import operator as _op
    allowed = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv, ast.Mod: _op.mod, ast.Pow: _op.pow, ast.USub: _op.neg, ast.UAdd: _op.pos}
    def evaluate(node):
        if isinstance(node, ast.Expression): return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool): return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in allowed:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 8: raise ValueError("Exponent too large")
            result = allowed[type(node.op)](left, right)
            if abs(result) > 10**15: raise ValueError("Result too large")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed: return allowed[type(node.op)](evaluate(node.operand))
        raise ValueError("Only basic arithmetic is supported")
    try:
        result = evaluate(ast.parse(expression, mode="eval"))
        await interaction.response.send_message(embed=_suite_embed("🧮 Calculator", f"`{expression}` = **{result:,}**" if isinstance(result, int) else f"`{expression}` = **{result:.10g}**"), ephemeral=True)
    except Exception as exc:
        await interaction.response.send_message(f"❌ Couldn't calculate that: {exc}", ephemeral=True)


@fun_group.command(name="choose", description="Let fate choose between your options.")
@app_commands.describe(options="Separate options with commas")
async def mega_choose(interaction: discord.Interaction, options: str):
    choices = [x.strip() for x in options.split(",") if x.strip()]
    if len(choices) < 2:
        return await interaction.response.send_message("Give me at least two options separated by commas.", ephemeral=True)
    await interaction.response.send_message(embed=_suite_embed("🎯 Fate has chosen", f"**{random.choice(choices)[:1800]}**"))


@fun_group.command(name="dice", description="Roll one or more dice.")
@app_commands.describe(sides="Number of sides", count="Number of dice")
async def mega_dice(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6, count: app_commands.Range[int, 1, 10] = 1):
    rolls = [random.randint(1, sides) for _ in range(count)]
    await interaction.response.send_message(embed=_suite_embed("🎲 Dice roll", f"**Rolls:** {', '.join(map(str, rolls))}\n**Total:** `{sum(rolls)}`"))


@fun_group.command(name="eightball", description="Ask the mysterious pirate 8-ball a question.")
@app_commands.describe(question="Your question")
async def mega_eightball(interaction: discord.Interaction, question: str):
    answers = ["Aye, absolutely.", "The seas point to yes.", "Without a doubt, captain.", "Likely — set sail.", "Ask again when the tide changes.", "The fog makes this unclear.", "Probably not, captain.", "The seas say no.", "Not looking good, matey.", "The answer is hidden beneath the waves."]
    await interaction.response.send_message(embed=_suite_embed("🔮 The Pirate 8-Ball", f"**Question:** {question}\n\n**Answer:** {random.choice(answers)}"))


@fun_group.command(name="ship", description="Calculate a playful compatibility score for two members.")
@app_commands.describe(first="First member", second="Second member")
async def mega_ship(interaction: discord.Interaction, first: discord.Member, second: discord.Member):
    import hashlib
    key = f"{min(first.id, second.id)}:{max(first.id, second.id)}:{interaction.guild_id}".encode()
    score = int(hashlib.sha256(key).hexdigest()[:8], 16) % 101
    hearts = "💜" * (score // 10) + "🖤" * (10 - score // 10)
    await interaction.response.send_message(embed=_suite_embed("💘 Pirate Compatibility", f"{first.mention} + {second.mention}\n\n**{score}%**\n{hearts}\n\nJust for fun — the sea keeps its own secrets."))


@fun_group.command(name="compliment", description="Send a friendly compliment to a member.")
@app_commands.describe(member="Member to compliment; defaults to you")
async def mega_compliment(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    lines = ["You've got main-character energy.", "You make the crew better just by being here.", "Your effort doesn't go unnoticed.", "Certified legend of the seas.", "Your vibe is rarer than a Mythical fruit.", "You're a solid crewmate — never forget it."]
    await interaction.response.send_message(embed=_suite_embed("💜 A message from the crew", f"{member.mention}, {random.choice(lines)}"))


@fun_group.command(name="roast", description="Send a harmless pirate-style roast to a member.")
@app_commands.describe(member="Member to roast; defaults to you")
async def mega_roast(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    lines = ["You'd lose a 1v1 to a training dummy.", "Your aim has more lag than a potato on dial-up.", "Even Rocket fruit is asking for a trade up.", "Your bounty is wanted by absolutely nobody.", "The NPCs are starting to feel bad for you.", "You bring 'forgot to equip aura' energy."]
    await interaction.response.send_message(embed=_suite_embed("🏴‍☠️ Friendly fire", f"{member.mention}, {random.choice(lines)}"))


@fun_group.command(name="reverse", description="Reverse a line of text.")
@app_commands.describe(text="Text to reverse")
async def mega_reverse(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text[:1900][::-1], allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="remindme", description="Set a reminder that is saved in the bot database.")
@app_commands.describe(minutes="Minutes from now (1–10080)", reminder="What should I remind you about?")
async def mega_remindme(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 10080], reminder: str):
    await db.execute("CREATE TABLE IF NOT EXISTS mega_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
    due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    await db.execute("INSERT INTO mega_reminders (user_id, channel_id, reminder, due_at, done) VALUES (?, ?, ?, ?, 0)", (interaction.user.id, interaction.channel_id, reminder, due.isoformat()))
    await interaction.response.send_message(f"⏰ I'll remind you in **{minutes} minutes** (<t:{int(due.timestamp())}:R>): **{reminder[:500]}**", ephemeral=True)


@tasks.loop(minutes=1)
async def mega_reminder_dispatcher():
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS mega_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = await db.fetchall("SELECT * FROM mega_reminders WHERE done = 0 AND due_at <= ? ORDER BY due_at LIMIT 50", (now,))
        for row in rows:
            # Mark first so a transient send error does not duplicate a reminder every minute.
            await db.execute("UPDATE mega_reminders SET done = 1 WHERE id = ?", (row["id"],))
            channel = bot.get_channel(row["channel_id"])
            message = f"⏰ <@{row['user_id']}> Reminder: **{row['reminder']}**"
            try:
                if channel and hasattr(channel, "send"):
                    await channel.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                else:
                    user = await bot.fetch_user(row["user_id"])
                    await user.send(message)
            except discord.HTTPException as exc:
                log.warning("Reminder %s could not be delivered: %s", row["id"], exc)
    except Exception:
        log.exception("Reminder dispatcher failed")


@mega_reminder_dispatcher.before_loop
async def before_mega_reminder_dispatcher():
    await bot.wait_until_ready()


# Start the add-on reminder service without replacing the original on_ready handler.
@bot.listen("on_ready")
async def start_mega_reminder_dispatcher():
    if not mega_reminder_dispatcher.is_running():
        mega_reminder_dispatcher.start()


@bot.tree.command(name="my_reminders", description="List your pending reminders.")
async def mega_my_reminders(interaction: discord.Interaction):
    await db.execute("CREATE TABLE IF NOT EXISTS mega_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
    rows = await db.fetchall("SELECT id, reminder, due_at FROM mega_reminders WHERE user_id = ? AND done = 0 ORDER BY due_at LIMIT 15", (interaction.user.id,))
    if not rows:
        return await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
    lines = [f"`#{r['id']}` • <t:{int(datetime.datetime.fromisoformat(r['due_at']).timestamp())}:R> — {r['reminder']}" for r in rows]
    await interaction.response.send_message(embed=_suite_embed("⏰ Your reminders", "\n".join(lines)), ephemeral=True)


@bot.tree.command(name="delete_reminder", description="Delete one of your pending reminders.")
@app_commands.describe(reminder_id="ID shown by /my_reminders")
async def mega_delete_reminder(interaction: discord.Interaction, reminder_id: app_commands.Range[int, 1, 2147483647]):
    await db.execute("CREATE TABLE IF NOT EXISTS mega_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
    row = await db.fetchone("SELECT id FROM mega_reminders WHERE id = ? AND user_id = ? AND done = 0", (reminder_id, interaction.user.id))
    if not row:
        return await interaction.response.send_message("I couldn't find a pending reminder with that ID belonging to you.", ephemeral=True)
    await db.execute("UPDATE mega_reminders SET done = 1 WHERE id = ?", (reminder_id,))
    await interaction.response.send_message("🗑️ Reminder deleted.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRA AUDIT EVENTS — listeners are used so the original event handlers stay intact.
# ═══════════════════════════════════════════════════════════════════════════════

async def _audit_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    # Prefer a per-guild setting, then the legacy environment variable.
    try:
        row = await db.fetchone("SELECT mod_log_channel_id FROM df_guild_settings WHERE guild_id = ?", (guild.id,))
        channel_id = int(row["mod_log_channel_id"] or 0) if row else 0
    except Exception:
        channel_id = 0
    if not channel_id:
        try:
            channel_id = int(os.environ.get("MOD_LOG_CHANNEL_ID", "0").strip() or 0)
        except ValueError:
            channel_id = 0
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


@bot.listen("on_message_delete")
async def mega_log_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    channel = await _audit_log_channel(message.guild)
    if not channel or channel.id == message.channel.id:
        return
    embed = _suite_embed("🗑️ Message deleted", f"**Author:** {message.author.mention} (`{message.author.id}`)\n**Channel:** {message.channel.mention}\n**Message ID:** `{message.id}`")
    if message.content:
        embed.add_field(name="Content", value=message.content[:1000], inline=False)
    await channel.send(embed=embed)


@bot.listen("on_message_edit")
async def mega_log_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    channel = await _audit_log_channel(before.guild)
    if not channel or channel.id == before.channel.id:
        return
    embed = _suite_embed("✏️ Message edited", f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}\n[Jump to message]({after.jump_url})")
    embed.add_field(name="Before", value=(before.content or "[No text]")[:900], inline=False)
    embed.add_field(name="After", value=(after.content or "[No text]")[:900], inline=False)
    await channel.send(embed=embed)


@bot.listen("on_member_remove")
async def mega_log_member_leave(member: discord.Member):
    channel = await _audit_log_channel(member.guild)
    if not channel:
        return
    embed = _suite_embed("👋 Member left", f"**Member:** {member} (`{member.id}`)\n**Joined:** <t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else f"**Member:** {member} (`{member.id}`)", color=discord.Color.orange())
    await channel.send(embed=embed)


@bot.listen("on_member_update")
async def mega_log_member_update(before: discord.Member, after: discord.Member):
    channel = await _audit_log_channel(after.guild)
    if not channel:
        return
    changes = []
    if before.nick != after.nick:
        changes.append(f"**Nickname:** `{before.nick or before.name}` → `{after.nick or after.name}`")
    if before.roles != after.roles:
        before_ids = {r.id for r in before.roles}; after_ids = {r.id for r in after.roles}
        added = [r.mention for r in after.roles if r.id not in before_ids]
        removed = [r.mention for r in before.roles if r.id not in after_ids]
        if added: changes.append("**Roles added:** " + ", ".join(added))
        if removed: changes.append("**Roles removed:** " + ", ".join(removed))
    if not changes:
        return
    await channel.send(embed=_suite_embed("🪪 Member updated", f"**Member:** {after.mention}\n" + "\n".join(changes)))


@bot.listen("on_voice_state_update")
async def mega_log_voice_state(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel == after.channel:
        return
    channel = await _audit_log_channel(member.guild)
    if not channel:
        return
    if before.channel is None and after.channel:
        desc = f"{member.mention} joined {after.channel.mention}."
    elif before.channel and after.channel is None:
        desc = f"{member.mention} left **{before.channel.name}**."
    else:
        desc = f"{member.mention} moved from **{before.channel.name}** to {after.channel.mention}."
    await channel.send(embed=_suite_embed("🔊 Voice activity", desc))


@bot.tree.error
async def mega_global_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "🛡️ You don't have the Discord permissions required for that command."
    elif isinstance(error, app_commands.errors.BotMissingPermissions):
        message = "⚠️ I need additional permissions to do that. Ask an administrator to check my role and channel permissions."
    elif isinstance(error, app_commands.errors.CommandOnCooldown):
        message = f"⏳ Try again in {error.retry_after:.1f} seconds."
    elif isinstance(error, app_commands.errors.TransformerError):
        message = "❌ One of those options couldn't be understood. Check the command inputs and try again."
    else:
        log.error("Unhandled slash command error: %r", error)
        message = "❌ Something went wrong while running that command. The error has been logged."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass



# ═══════════════════════════════════════════════════════════════════════════════
# COMMUNITY SYSTEMS FROM THE DARK FRUITS FEATURE SPEC
# Leveling, AFK, tickets, suggestions, vouches, warnings, birthdays, tags,
# starboard, counting and configurable moderation helpers.
# ═══════════════════════════════════════════════════════════════════════════════

_community_schema_ready = False
_community_schema_lock = asyncio.Lock()


async def _ensure_community_schema() -> None:
    global _community_schema_ready
    if _community_schema_ready:
        return
    async with _community_schema_lock:
        if _community_schema_ready:
            return
        statements = [
        "CREATE TABLE IF NOT EXISTS member_xp (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, last_xp TEXT, PRIMARY KEY(guild_id,user_id))",
        "CREATE TABLE IF NOT EXISTS level_rewards (guild_id INTEGER NOT NULL, level INTEGER NOT NULL, role_id INTEGER NOT NULL, PRIMARY KEY(guild_id,level))",
        "CREATE TABLE IF NOT EXISTS afk_members (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY(guild_id,user_id))",
        "CREATE TABLE IF NOT EXISTS df_tickets (channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, opener_id INTEGER NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, claimed_by INTEGER)",
        "CREATE TABLE IF NOT EXISTS df_vouches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, giver_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(guild_id,giver_id,receiver_id,details))",
        "CREATE TABLE IF NOT EXISTS df_warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE IF NOT EXISTS df_suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, suggestion TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, reviewer_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS df_birthdays (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, birthday TEXT NOT NULL, last_greeted_year INTEGER, PRIMARY KEY(guild_id,user_id))",
        "CREATE TABLE IF NOT EXISTS df_tags (guild_id INTEGER NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL, creator_id INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,name))",
        "CREATE TABLE IF NOT EXISTS df_starboard (guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, star_message_id INTEGER NOT NULL, PRIMARY KEY(guild_id,message_id))",
        "CREATE TABLE IF NOT EXISTS df_counting (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, current_number INTEGER NOT NULL DEFAULT 0, last_user_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS df_guild_settings (guild_id INTEGER PRIMARY KEY, suggestion_channel_id INTEGER, starboard_channel_id INTEGER, starboard_threshold INTEGER NOT NULL DEFAULT 3, ticket_category_id INTEGER, ticket_log_channel_id INTEGER, mod_log_channel_id INTEGER, media_only_channel_id INTEGER, counting_channel_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS df_birthday_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS df_codes_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), codes TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS df_monthly_leaderboard_runs (month_key TEXT PRIMARY KEY, posted_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS df_bonus_entries (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, entries INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id))",
        "CREATE TABLE IF NOT EXISTS mega_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, reminder TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)",
    ]
        for statement in statements:
            await db.execute(statement)
        _community_schema_ready = True


@bot.listen("on_ready")
async def community_schema_ready():
    try:
        await _ensure_community_schema()
    except Exception:
        log.exception("Community feature schema setup failed")


def _xp_for_next_level(level: int) -> int:
    return 100 + (level * 50)


async def _make_levelup_card(member: discord.Member, level: int, xp: int, needed: int) -> discord.File | None:
    """Render a branded purple level-up card with the member avatar using Pillow."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont, ImageOps
        width, height = 1000, 320
        image = Image.new("RGB", (width, height))
        pixels = image.load()
        for x in range(width):
            blend = x / max(1, width - 1)
            r = int(13 + (45 - 13) * blend)
            g = int(10 + (20 - 10) * blend)
            b = int(28 + (90 - 28) * blend)
            for y in range(height):
                glow = max(0, 1 - (((x - 790) / 460) ** 2 + ((y - 120) / 260) ** 2)) * 18
                pixels[x, y] = (min(255, r + int(glow)), min(255, g), min(255, b + int(glow)))
        draw = ImageDraw.Draw(image)
        try:
            title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
            level_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 76)
            body_font = ImageFont.truetype("DejaVuSans.ttf", 25)
        except OSError:
            title_font = level_font = body_font = ImageFont.load_default()
        avatar_bytes = await member.display_avatar.with_size(256).read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        avatar = ImageOps.fit(avatar, (210, 210))
        mask = Image.new("L", (210, 210), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 209, 209), fill=255)
        image.paste(avatar, (54, 55), mask)
        draw.ellipse((48, 49, 270, 271), outline=(177, 110, 255), width=7)
        draw.text((310, 42), "DARK FRUITS • LEVEL UP", font=title_font, fill=(214, 180, 255))
        draw.text((310, 95), f"LEVEL {level}", font=level_font, fill=(255, 255, 255))
        draw.text((315, 205), f"{member.display_name[:28]}  •  {xp:,} / {needed:,} XP", font=body_font, fill=(218, 216, 235))
        bar_x, bar_y, bar_w, bar_h = 315, 258, 610, 18
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=9, fill=(54, 48, 75))
        progress = max(0, min(1, xp / max(1, needed)))
        draw.rounded_rectangle((bar_x, bar_y, bar_x + int(bar_w * progress), bar_y + bar_h), radius=9, fill=(173, 93, 255))
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return discord.File(output, filename="dark-fruits-levelup.png")
    except Exception:
        log.debug("Could not render level-up card", exc_info=True)
        return None


@bot.listen("on_message")
async def community_message_features(message: discord.Message):
    if not message.guild or message.author.bot or message.webhook_id:
        return
    try:
        await _ensure_community_schema()
        # AFK state is cleared on the member's next message.
        afk = await db.fetchone("SELECT reason, since FROM afk_members WHERE guild_id = ? AND user_id = ?", (message.guild.id, message.author.id))
        if afk:
            await db.execute("DELETE FROM afk_members WHERE guild_id = ? AND user_id = ?", (message.guild.id, message.author.id))
            try:
                await message.reply("👋 Welcome back! I've removed your AFK status.", mention_author=False, delete_after=8)
            except discord.HTTPException:
                pass
        # Notify authors when they mention an AFK member.
        for mentioned in message.mentions[:5]:
            row = await db.fetchone("SELECT reason, since FROM afk_members WHERE guild_id = ? AND user_id = ?", (message.guild.id, mentioned.id))
            if row:
                try:
                    await message.reply(f"💤 **{mentioned.display_name} is AFK:** {row['reason']}", mention_author=False, delete_after=12)
                except discord.HTTPException:
                    pass
        # XP uses a persisted one-minute cooldown to prevent spam farming.
        row = await db.fetchone("SELECT xp, level, last_xp FROM member_xp WHERE guild_id = ? AND user_id = ?", (message.guild.id, message.author.id))
        now = datetime.datetime.now(datetime.timezone.utc)
        if row and row["last_xp"]:
            try:
                last = datetime.datetime.fromisoformat(row["last_xp"])
                if (now - last).total_seconds() < 60:
                    return
            except (ValueError, TypeError):
                pass
        old_xp = int(row["xp"]) if row else 0
        old_level = int(row["level"]) if row else 0
        new_xp = old_xp + random.randint(15, 25)
        new_level = old_level
        while new_xp >= _xp_for_next_level(new_level):
            new_xp -= _xp_for_next_level(new_level)
            new_level += 1
        await db.execute("INSERT INTO member_xp (guild_id,user_id,xp,level,last_xp) VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level, last_xp=excluded.last_xp", (message.guild.id, message.author.id, new_xp, new_level, now.isoformat()))
        if new_level > old_level:
            needed = _xp_for_next_level(new_level)
            embed = _suite_embed("✨ LEVEL UP!", f"{message.author.mention} reached **Level {new_level}**! Keep sailing, pirate.")
            embed.add_field(name="Progress", value=f"`{new_xp}/{needed} XP`", inline=False)
            card = await _make_levelup_card(message.author, new_level, new_xp, needed)
            if card:
                embed.set_image(url="attachment://dark-fruits-levelup.png")
            try:
                if card:
                    await message.channel.send(embed=embed, file=card, delete_after=20)
                else:
                    await message.channel.send(embed=embed, delete_after=15)
            except discord.HTTPException:
                pass
            reward = await db.fetchone("SELECT role_id FROM level_rewards WHERE guild_id = ? AND level = ?", (message.guild.id, new_level))
            if reward and isinstance(message.author, discord.Member):
                role = message.guild.get_role(int(reward["role_id"]))
                if role and role < message.guild.me.top_role:
                    try:
                        await message.author.add_roles(role, reason=f"Reached level {new_level}")
                    except discord.HTTPException:
                        pass
    except Exception:
        log.exception("Community message features failed")


@bot.tree.command(name="rank", description="View your or another member's XP rank.")
@app_commands.describe(member="Member to inspect; defaults to you")
async def community_rank(interaction: discord.Interaction, member: discord.Member | None = None):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    member = member or interaction.user
    row = await db.fetchone("SELECT xp, level FROM member_xp WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
    xp, level = (int(row["xp"]), int(row["level"])) if row else (0, 0)
    total = await db.fetchall("SELECT user_id, level, xp FROM member_xp WHERE guild_id = ? ORDER BY level DESC, xp DESC", (interaction.guild.id,))
    rank = next((i for i, r in enumerate(total, 1) if int(r["user_id"]) == member.id), None)
    needed = _xp_for_next_level(level)
    progress = min(10, int((xp / needed) * 10))
    bar = "🟪" * progress + "⬛" * (10 - progress)
    embed = _suite_embed(f"🏴‍☠️ {member.display_name}'s Rank", f"**Level {level}**\n{bar}\n`{xp} / {needed} XP`")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Server rank", value=f"`#{rank}`" if rank else "Not ranked yet", inline=True)
    embed.add_field(name="XP remaining", value=f"`{max(0, needed - xp)}`", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="level_leaderboard", description="Show the top 10 members by XP level.")
async def community_level_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    rows = await db.fetchall("SELECT user_id, level, xp FROM member_xp WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10", (interaction.guild.id,))
    lines = []
    for i, row in enumerate(rows, 1):
        member = interaction.guild.get_member(int(row["user_id"]))
        label = member.display_name if member else f"User {row['user_id']}"
        lines.append(f"**{i}.** {label} — Level `{row['level']}` · `{row['xp']} XP`")
    await interaction.response.send_message(embed=_suite_embed("🏆 XP Leaderboard", "\n".join(lines) or "No XP earned yet. Send messages to start levelling up!"))


@bot.tree.command(name="set_level_reward", description="Set a role reward for a level (staff only).")
@app_commands.describe(level="Level required", role="Role to grant at that level")
@app_commands.checks.has_permissions(manage_roles=True)
async def community_set_level_reward(interaction: discord.Interaction, level: app_commands.Range[int, 1, 10000], role: discord.Role):
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I can't grant a role equal to or above my highest role.", ephemeral=True)
    await _ensure_community_schema()
    await db.execute("INSERT INTO level_rewards (guild_id,level,role_id) VALUES (?,?,?) ON CONFLICT(guild_id,level) DO UPDATE SET role_id=excluded.role_id", (interaction.guild.id, level, role.id))
    await interaction.response.send_message(f"✅ Members will receive {role.mention} when they reach **Level {level}** (if my role can manage it).", ephemeral=True)


@bot.tree.command(name="afk", description="Set your AFK status and an optional reason.")
@app_commands.describe(reason="Why you're away")
async def community_afk(interaction: discord.Interaction, reason: str = "Away from the keyboard"):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    await db.execute("INSERT INTO afk_members (guild_id,user_id,reason,since) VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET reason=excluded.reason,since=excluded.since", (interaction.guild.id, interaction.user.id, reason[:500], datetime.datetime.now(datetime.timezone.utc).isoformat()))
    await interaction.response.send_message(f"💤 AFK enabled: **{reason[:500]}**\nI'll clear it when you next send a message.", ephemeral=True)


@bot.tree.command(name="warn_member", description="Issue a recorded warning (staff only).")
@app_commands.describe(member="Member to warn", reason="Reason for the warning")
@app_commands.checks.has_permissions(moderate_members=True)
async def community_warn_member(interaction: discord.Interaction, member: discord.Member, reason: str):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("Choose a different human member.", ephemeral=True)
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_warnings (guild_id,user_id,moderator_id,reason,created_at,active) VALUES (?,?,?,?,?,1)", (interaction.guild.id, member.id, interaction.user.id, reason[:1000], datetime.datetime.now(datetime.timezone.utc).isoformat()))
    row = await db.fetchone("SELECT COUNT(*) AS c FROM df_warnings WHERE guild_id = ? AND user_id = ? AND active = 1", (interaction.guild.id, member.id))
    try:
        await member.send(f"⚠️ You received a warning in **{interaction.guild.name}**.\nReason: {reason[:1000]}")
    except discord.HTTPException:
        pass
    await interaction.response.send_message(f"⚠️ Warned {member.mention}. Active warnings: **{row['c']}**. Reason: {reason}", ephemeral=True)


@bot.tree.command(name="warnings", description="View a member's active warnings (staff only).")
@app_commands.describe(member="Member to inspect")
@app_commands.checks.has_permissions(moderate_members=True)
async def community_warnings(interaction: discord.Interaction, member: discord.Member):
    await _ensure_community_schema()
    rows = await db.fetchall("SELECT id, moderator_id, reason, created_at FROM df_warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id DESC LIMIT 15", (interaction.guild.id, member.id))
    embed = _suite_embed(f"⚠️ Warnings for {member.display_name}", f"Active warnings: **{len(rows)}**")
    for row in rows:
        ts = int(datetime.datetime.fromisoformat(row["created_at"]).timestamp())
        embed.add_field(name=f"Warning #{row['id']} • <t:{ts}:R>", value=f"Moderator: <@{row['moderator_id']}>\nReason: {row['reason'][:700]}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clear_warnings", description="Clear a member's active warnings (staff only).")
@app_commands.describe(member="Member whose warnings should be cleared")
@app_commands.checks.has_permissions(moderate_members=True)
async def community_clear_warnings(interaction: discord.Interaction, member: discord.Member):
    await _ensure_community_schema()
    await db.execute("UPDATE df_warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1", (interaction.guild.id, member.id))
    await interaction.response.send_message(f"✅ Cleared active warnings for {member.mention}.", ephemeral=True)


@bot.tree.command(name="suggest", description="Submit an idea to the Dark Fruits team.")
@app_commands.describe(suggestion="Your idea or improvement")
async def community_suggest(interaction: discord.Interaction, suggestion: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    settings = await db.fetchone("SELECT suggestion_channel_id FROM df_guild_settings WHERE guild_id = ?", (interaction.guild.id,))
    channel_id = int(settings["suggestion_channel_id"] or 0) if settings else 0
    target = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel
    if not isinstance(target, discord.TextChannel):
        target = interaction.channel
    cur = await db.execute("INSERT INTO df_suggestions (guild_id,user_id,channel_id,message_id,suggestion,status,created_at) VALUES (?,?,?,?,?,'pending',?)", (interaction.guild.id, interaction.user.id, target.id, None, suggestion[:3000], datetime.datetime.now(datetime.timezone.utc).isoformat()))
    suggestion_id = cur.lastrowid
    embed = _suite_embed(f"💡 Community Suggestion #{suggestion_id}", suggestion[:3000])
    embed.add_field(name="Submitted by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Status", value="🟡 Pending review", inline=True)
    message = await target.send(embed=embed)
    await message.add_reaction("👍")
    await message.add_reaction("👎")
    await db.execute("UPDATE df_suggestions SET message_id = ? WHERE id = ?", (message.id, suggestion_id))
    await interaction.response.send_message(f"✅ Your suggestion was submitted in {target.mention}. Reference: `#{suggestion_id}`", ephemeral=True)


@bot.tree.command(name="suggestion_channel", description="Set the suggestion destination (staff only).")
@app_commands.describe(channel="Channel where suggestions should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_suggestion_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_guild_settings (guild_id,suggestion_channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET suggestion_channel_id=excluded.suggestion_channel_id", (interaction.guild.id, channel.id))
    await interaction.response.send_message(f"✅ Suggestions will be posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="suggestion_status", description="Approve, reject or mark a suggestion in progress (staff only).")
@app_commands.describe(suggestion_id="ID from the suggestion title", status="New status", note="Optional staff note")
@app_commands.choices(status=[app_commands.Choice(name="Approved", value="approved"), app_commands.Choice(name="Rejected", value="rejected"), app_commands.Choice(name="In progress", value="in progress"), app_commands.Choice(name="Completed", value="completed")])
@app_commands.checks.has_permissions(manage_messages=True)
async def community_suggestion_status(interaction: discord.Interaction, suggestion_id: app_commands.Range[int, 1, 2147483647], status: str, note: str = ""):
    await _ensure_community_schema()
    row = await db.fetchone("SELECT * FROM df_suggestions WHERE id = ? AND guild_id = ?", (suggestion_id, interaction.guild.id))
    if not row:
        return await interaction.response.send_message("Suggestion not found.", ephemeral=True)
    await db.execute("UPDATE df_suggestions SET status = ?, reviewer_id = ? WHERE id = ?", (status, interaction.user.id, suggestion_id))
    reward_note = ""
    if status in {"approved", "completed"} and row["status"] not in {"approved", "completed"}:
        # Reward a genuinely accepted idea once: optional role + two bonus entries.
        reward_role_id = int(os.environ.get("SUGGESTION_REWARD_ROLE_ID", "0") or 0)
        reward_role = interaction.guild.get_role(reward_role_id) if reward_role_id else None
        suggester = interaction.guild.get_member(int(row["user_id"]))
        if reward_role and suggester and reward_role < interaction.guild.me.top_role:
            try:
                await suggester.add_roles(reward_role, reason=f"Suggestion #{suggestion_id} approved")
                reward_note = f" Reward role granted: {reward_role.mention}."
            except discord.HTTPException:
                pass
        await db.execute("INSERT INTO df_bonus_entries (guild_id,user_id,entries) VALUES (?,?,2) ON CONFLICT(guild_id,user_id) DO UPDATE SET entries=entries+2", (interaction.guild.id, int(row["user_id"])))
        reward_note += " Two bonus giveaway entries added for the suggester's next giveaway entry."
    channel = interaction.guild.get_channel(int(row["channel_id"]))
    if isinstance(channel, discord.TextChannel) and row["message_id"]:
        try:
            msg = await channel.fetch_message(int(row["message_id"]))
            embed = msg.embeds[0] if msg.embeds else _suite_embed(f"Suggestion #{suggestion_id}", row["suggestion"])
            embed.color = discord.Color.green() if status in {"approved", "completed"} else discord.Color.red() if status == "rejected" else DARK_FRUITS_PURPLE
            embed.add_field(name="Status", value=f"**{status.title()}**\n{note[:700]}", inline=False)
            embed.add_field(name="Reviewed by", value=interaction.user.mention, inline=True)
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass
    await interaction.response.send_message(f"✅ Suggestion `#{suggestion_id}` marked **{status}**.{reward_note}", ephemeral=True)


@bot.tree.command(name="vouch", description="Vouch for a member after a trade or helpful interaction.")
@app_commands.describe(member="Member you're vouching for", details="What went well?")
async def community_vouch(interaction: discord.Interaction, member: discord.Member, details: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    if member.id == interaction.user.id or member.bot:
        return await interaction.response.send_message("You can't vouch for yourself or a bot.", ephemeral=True)
    await _ensure_community_schema()
    try:
        await db.execute("INSERT INTO df_vouches (guild_id,giver_id,receiver_id,details,created_at) VALUES (?,?,?,?,?)", (interaction.guild.id, interaction.user.id, member.id, details[:1000], datetime.datetime.now(datetime.timezone.utc).isoformat()))
    except Exception:
        return await interaction.response.send_message("You've already given this member the same vouch. Try adding different details if this is a separate trade.", ephemeral=True)
    count = await db.fetchone("SELECT COUNT(*) AS c FROM df_vouches WHERE guild_id = ? AND receiver_id = ?", (interaction.guild.id, member.id))
    embed = _suite_embed("🤝 New Community Vouch", f"{interaction.user.mention} vouched for {member.mention}.\n\n**Details:** {details[:1000]}")
    embed.add_field(name="Total vouches", value=f"`{count['c']}`", inline=True)
    vouch_channel_id = int(os.environ.get("VOUCH_CHANNEL_ID", "1555694574452736041") or 0)
    vouch_channel = interaction.guild.get_channel(vouch_channel_id) if vouch_channel_id else None
    if isinstance(vouch_channel, discord.TextChannel) and vouch_channel.id != interaction.channel_id:
        await vouch_channel.send(embed=embed)
        await interaction.response.send_message(f"✅ Vouch recorded and posted in {vouch_channel.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="vouches", description="Check a member's vouch count and recent feedback.")
@app_commands.describe(member="Member to check")
async def community_vouches(interaction: discord.Interaction, member: discord.Member):
    await _ensure_community_schema()
    rows = await db.fetchall("SELECT giver_id, details, created_at FROM df_vouches WHERE guild_id = ? AND receiver_id = ? ORDER BY id DESC LIMIT 5", (interaction.guild.id, member.id))
    count = await db.fetchone("SELECT COUNT(*) AS c FROM df_vouches WHERE guild_id = ? AND receiver_id = ?", (interaction.guild.id, member.id))
    embed = _suite_embed(f"🤝 {member.display_name}'s Vouches", f"**Total vouches:** `{count['c']}`")
    for row in rows:
        embed.add_field(name=f"From <@{row['giver_id']}> • <t:{int(datetime.datetime.fromisoformat(row['created_at']).timestamp())}:R>", value=row["details"][:900], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="birthday", description="Set your birthday for a server shout-out (DD-MM).")
@app_commands.describe(date="Your birthday in DD-MM format, e.g. 23-04")
async def community_birthday(interaction: discord.Interaction, date: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    try:
        parsed = datetime.datetime.strptime(date, "%d-%m")
        normalized = parsed.strftime("%d-%m")
    except ValueError:
        return await interaction.response.send_message("Use **DD-MM**, for example `23-04`.", ephemeral=True)
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_birthdays (guild_id,user_id,birthday,last_greeted_year) VALUES (?,?,?,NULL) ON CONFLICT(guild_id,user_id) DO UPDATE SET birthday=excluded.birthday,last_greeted_year=NULL", (interaction.guild.id, interaction.user.id, normalized))
    await interaction.response.send_message(f"🎂 Birthday saved as **{normalized}**. I'll give you a shout-out on that date if a birthday channel is configured.", ephemeral=True)


@bot.tree.command(name="birthday_channel", description="Set the birthday shout-out channel (staff only).")
@app_commands.describe(channel="Channel for birthday greetings")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_birthday_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_guild_settings (guild_id) VALUES (?) ON CONFLICT(guild_id) DO NOTHING", (interaction.guild.id,))
    # Reuse a dedicated setting key through the existing schema extension table.
    await db.execute("CREATE TABLE IF NOT EXISTS df_birthday_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)")
    await db.execute("INSERT INTO df_birthday_channels (guild_id,channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (interaction.guild.id, channel.id))
    await interaction.response.send_message(f"🎂 Birthday greetings will go to {channel.mention}.", ephemeral=True)


@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc))
async def mega_birthday_dispatcher():
    try:
        await _ensure_community_schema()
        await db.execute("CREATE TABLE IF NOT EXISTS df_birthday_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)")
        now = datetime.datetime.now(datetime.timezone.utc)
        birthday = now.strftime("%d-%m")
        rows = await db.fetchall("SELECT * FROM df_birthdays WHERE birthday = ? AND (last_greeted_year IS NULL OR last_greeted_year < ?)", (birthday, now.year))
        for row in rows:
            guild = bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue
            target_row = await db.fetchone("SELECT channel_id FROM df_birthday_channels WHERE guild_id = ?", (guild.id,))
            target = guild.get_channel(int(target_row["channel_id"])) if target_row else None
            member = guild.get_member(int(row["user_id"]))
            if not member:
                try: member = await guild.fetch_member(int(row["user_id"]))
                except discord.HTTPException: member = None
            if isinstance(target, discord.TextChannel) and member:
                await target.send(embed=_suite_embed("🎂 HAPPY BIRTHDAY!", f"Everyone wish {member.mention} a fantastic birthday! 💜🏴‍☠️"))
            await db.execute("UPDATE df_birthdays SET last_greeted_year = ? WHERE guild_id = ? AND user_id = ?", (now.year, row["guild_id"], row["user_id"]))
    except Exception:
        log.exception("Birthday dispatcher failed")


@mega_birthday_dispatcher.before_loop
async def before_mega_birthday_dispatcher():
    await bot.wait_until_ready()


@bot.listen("on_ready")
async def start_mega_birthday_dispatcher():
    if not mega_birthday_dispatcher.is_running():
        mega_birthday_dispatcher.start()


@bot.tree.command(name="tag_create", description="Create a reusable server text tag (staff only).")
@app_commands.describe(name="Short tag name", content="Text returned by the tag")
@app_commands.checks.has_permissions(manage_messages=True)
async def community_tag_create(interaction: discord.Interaction, name: str, content: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    normalized = re.sub(r"[^a-z0-9_-]", "", name.lower())[:32]
    if not normalized or not content.strip():
        return await interaction.response.send_message("Tag names can contain letters, numbers, `_` and `-`; content can't be empty.", ephemeral=True)
    await _ensure_community_schema()
    try:
        await db.execute("INSERT INTO df_tags (guild_id,name,content,creator_id,uses) VALUES (?,?,?,?,0)", (interaction.guild.id, normalized, content[:1800], interaction.user.id))
    except Exception:
        return await interaction.response.send_message("That tag already exists. Delete it first or choose another name.", ephemeral=True)
    await interaction.response.send_message(f"✅ Created tag `{normalized}`. Use `/tag` to retrieve it.", ephemeral=True)


@bot.tree.command(name="tag", description="Retrieve a saved server tag.")
@app_commands.describe(name="Tag name")
async def community_tag(interaction: discord.Interaction, name: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    row = await db.fetchone("SELECT content FROM df_tags WHERE guild_id = ? AND name = ?", (interaction.guild.id, name.lower()))
    if not row:
        return await interaction.response.send_message("Tag not found.", ephemeral=True)
    await db.execute("UPDATE df_tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (interaction.guild.id, name.lower()))
    await interaction.response.send_message(row["content"], allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="tag_delete", description="Delete a saved server tag (staff only).")
@app_commands.describe(name="Tag name")
@app_commands.checks.has_permissions(manage_messages=True)
async def community_tag_delete(interaction: discord.Interaction, name: str):
    await _ensure_community_schema()
    cur = await db.execute("DELETE FROM df_tags WHERE guild_id = ? AND name = ?", (interaction.guild.id, name.lower()))
    if cur.rowcount:
        await interaction.response.send_message(f"🗑️ Deleted tag `{name.lower()}`.", ephemeral=True)
    else:
        await interaction.response.send_message("Tag not found.", ephemeral=True)


@bot.tree.command(name="starboard_channel", description="Configure the starboard destination and threshold (staff only).")
@app_commands.describe(channel="Channel where starred messages are reposted", threshold="Number of ⭐ reactions needed")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_starboard_channel(interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int, 2, 20] = 3):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_guild_settings (guild_id,starboard_channel_id,starboard_threshold) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET starboard_channel_id=excluded.starboard_channel_id,starboard_threshold=excluded.starboard_threshold", (interaction.guild.id, channel.id, threshold))
    await interaction.response.send_message(f"⭐ Starboard set to {channel.mention} with a threshold of **{threshold} stars**.", ephemeral=True)


@bot.listen("on_raw_reaction_add")
async def mega_starboard_reaction(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) not in {"⭐", "🌟"} or not payload.guild_id or not payload.member or payload.member.bot:
        return
    try:
        await _ensure_community_schema()
        settings = await db.fetchone("SELECT starboard_channel_id, starboard_threshold FROM df_guild_settings WHERE guild_id = ?", (payload.guild_id,))
        if not settings or not settings["starboard_channel_id"]:
            return
        target = bot.get_channel(int(settings["starboard_channel_id"]))
        source = bot.get_channel(payload.channel_id)
        if not isinstance(target, discord.TextChannel) or not isinstance(source, discord.TextChannel) or target.id == source.id:
            return
        source_msg = await source.fetch_message(payload.message_id)
        if source_msg.author.bot:
            return
        reaction = next((r for r in source_msg.reactions if str(r.emoji) in {"⭐", "🌟"}), None)
        if not reaction or reaction.count < int(settings["starboard_threshold"]):
            return
        exists = await db.fetchone("SELECT star_message_id FROM df_starboard WHERE guild_id = ? AND message_id = ?", (payload.guild_id, payload.message_id))
        if exists:
            return
        embed = _suite_embed("⭐ Starboard", (source_msg.content or "[Image or attachment]")[:3000])
        embed.add_field(name="Original", value=f"[Jump to message]({source_msg.jump_url}) · {source.mention}", inline=False)
        embed.set_author(name=str(source_msg.author), icon_url=source_msg.author.display_avatar.url)
        if source_msg.attachments:
            image_file = next((a for a in source_msg.attachments if a.content_type and a.content_type.startswith("image/")), None)
            if image_file: embed.set_image(url=image_file.url)
        posted = await target.send(embed=embed)
        await db.execute("INSERT INTO df_starboard (guild_id,message_id,star_message_id) VALUES (?,?,?)", (payload.guild_id, payload.message_id, posted.id))
    except Exception:
        log.exception("Starboard reaction handler failed")


@bot.tree.command(name="counting_channel", description="Set the channel for the counting game (staff only).")
@app_commands.describe(channel="Text channel dedicated to counting")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_counting_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_counting (guild_id,channel_id,current_number,last_user_id) VALUES (?,?,0,NULL) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (interaction.guild.id, channel.id))
    await interaction.response.send_message(f"🔢 Counting game set up in {channel.mention}. Start with **1**!", ephemeral=True)


@bot.listen("on_message")
async def mega_counting_game(message: discord.Message):
    if not message.guild or message.author.bot or not message.content.strip().isdigit():
        return
    try:
        row = await db.fetchone("SELECT channel_id,current_number,last_user_id FROM df_counting WHERE guild_id = ?", (message.guild.id,))
        if not row or message.channel.id != int(row["channel_id"]):
            return
        number = int(message.content.strip())
        expected = int(row["current_number"]) + 1
        if int(row["last_user_id"] or 0) == message.author.id or number != expected:
            await db.execute("UPDATE df_counting SET current_number=0,last_user_id=NULL WHERE guild_id = ?", (message.guild.id,))
            try:
                await message.add_reaction("❌")
                await message.channel.send(f"💥 Counting reset! The next number is **1** — don't count twice in a row or skip numbers.", delete_after=8)
            except discord.HTTPException:
                pass
            return
        await db.execute("UPDATE df_counting SET current_number=?,last_user_id=? WHERE guild_id=?", (number, message.author.id, message.guild.id))
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
    except Exception:
        log.exception("Counting game handler failed")


@bot.tree.command(name="ticket_open", description="Open a private support ticket.")
@app_commands.describe(topic="Briefly describe what you need help with")
async def community_ticket_open(interaction: discord.Interaction, topic: str = "General support"):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    existing = await db.fetchone("SELECT channel_id FROM df_tickets WHERE guild_id = ? AND opener_id = ? AND closed_at IS NULL", (interaction.guild.id, interaction.user.id))
    if existing:
        channel = interaction.guild.get_channel(int(existing["channel_id"]))
        if channel:
            return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
    category_id = int(os.environ.get("TICKET_CATEGORY_ID", "0") or 0)
    category = interaction.guild.get_channel(category_id) if category_id else None
    overwrites = {interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True), interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)}
    staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    safe_name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:18] or str(interaction.user.id)
    channel = await interaction.guild.create_text_channel(f"ticket-{safe_name}", category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, topic=f"Ticket opener={interaction.user.id}; topic={topic[:150]}", reason=f"Support ticket opened by {interaction.user}")
    await db.execute("INSERT INTO df_tickets (channel_id,guild_id,opener_id,opened_at,closed_at,claimed_by) VALUES (?,?,?,?,NULL,NULL)", (channel.id, interaction.guild.id, interaction.user.id, datetime.datetime.now(datetime.timezone.utc).isoformat()))
    embed = _suite_embed("🎫 Dark Fruits Support Ticket", f"Welcome {interaction.user.mention}!\n\n**Topic:** {topic[:500]}\nPlease explain the issue and wait for a staff member. Do not share passwords or private account details.")
    await channel.send(content=f"{interaction.user.mention} {staff_role.mention if staff_role else ''}", embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=bool(staff_role)))
    await interaction.response.send_message(f"✅ Your private ticket is ready: {channel.mention}", ephemeral=True)


@bot.tree.command(name="ticket_claim", description="Claim the current support ticket (staff only).")
@app_commands.checks.has_permissions(manage_channels=True)
async def community_ticket_claim(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
    await _ensure_community_schema()
    row = await db.fetchone("SELECT channel_id FROM df_tickets WHERE channel_id = ? AND closed_at IS NULL", (interaction.channel.id,))
    if not row:
        return await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
    await db.execute("UPDATE df_tickets SET claimed_by = ? WHERE channel_id = ?", (interaction.user.id, interaction.channel.id))
    await interaction.response.send_message(f"🫡 {interaction.user.mention} has claimed this ticket.")


@bot.tree.command(name="ticket_close", description="Close the current support ticket.")
@app_commands.describe(reason="Reason for closing")
async def community_ticket_close(interaction: discord.Interaction, reason: str = "Resolved"):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
    await _ensure_community_schema()
    row = await db.fetchone("SELECT opener_id FROM df_tickets WHERE channel_id = ? AND closed_at IS NULL", (interaction.channel.id,))
    if not row:
        return await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
    if interaction.user.id != int(row["opener_id"]) and not _suite_staff(interaction):
        return await interaction.response.send_message("Only the ticket opener or staff can close this ticket.", ephemeral=True)
    await db.execute("UPDATE df_tickets SET closed_at = ? WHERE channel_id = ?", (datetime.datetime.now(datetime.timezone.utc).isoformat(), interaction.channel.id))
    await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:95], reason=f"Ticket closed by {interaction.user}: {reason[:200]}")
    await interaction.channel.set_permissions(interaction.guild.default_role, view_channel=False)
    opener = interaction.guild.get_member(int(row["opener_id"]))
    if opener:
        await interaction.channel.set_permissions(opener, view_channel=False, send_messages=False)
    await interaction.response.send_message(f"🔒 Ticket closed by {interaction.user.mention}. Reason: {reason[:500]}")
    log_id = int(os.environ.get("TICKET_LOG_CHANNEL_ID", "0") or 0)
    log_channel = interaction.guild.get_channel(log_id) if log_id else None
    if isinstance(log_channel, discord.TextChannel):
        await log_channel.send(embed=_suite_embed("🎫 Ticket closed", f"**Channel:** #{interaction.channel.name}\n**Opener:** <@{row['opener_id']}>\n**Closed by:** {interaction.user.mention}\n**Reason:** {reason[:700]}"))
        # Attach a readable transcript for staff records.
        try:
            import io
            transcript_lines = [f"Dark Fruits ticket transcript — {interaction.channel.name}", f"Guild: {interaction.guild.name} ({interaction.guild.id})", f"Ticket opener ID: {row['opener_id']}", f"Closed by: {interaction.user} ({interaction.user.id})", f"Reason: {reason}", "=" * 72, ""]
            async for item in interaction.channel.history(limit=300, oldest_first=True):
                timestamp = item.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                body = item.content or "[no text]"
                if item.attachments:
                    body += "\nAttachments: " + ", ".join(a.url for a in item.attachments)
                if item.embeds:
                    body += f"\n[Embeds: {len(item.embeds)}]"
                transcript_lines.append(f"[{timestamp}] {item.author} ({item.author.id}):\n{body}\n")
            transcript = "\n".join(transcript_lines).encode("utf-8", errors="replace")[:7_500_000]
            await log_channel.send(file=discord.File(io.BytesIO(transcript), filename=f"ticket-{interaction.channel.id}-transcript.txt"))
        except Exception:
            log.exception("Ticket transcript export failed")


@bot.tree.command(name="set_media_channel", description="Configure the media-only channel (staff only).")
@app_commands.describe(channel="Channel where image/video-only posting is enforced")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_set_media_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_guild_settings (guild_id,media_only_channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET media_only_channel_id=excluded.media_only_channel_id", (interaction.guild.id, channel.id))
    await interaction.response.send_message(f"🖼️ {channel.mention} is now configured as the media-only channel. This setting is saved; existing server automod rules are not altered.", ephemeral=True)


@bot.tree.command(name="set_mod_log", description="Set the moderation/audit log channel (staff only).")
@app_commands.describe(channel="Channel for audit logs")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_set_mod_log(interaction: discord.Interaction, channel: discord.TextChannel):
    await _ensure_community_schema()
    await db.execute("INSERT INTO df_guild_settings (guild_id,mod_log_channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET mod_log_channel_id=excluded.mod_log_channel_id", (interaction.guild.id, channel.id))
    await interaction.response.send_message(f"🛡️ Saved {channel.mention} as this server's moderation log channel.", ephemeral=True)



# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURABLE AUTO-MODERATION — staff bypass; channel IDs can be changed in Render.
# ═══════════════════════════════════════════════════════════════════════════════

_MEDIA_URL_RE = re.compile(r"(?:https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)


def _member_is_staff_for_automod(member: discord.Member) -> bool:
    return is_staff(member) or member.guild_permissions.administrator or member.guild_permissions.manage_messages


@bot.listen("on_message")
async def mega_automod_message(message: discord.Message):
    if not message.guild or message.author.bot or message.webhook_id or not isinstance(message.author, discord.Member):
        return
    if _member_is_staff_for_automod(message.author):
        return
    try:
        await _ensure_community_schema()
        settings = await db.fetchone("SELECT media_only_channel_id FROM df_guild_settings WHERE guild_id = ?", (message.guild.id,))
        media_only_id = int(settings["media_only_channel_id"] or 0) if settings else 0
        if not media_only_id:
            media_only_id = int(os.environ.get("MEDIA_ONLY_CHANNEL_ID", "1504931608170463312") or 0)
        allowed_media_id = int(os.environ.get("ALLOWED_MEDIA_CHANNEL_ID", "1489744139930173550") or 0)
        channel_id = message.channel.id
        attachments = list(message.attachments)
        has_media = any((a.content_type or "").startswith(("image/", "video/")) for a in attachments)
        has_gif = any("gif" in (a.content_type or "").lower() or a.filename.lower().endswith((".gif", ".gifv")) for a in attachments)
        has_link = bool(_MEDIA_URL_RE.search(message.content or "")) or any(_MEDIA_URL_RE.search(a.url) for a in attachments)
        violation = None
        if channel_id == media_only_id:
            # Dedicated media channel: only actual image/video attachments, no GIFs,
            # links, captions or plain text. Staff can still post instructions.
            if not has_media or has_gif or has_link or (message.content or "").strip():
                violation = "This is a media-only channel: post an image or video attachment only (no captions, links or GIFs)."
        else:
            block_links = os.environ.get("AUTOMOD_BLOCK_LINKS", "true").lower() in {"1", "true", "yes", "on"}
            block_media = os.environ.get("AUTOMOD_BLOCK_MEDIA_OUTSIDE_CHANNEL", "true").lower() in {"1", "true", "yes", "on"}
            if channel_id == allowed_media_id:
                return
            if has_link and block_links:
                violation = "Links and Discord invites aren't allowed in this channel. Please use the approved channels."
            elif has_media and block_media:
                violation = "Images and videos belong in the approved media channel."
        if violation:
            try:
                await message.delete(reason="Dark Fruits AutoMod channel rules")
            except discord.HTTPException:
                return
            try:
                await message.channel.send(f"⚠️ {message.author.mention} {violation}", delete_after=8, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            except discord.HTTPException:
                pass
    except Exception:
        log.exception("AutoMod handler failed")


@bot.listen("on_member_join")
async def mega_log_member_join(member: discord.Member):
    try:
        channel = await _audit_log_channel(member.guild)
        if not channel:
            return
        embed = _suite_embed("👋 Member joined", f"**Member:** {member.mention} (`{member.id}`)\n**Account created:** <t:{int(member.created_at.timestamp())}:R>\n**Member count:** `{member.guild.member_count or 'unknown'}`")
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)
    except Exception:
        log.exception("Member join audit log failed")



# Standardly named moderation aliases, so staff can discover the expected commands.

@bot.tree.command(name="ban", description="Ban a member from the server (staff only).")
@app_commands.describe(member="Member to ban", reason="Reason", delete_message_days="Delete recent messages (0–7 days)")
@app_commands.checks.has_permissions(ban_members=True)
async def standard_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason supplied", delete_message_days: app_commands.Range[int, 0, 7] = 0):
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You can't ban yourself or an administrator.", ephemeral=True)
    await member.ban(reason=f"{reason} — by {interaction.user}", delete_message_days=delete_message_days)
    await interaction.response.send_message(f"🔨 Banned {member} (`{member.id}`). Reason: {reason}", ephemeral=True)


@bot.tree.command(name="kick", description="Kick a member from the server (staff only).")
@app_commands.describe(member="Member to kick", reason="Reason")
@app_commands.checks.has_permissions(kick_members=True)
async def standard_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason supplied"):
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You can't kick yourself or an administrator.", ephemeral=True)
    await member.kick(reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"👢 Kicked {member} (`{member.id}`). Reason: {reason}", ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout a member (staff only).")
@app_commands.describe(member="Member", minutes="Duration in minutes", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def standard_timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason supplied"):
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You can't timeout yourself or an administrator.", ephemeral=True)
    await member.timeout(datetime.timedelta(minutes=minutes), reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"⏳ Timed out {member.mention} for {minutes} minutes. Reason: {reason}", ephemeral=True)


@bot.tree.command(name="untimeout", description="Remove a member's timeout (staff only).")
@app_commands.describe(member="Member to untimeout", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def standard_untimeout(interaction: discord.Interaction, member: discord.Member, reason: str = "Timeout lifted"):
    await member.timeout(None, reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"✅ Timeout removed for {member.mention}.", ephemeral=True)


@bot.tree.command(name="unban", description="Unban a user by Discord ID (staff only).")
@app_commands.describe(user_id="Numeric Discord user ID", reason="Reason")
@app_commands.checks.has_permissions(ban_members=True)
async def standard_unban(interaction: discord.Interaction, user_id: str, reason: str = "Appeal accepted"):
    if not user_id.isdigit():
        return await interaction.response.send_message("User ID must contain digits only.", ephemeral=True)
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=f"{reason} — by {interaction.user}")
    except discord.NotFound:
        return await interaction.response.send_message("That user isn't currently banned (or the ID doesn't exist).", ephemeral=True)
    await interaction.response.send_message(f"✅ Unbanned **{user}** (`{user.id}`). Reason: {reason}", ephemeral=True)


@bot.tree.command(name="purge", description="Delete up to 100 recent messages (staff only).")
@app_commands.describe(amount="Number of messages to delete", user="Optionally only delete one user's messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def standard_purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: discord.Member | None = None):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command requires a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    check = (lambda m: m.author.id == user.id) if user else (lambda m: True)
    deleted = await interaction.channel.purge(limit=amount, check=check, reason=f"Purge requested by {interaction.user}")
    await interaction.followup.send(f"🧹 Deleted **{len(deleted)}** messages.", ephemeral=True)


@bot.tree.command(name="slowmode", description="Set the current channel's slowmode (staff only).")
@app_commands.describe(seconds="Seconds between messages (0–21600)")
@app_commands.checks.has_permissions(manage_channels=True)
async def standard_slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command requires a text channel.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {interaction.user}")
    await interaction.response.send_message(f"🐢 Slowmode set to **{seconds} seconds**.", ephemeral=True)


@bot.tree.command(name="lock", description="Lock the current channel for @everyone (staff only).")
@app_commands.describe(reason="Reason for locking")
@app_commands.checks.has_permissions(manage_channels=True)
async def standard_lock(interaction: discord.Interaction, reason: str = "Staff lock"):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command requires a text channel.", ephemeral=True)
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"{reason} — by {interaction.user}")
    await interaction.response.send_message(f"🔒 Channel locked. Reason: {reason}", ephemeral=True)


@bot.tree.command(name="unlock", description="Unlock the current channel for @everyone (staff only).")
@app_commands.checks.has_permissions(manage_channels=True)
async def standard_unlock(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command requires a text channel.", ephemeral=True)
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
    await interaction.response.send_message("🔓 Channel unlocked.", ephemeral=True)


@bot.tree.command(name="purge_links", description="Delete recent messages containing links or Discord invites (staff only).")
@app_commands.describe(amount="Messages to scan (1–100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def standard_purge_links(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100] = 50):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command requires a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    pattern = re.compile(r"(?:https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)
    deleted = await interaction.channel.purge(limit=amount, check=lambda m: bool(pattern.search(m.content or "")), reason=f"Link purge by {interaction.user}")
    await interaction.followup.send(f"🔗 Deleted **{len(deleted)}** messages containing links or invites.", ephemeral=True)


@bot.tree.context_menu(name="Quote message")
async def quote_message_context(interaction: discord.Interaction, message: discord.Message):
    embed = _suite_embed("💬 Message quote", (message.content or "[No text content]")[:3000])
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    embed.add_field(name="Original channel", value=message.channel.mention, inline=True)
    embed.add_field(name="Sent", value=f"<t:{int(message.created_at.timestamp())}:F>", inline=True)
    embed.add_field(name="Jump to original", value=f"[Open message]({message.jump_url})", inline=False)
    if message.attachments:
        embed.add_field(name="Attachments", value="\n".join(a.url for a in message.attachments[:5])[:1000], inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="birthday_list", description="List birthdays registered in this server (staff only).")
@app_commands.checks.has_permissions(manage_guild=True)
async def community_birthday_list(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    rows = await db.fetchall("SELECT user_id,birthday FROM df_birthdays WHERE guild_id = ? ORDER BY substr(birthday,4,2), substr(birthday,1,2) LIMIT 50", (interaction.guild.id,))
    lines = []
    for row in rows:
        member = interaction.guild.get_member(int(row["user_id"]))
        lines.append(f"**{row['birthday']}** — {member.mention if member else f'<@{row['user_id']}>'}")
    await interaction.response.send_message(embed=_suite_embed("🎂 Registered birthdays", "\n".join(lines) or "No birthdays saved yet."), ephemeral=True)


@bot.tree.command(name="setup_status", description="Show Dark Fruits feature and channel configuration.")
@app_commands.checks.has_permissions(manage_guild=True)
async def mega_setup_status(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await _ensure_community_schema()
    row = await db.fetchone("SELECT * FROM df_guild_settings WHERE guild_id = ?", (guild.id,))
    def channel_label(key):
        value = int(row[key] or 0) if row else 0
        ch = guild.get_channel(value) if value else None
        return ch.mention if ch else "Not configured"
    embed = _suite_embed("⚙️ Dark Fruits • Setup status", "Current saved configuration for this server.")
    embed.add_field(name="Suggestions", value=channel_label("suggestion_channel_id"), inline=True)
    embed.add_field(name="Starboard", value=channel_label("starboard_channel_id"), inline=True)
    counting_row = await db.fetchone("SELECT channel_id FROM df_counting WHERE guild_id = ?", (guild.id,))
    counting_channel = guild.get_channel(int(counting_row["channel_id"])) if counting_row else None
    embed.add_field(name="Counting", value=counting_channel.mention if counting_channel else "Not configured", inline=True)
    embed.add_field(name="Media-only", value=channel_label("media_only_channel_id"), inline=True)
    embed.add_field(name="Moderation logs", value=channel_label("mod_log_channel_id"), inline=True)
    embed.add_field(name="Star threshold", value=f"`{int(row['starboard_threshold']) if row else 3}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ═══════════════════════════════════════════════════════════════════════════════
# CODE WATCHER + MONTHLY COMMUNITY LEADERBOARD
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_current_blox_codes() -> tuple[list[str], str]:
    """Fetch code listings from community pages; never invent or hard-code active codes."""
    sources = [
        ("Pocket Tactics", "https://www.pockettactics.com/blox-fruits/codes"),
        ("Blox Fruits Wiki", "https://blox-fruits.fandom.com/wiki/Codes"),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DarkFruitsBot/2.0; +https://discord.com)"}
    active: list[str] = []
    used_source = ""
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for source_name, url in sources:
            try:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        continue
                    html = await response.text(errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                # Prioritize explicit active/working sections when the page uses headings.
                sections = []
                for heading in soup.find_all(["h2", "h3"]):
                    heading_text = heading.get_text(" ", strip=True).lower()
                    if any(term in heading_text for term in ("active", "working", "currently working")) and not any(term in heading_text for term in ("expired", "inactive")):
                        sibling = heading.find_next(["table", "ul", "ol"])
                        if sibling:
                            sections.append(sibling)
                nodes = sections or soup.find_all(["table", "ul", "ol"])
                candidates = []
                for node in nodes:
                    for cell in node.find_all(["code", "td", "li"]):
                        text_value = cell.get_text(" ", strip=True)
                        match = re.search(r"\b([A-Z][A-Z0-9_]{2,30})\b", text_value)
                        if match:
                            code = match.group(1)
                            if code not in {"CODE", "CODES", "ACTIVE", "WORKING", "EXPIRED", "REWARD", "REWARDS", "BLOX", "FRUITS", "UPDATE"}:
                                candidates.append(code)
                candidates = list(dict.fromkeys(candidates))
                if candidates:
                    active = candidates[:40]
                    used_source = f"{source_name} — {url}"
                    break
            except Exception as exc:
                log.debug("Code source %s unavailable: %s", source_name, exc)
    return active, used_source


@bot.tree.command(name="codes", description="Fetch a current community list of Blox Fruits codes.")
async def mega_codes(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    codes, source = await fetch_current_blox_codes()
    if not codes:
        return await interaction.followup.send("⚠️ I couldn't verify a current code list right now. Check the linked sources directly: https://www.pockettactics.com/blox-fruits/codes and https://blox-fruits.fandom.com/wiki/Codes", ephemeral=True)
    embed = _suite_embed("🎟️ Blox Fruits • Code check", "These codes were extracted from a community-maintained page. Roblox may expire codes at any time; try them in-game and treat the source as unofficial.")
    embed.add_field(name=f"Codes found ({len(codes)})", value="\n".join(f"`{code}`" for code in codes[:30])[:1024], inline=False)
    embed.add_field(name="Source", value=source, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@tasks.loop(hours=6)
async def mega_codes_watcher():
    """Post an updated code list when its source-derived snapshot changes."""
    channel_id = int(os.environ.get("CODES_CHANNEL_ID", "1504914665589706762") or 0)
    if not channel_id:
        return
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS df_codes_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), codes TEXT NOT NULL, updated_at TEXT NOT NULL)")
        codes, source = await fetch_current_blox_codes()
        if not codes:
            return
        snapshot = json.dumps(codes, ensure_ascii=False)
        old = await db.fetchone("SELECT codes FROM df_codes_snapshot WHERE id = 1")
        if old and old["codes"] == snapshot:
            return
        await db.execute("INSERT INTO df_codes_snapshot (id,codes,updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET codes=excluded.codes,updated_at=excluded.updated_at", (snapshot, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = _suite_embed("🎟️ Blox Fruits • Code list updated", "Community-sourced code candidates. Codes can expire at any time; verify each in-game. No code is guaranteed active.")
        embed.add_field(name=f"Codes found ({len(codes)})", value="\n".join(f"`{code}`" for code in codes[:30])[:1024], inline=False)
        embed.add_field(name="Source", value=source or "Community code page", inline=False)
        await channel.send(embed=embed)
    except Exception:
        log.exception("Blox Fruits codes watcher failed")


@mega_codes_watcher.before_loop
async def before_mega_codes_watcher():
    await bot.wait_until_ready()


@bot.listen("on_ready")
async def start_mega_codes_watcher():
    if not mega_codes_watcher.is_running():
        mega_codes_watcher.start()


@tasks.loop(hours=1)
async def mega_monthly_leaderboard():
    """Post the Dark Coins monthly leaderboard once on the first day of each month."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.day != 1:
        return
    channel_id = int(os.environ.get("MONTHLY_LEADERBOARD_CHANNEL_ID", "1555694574452736041") or 0)
    if not channel_id:
        return
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS df_monthly_leaderboard_runs (month_key TEXT PRIMARY KEY, posted_at TEXT NOT NULL)")
        month_key = now.strftime("%Y-%m")
        if await db.fetchone("SELECT month_key FROM df_monthly_leaderboard_runs WHERE month_key = ?", (month_key,)):
            return
        rows = await db.fetchall("SELECT user_id,balance,tournaments_won,quests_completed FROM user_wallets ORDER BY balance DESC LIMIT 10")
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        lines = []
        for rank, row in enumerate(rows, 1):
            member = channel.guild.get_member(int(row["user_id"]))
            label = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"**{rank}.** {label} — **{int(row['balance']):,} DC**")
        embed = _suite_embed("🏆 Dark Fruits • Monthly Richest Pirates", f"Leaderboard snapshot for **{now.strftime('%B %Y')}**.\n\n" + ("\n".join(lines) or "No economy data yet."))
        await channel.send(embed=embed)
        await db.execute("INSERT INTO df_monthly_leaderboard_runs (month_key,posted_at) VALUES (?,?)", (month_key, now.isoformat()))
    except Exception:
        log.exception("Monthly leaderboard job failed")


@mega_monthly_leaderboard.before_loop
async def before_mega_monthly_leaderboard():
    await bot.wait_until_ready()


@bot.listen("on_ready")
async def start_mega_monthly_leaderboard():
    if not mega_monthly_leaderboard.is_running():
        mega_monthly_leaderboard.start()


# ═══════════════════════════════════════════════════════════════════════════════
# WEB + MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="Dark Fruits bot is online.")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server on port %s", port)


async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN missing — cannot start")
        return
    await start_web_server()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
    except Exception as e:
        log.critical("Startup fault: %s", e)
        raise
