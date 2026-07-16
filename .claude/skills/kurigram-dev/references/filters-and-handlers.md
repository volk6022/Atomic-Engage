# Kurigram Filters & Handlers Reference

## Handler Types

All handlers live in `pyrogram.handlers`. Register them via decorators or `app.add_handler()`.

| Handler | Decorator | Fires when |
|---|---|---|
| `MessageHandler` | `@app.on_message()` | New message arrives |
| `EditedMessageHandler` | `@app.on_edited_message()` | Message is edited |
| `DeletedMessagesHandler` | `@app.on_deleted_messages()` | Messages deleted |
| `CallbackQueryHandler` | `@app.on_callback_query()` | Inline button pressed |
| `InlineQueryHandler` | `@app.on_inline_query()` | Inline query typed |
| `ChosenInlineResultHandler` | `@app.on_chosen_inline_result()` | Inline result chosen |
| `ChatMemberUpdatedHandler` | `@app.on_chat_member_updated()` | Member status changes |
| `ChatJoinRequestHandler` | `@app.on_chat_join_request()` | Join request received |
| `PollHandler` | `@app.on_poll()` | Poll state changes |
| `UserStatusHandler` | `@app.on_user_status()` | User goes online/offline |
| `StoryHandler` | `@app.on_story()` | Story posted/updated |
| `MessageReactionHandler` | `@app.on_message_reaction()` | Reaction on message |
| `RawUpdateHandler` | `@app.on_raw_update()` | Raw MTProto update (advanced) |
| `StartHandler` | `@app.on_start()` | Client starts |
| `StopHandler` | `@app.on_stop()` | Client stops |
| `ConnectHandler` | `@app.on_connect()` | Connected to Telegram |
| `DisconnectHandler` | `@app.on_disconnect()` | Disconnected |
| `ErrorHandler` | `@app.on_error()` | Unhandled exception in handler |
| `BusinessMessageHandler` | `@app.on_business_message()` | Business bot message |
| `EditedBusinessMessageHandler` | `@app.on_edited_business_message()` | Business bot edited message |

### Registering Handlers

**Via decorator (most common):**
```python
@app.on_message(filters.private & filters.text)
async def handler(client, message):
    await message.reply("got it")
```

**Via `add_handler` (useful for dynamic registration):**
```python
from pyrogram.handlers import MessageHandler

async def handler(client, message):
    await message.reply("got it")

app.add_handler(MessageHandler(handler, filters.private))
```

**Group parameter** — handlers with lower group number run first. Default is 0.
```python
@app.on_message(filters.text, group=1)   # runs after group=0 handlers
```

---

## Built-in Filters (`pyrogram.filters`)

### Source / Direction
| Filter | Matches |
|---|---|
| `filters.all` | Every update |
| `filters.incoming` | Messages you received |
| `filters.outgoing` | Messages you sent |
| `filters.me` | Messages from yourself |

### Chat Type
| Filter | Matches |
|---|---|
| `filters.private` | Private chats and bot DMs |
| `filters.group` | Groups, supergroups, forums |
| `filters.channel` | Channels |
| `filters.forum` | Forum supergroups specifically |
| `filters.direct` | Telegram Direct |

### Sender
| Filter | Matches |
|---|---|
| `filters.bot` | Messages from bots |
| `filters.sender_chat` | Messages sent as a channel/group |
| `filters.via_bot` | Messages sent via inline bot |
| `filters.mentioned` | Messages mentioning you |
| `filters.admin` | Chats where you're admin |

### Content Type
| Filter | Matches |
|---|---|
| `filters.text` | Text messages |
| `filters.media` | Any media |
| `filters.photo` | Photos |
| `filters.video` | Videos |
| `filters.audio` | Audio files |
| `filters.voice` | Voice messages |
| `filters.video_note` | Video notes (circles) |
| `filters.document` | Documents / files |
| `filters.sticker` | Stickers |
| `filters.animation` | GIFs / animations |
| `filters.contact` | Contact cards |
| `filters.location` | Static locations |
| `filters.live_location` | Live locations |
| `filters.venue` | Venues |
| `filters.poll` | Polls |
| `filters.dice` | Dice / game emojis |
| `filters.game` | Games |
| `filters.caption` | Media with captions |
| `filters.web_page` | Messages with link previews |
| `filters.reply` | Replies to other messages |
| `filters.forwarded` | Forwarded messages |
| `filters.quote` | Quote messages |
| `filters.media_group` | Messages in a media album |
| `filters.media_spoiler` | Media with spoiler tag |
| `filters.story` | Story share messages |

### Service Messages
| Filter | Matches |
|---|---|
| `filters.service` | Any service message |
| `filters.new_chat_members` | Join service message |
| `filters.left_chat_member` | Leave service message |
| `filters.pinned_message` | Pin service message |
| `filters.new_chat_title` | Title change |
| `filters.new_chat_photo` | Photo change |
| `filters.video_chat_started` | Voice/video chat started |
| `filters.video_chat_ended` | Voice/video chat ended |
| `filters.successful_payment` | Payment completed |

### Scheduling / Business
| Filter | Matches |
|---|---|
| `filters.scheduled` | Scheduled (not yet sent) messages |
| `filters.from_scheduled` | Auto-sent scheduled messages |
| `filters.business` | Messages via business bot |
| `filters.paid_message` | Paid messages |

### Gifts
| Filter | Matches |
|---|---|
| `filters.gift` | Gift messages |
| `filters.gift_code` | Premium gift code messages |
| `filters.gift_offer` | Pending gift offers |
| `filters.gift_offer_accepted` | Accepted gift offers |
| `filters.gift_offer_rejected` | Rejected gift offers |
| `filters.giveaway` | Giveaway messages |
| `filters.giveaway_winners` | Giveaway winner announcements |

---

## Parameterized Filters

### `filters.command(commands, prefixes="/", case_sensitive=False)`
Matches text messages that start with a command. Sets `message.command` as a list: `[cmd, arg1, arg2, ...]`.

```python
# Single command
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply(f"Hello {message.from_user.first_name}!")

# Multiple commands
@app.on_message(filters.command(["help", "info"]))
async def help_cmd(client, message):
    pass

# Custom prefix (e.g. for userbots)
@app.on_message(filters.command("warn", prefixes=["!", "."]))
async def warn(client, message):
    pass

# Access arguments
@app.on_message(filters.command("ban"))
async def ban(client, message):
    # /ban @username reason here
    cmd, username, *reason = message.command
    reason = " ".join(reason)
```

### `filters.regex(pattern, flags=0)`
Matches text/caption against a regex. On match, stores all `re.Match` objects in `update.matches`.
Works with: `Message`, `CallbackQuery`, `InlineQuery`, `ChosenInlineResult`, `PreCheckoutQuery`.

```python
@app.on_message(filters.regex(r"hello|hi", re.IGNORECASE))
async def greet(client, message):
    match = message.matches[0]
    await message.reply(f"You said: {match.group()}")

@app.on_callback_query(filters.regex(r"^action:(\w+)$"))
async def handle_action(client, callback_query):
    action = callback_query.matches[0].group(1)
    await callback_query.answer(f"Action: {action}")
```

### `filters.user(users)`
Matches messages from specific users.

```python
ADMIN_IDS = [123456789, "someusername"]
@app.on_message(filters.user(ADMIN_IDS) & filters.command("restart"))
async def admin_restart(client, message):
    pass
```

### `filters.chat(chats)`
Matches messages from specific chats.

```python
@app.on_message(filters.chat([-100123456789, "mychannel"]) & filters.text)
async def in_specific_chat(client, message):
    pass
```

### `filters.topic(topics)`
Matches messages from specific forum topics.

---

## Filter Composition

Filters compose with Python operators:

```python
# AND — both must match
filters.private & filters.text

# OR — either must match
filters.photo | filters.video

# NOT — must not match
~filters.bot

# Combining
filters.group & (filters.photo | filters.video) & ~filters.bot
```

---

## Custom Filters

Use `filters.create()` to make a reusable filter:

```python
from pyrogram import filters

# Simple custom filter
async def is_premium_func(_, __, message):
    return message.from_user and message.from_user.is_premium

is_premium = filters.create(is_premium_func, "IsPremiumFilter")

@app.on_message(is_premium & filters.private)
async def premium_only(client, message):
    await message.reply("Premium perks!")
```

**Parameterized custom filter:**
```python
def keyword_filter(keywords):
    async def func(flt, _, message):
        text = (message.text or message.caption or "").lower()
        return any(kw in text for kw in flt.keywords)
    return filters.create(func, "KeywordFilter", keywords=[k.lower() for k in keywords])

@app.on_message(keyword_filter(["order", "buy", "purchase"]))
async def purchase_intent(client, message):
    pass
```

**Async filter with DB lookup:**
```python
async def is_banned_func(_, client, message):
    # can call async functions — DB, cache, etc.
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return False
    return await db.is_banned(user_id)

is_banned = filters.create(is_banned_func, "IsBannedFilter")

@app.on_message(~is_banned)   # allow only non-banned users
async def handler(client, message):
    pass
```
