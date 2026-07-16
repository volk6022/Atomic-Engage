---
name: kurigram-dev
description: >
  Use this skill whenever someone is developing with kurigram or Pyrogram — the Python Telegram MTProto API framework.
  Trigger for any task involving: writing Telegram bots or userbots in Python, handling messages/callbacks/inline queries,
  composing filters, downloading/sending media, iterating chat history or members, managing chats, dealing with FloodWait
  or other Telegram API errors, or modifying the kurigram library itself (adding methods, types, handlers).
  Also trigger when someone pastes kurigram/pyrogram code and asks to fix, explain, or extend it — even if they don't
  say "kurigram" explicitly. If it imports from `pyrogram`, this skill applies.
---

# Kurigram Development

Kurigram is a fork of Pyrogram — an async Python framework for the Telegram MTProto API.
It works for both **bot accounts** (bot_token) and **user accounts** (phone login).
All code imports from the `pyrogram` namespace: `from pyrogram import Client, filters`.

The source lives in `pyrogram/` inside the repo:
- `pyrogram/client.py` — the `Client` class and all its init params
- `pyrogram/methods/` — all API methods, grouped by category
- `pyrogram/handlers/` — all handler classes
- `pyrogram/filters.py` — built-in filters + `create()` for custom ones
- `pyrogram/types/` — all type definitions (Message, User, Chat, etc.)
- `pyrogram/errors/` — error hierarchy

---

## Quick-Start Checklist

When helping someone build with kurigram, always confirm:

1. **Account type** — bot (`bot_token`) or userbot (phone login)?
2. **Session storage** — file-based (default), in-memory (`in_memory=True`), or external DB?
3. **Credentials** — `api_id` + `api_hash` from https://my.telegram.org (required for both bots and userbots)
4. **Run style** — long-running daemon (`app.run()`), script (`async with Client(...) as app`), or multi-client (`compose()`)?

---

## Client Setup

```python
from pyrogram import Client

# Bot account
app = Client(
    "my_bot",
    api_id=12345,
    api_hash="0123456789abcdef0123456789abcdef",
    bot_token="123456:ABC-TOKEN",
)

# User account
app = Client(
    "my_account",   # session file name
    api_id=12345,
    api_hash="...",
)
```

**Key Client parameters:**
- `name` — session identifier (file saved as `<name>.session`)
- `bot_token` — omit for user accounts
- `plugins=dict(root="plugins")` — enable Smart Plugins (auto-load handlers from a folder)
- `sleep_threshold=10` — auto-sleep FloodWaits up to this many seconds
- `no_updates=True` — disable update receiving (for batch scripts)
- `in_memory=True` — don't write a session file (use `export_session_string()` to persist)
- `proxy=dict(scheme="socks5", hostname="...", port=1080)` — proxy support
- `workdir="./sessions"` — where to store session files
- `workers=N` — concurrent update worker threads

---

## Decorator Pattern (most common)

```python
from pyrogram import Client, filters

app = Client("my_bot", api_id=..., api_hash=..., bot_token="...")

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply(f"Hello, {message.from_user.first_name}!")

@app.on_callback_query(filters.regex(r"^btn:"))
async def on_button(client, callback_query):
    await callback_query.answer("Got it!")

app.run()
```

Handler functions always receive `(client, update)` — where `update` type matches the handler
(Message, CallbackQuery, InlineQuery, etc.).

---

## Filter Composition

```python
# AND
filters.private & filters.text

# OR
filters.photo | filters.video

# NOT
~filters.bot

# Combined
filters.group & (filters.photo | filters.video) & ~filters.bot
```

For the full list of built-in filters and custom filter creation, read:
→ `references/filters-and-handlers.md`

---

## Sending Messages

```python
# Text (supports HTML and Markdown by default)
await client.send_message(chat_id, "Hello **world**!")

# Reply to a specific message
await message.reply("Direct reply")
await client.send_message(chat_id, "text", reply_to_message_id=msg_id)

# With inline keyboard
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
markup = InlineKeyboardMarkup([[InlineKeyboardButton("Click", callback_data="data")]])
await client.send_message(chat_id, "Pick one:", reply_markup=markup)

# Edit a message
await client.edit_message_text(chat_id, message_id, "New text")
# or via the message object:
await message.edit("New text")

# Delete
await client.delete_messages(chat_id, [message_id])
```

Parse modes: `enums.ParseMode.HTML`, `enums.ParseMode.MARKDOWN`, `enums.ParseMode.DISABLED`.
Set globally: `Client(..., parse_mode=enums.ParseMode.HTML)`.

---

## Media

```python
# Send from file path, URL, or file_id
await client.send_photo(chat_id, "photo.jpg", caption="A caption")
await client.send_document(chat_id, "report.pdf")
await client.send_video(chat_id, "clip.mp4")

# Download received media
path = await message.download()                      # to current dir
path = await message.download(file_name="/tmp/f.jpg")

# Album
from pyrogram.types import InputMediaPhoto
await client.send_media_group(chat_id, [InputMediaPhoto("a.jpg"), InputMediaPhoto("b.jpg")])
```

---

## FloodWait — the most important error to handle

```python
import asyncio
from pyrogram.errors import FloodWait

try:
    await client.send_message(chat_id, text)
except FloodWait as e:
    await asyncio.sleep(e.value)
    await client.send_message(chat_id, text)  # retry once
```

Always add `FloodWait` handling for any bulk/looped operation. The client auto-handles
FloodWaits shorter than `sleep_threshold` (default 10s) — you only see ones that exceed it.

For the full error hierarchy and all common errors, read:
→ `references/errors.md`

---

## Iterating (Async Generators)

```python
# Chat history
async for message in client.get_chat_history(chat_id, limit=100):
    print(message.text)

# Members
async for member in client.get_chat_members(chat_id):
    print(member.user.username)

# Search
async for msg in client.search_messages(chat_id, "keyword"):
    print(msg.id, msg.text)
```

These are async generators — use `async for`. Never collect them all into a list unless
you know the result set is bounded.

---

## Multi-client / Idle

```python
# Single client long-running
app.run()   # blocks until Ctrl+C

# Multiple clients
from pyrogram import compose
compose([app1, app2])

# Manual async style
import asyncio

async def main():
    async with Client("name", ...) as app:
        await app.send_message("me", "ready")
        await pyrogram.idle()

asyncio.run(main())
```

---

## Plugin System

For larger bots, use Smart Plugins to split handlers across files:

```
mybot/
├── main.py                    ← Client(..., plugins=dict(root="plugins"))
└── plugins/
    ├── start.py               ← @Client.on_message(...)
    ├── admin.py
    └── utils.py
```

Handlers in plugin files use `@Client.on_message` (class-level) instead of `@app.on_message`.

---

## Contributing to Kurigram: Adding a Method

Each method is its own class file in `pyrogram/methods/<category>/`:

1. Create `pyrogram/methods/<category>/my_method.py` with a class containing the async method
2. Add `from .my_method import MyMethod` to that category's `__init__.py`
3. Add `MyMethod` to the mixin class in the same `__init__.py`

The method is then available on every `Client` instance automatically. Use `self.invoke(raw.functions....)` to call raw MTProto functions, and `self.resolve_peer(id)` to convert IDs to InputPeer.

For full examples of adding methods and types, read:
→ `references/common-patterns.md` (sections 15–16)

---

## Reference Files

Load these when you need more detail:

| File | When to read |
|---|---|
| `references/filters-and-handlers.md` | Complete filter list, handler types, custom filter examples |
| `references/api-methods.md` | All methods grouped by category (messages, chats, users, bots, etc.) |
| `references/common-patterns.md` | Ready-to-copy code for the most common patterns |
| `references/errors.md` | Full error hierarchy, common errors by code, handling patterns |
