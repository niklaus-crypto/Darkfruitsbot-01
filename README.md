# Dark Fruits Mega Bot

This is an expansion of the longer Gemini-only Dark Fruits bot, not a replacement written from scratch. Existing Blox Fruits knowledge retrieval, trade-value scraping, Normal/Mirage stock tracking, economy, inventory, quests, temporary roles, sticky messages, channel command restrictions, store, persistent giveaways, and original server IDs are retained. New command systems are appended around the original implementation.

## What's inside

- `bot.py` — main bot (5,000+ lines; 107 slash commands plus a message context menu).
- `requirements.txt` — runtime dependencies; no Groq dependency or fallback is used.
- `bloxfruits_knowledge.db`, `bloxfruits_seed.json`, `seed_bloxfruits_database.py`, `bloxfruits_knowledge_sync.py` — Blox Fruits knowledge data and refresh helpers.
- `render.yaml` — Render deployment template.
- `.env.example` — environment-variable template. Never commit real tokens or API keys.

## Features

- **Original systems:** Gemini-only Blox Fruits assistant with bounded waits and knowledge retrieval, value scraper for `bloxfruitsvalues.com/values`, stock scraper and alerts, daily quests, Dark Coins, inventory, store, role rewards, temporary roles, sticky messages, command restrictions, and persistent giveaway management.
- **Moderation:** ban, kick, timeout/untimeout, unban, warn/warnings/clear warnings, purge, link purge, slowmode, lock/unlock, role/nickname tools, and audit logs.
- **Community:** XP/ranks/leaderboards, level role rewards, AFK, reminders, birthdays, suggestions and review status, vouches, tags, starboard, counting game, tickets with transcripts, polls, quote context menu, and monthly leaderboard.
- **Utilities and fun:** help directory, bot/server/member/role/channel info, avatar/banner, invite creation, calculator, dice, choose, 8-ball, ship score, compliment, roast, reverse text, emojis and roles lists.
- **AutoMod:** media-only channel enforcement, link/invite blocking and media-channel restrictions with staff bypass. This is deliberately strict by default; see below.

## Important setup notes

1. Upload the contents of this folder to the **root** of the GitHub repository used by Render.
2. In Render, set `DISCORD_TOKEN` and `GEMINI_API_KEY` as secrets. Do not put real secrets in GitHub.
3. Confirm `GEMINI_MODEL` is a model ID enabled for your Google AI Studio key. If the API reports a model-not-found error, change that variable to a currently supported model.
4. Enable **Message Content Intent** and **Server Members Intent** in the Discord Developer Portal. Invite the bot with `bot` and `applications.commands` scopes.
5. Give the bot the permissions it needs for the enabled features: View Channels, Send Messages, Embed Links, Read Message History, Manage Messages, Manage Channels, Manage Roles, Moderate Members, Ban Members, Kick Members, Add Reactions, and Attach Files. Keep the bot's highest role above roles it must assign.
6. Check the IDs in `render.yaml` / `.env.example` before deployment. Set ticket category/log IDs if you use tickets.
7. The persistent disk paths in `render.yaml` require a Render service plan that supports a disk. Without persistence, local SQLite state may be lost on restart.

## AutoMod warning

By default, non-staff messages containing links/invites or media outside the approved media channel are deleted. The configured media-only channel accepts image/video attachments only—no captions, links, or GIFs. Staff with Manage Messages or Administrator bypass these rules. If this is too strict for your server, set `AUTOMOD_BLOCK_LINKS=false` and/or `AUTOMOD_BLOCK_MEDIA_OUTSIDE_CHANNEL=false` in Render. Review the channel IDs carefully before enabling it in your live server.

## Validation status

The Python file passes `python -m py_compile`, and the slash-command registrations were checked for duplicate names. A live Discord runtime test could not be completed in the file-building environment because dependencies could not be downloaded there. Test in a private Discord server before replacing the production bot.

No bot can realistically include every command ever created for Discord. This build provides a broad integrated suite; additional third-party systems still require their own APIs, permissions, and testing.
