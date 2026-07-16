# Kurigram Error Handling Reference

All kurigram errors live in `pyrogram.errors`. Import them explicitly for catching.

---

## The Most Important: FloodWait

`FloodWait` is by far the most common error. It means Telegram is rate-limiting you.

```python
from pyrogram.errors import FloodWait
import asyncio

try:
    await client.send_message(chat_id, text)
except FloodWait as e:
    # e.value is the number of seconds to wait
    await asyncio.sleep(e.value)
    await client.send_message(chat_id, text)  # retry
```

The client has a built-in `sleep_threshold` (default 10s). Any `FloodWait` below this value is **automatically retried** — you only see FloodWaits that exceed the threshold.

To raise it (e.g. tolerate up to 60s auto-sleeps):
```python
app = Client("name", ..., sleep_threshold=60)
```

Related flood errors:
- `FloodPremiumWait` — rate-limited until buying Telegram Premium
- `SlowmodeWait` — group slow mode active, `e.value` = seconds remaining

---

## Error Hierarchy

```
Exception
└── RPCError                     # all Telegram API errors
    ├── BadRequest (400)          # invalid input / wrong parameters
    ├── Unauthorized (401)        # not logged in / session invalid
    ├── Forbidden (403)           # no permission
    ├── NotFound (404)            # resource not found
    ├── Flood (420)               # rate limiting
    │   ├── FloodWait             # wait e.value seconds
    │   ├── FloodPremiumWait      # need Premium or wait
    │   └── SlowmodeWait          # group slow mode
    ├── InternalServerError (500) # Telegram server error
    └── ServiceUnavailable (503)  # Telegram service down
```

You can catch at any level:
```python
from pyrogram.errors import RPCError, BadRequest, Forbidden

try:
    await client.ban_chat_member(chat_id, user_id)
except Forbidden:
    print("No admin rights")
except BadRequest as e:
    print(f"Bad input: {e}")
except RPCError as e:
    print(f"Telegram error: {e}")
```

---

## Common Errors by Category

### 400 Bad Request — invalid input

| Error | Cause |
|---|---|
| `PeerIdInvalid` | chat_id / user_id doesn't exist or not in contacts |
| `UserNotParticipant` | Target user is not in the group |
| `MessageNotModified` | Editing a message with the same content |
| `MessageIdInvalid` | Message ID doesn't exist |
| `MessageEmpty` | Tried to send empty text |
| `ChatNotModified` | No change was made (title, description already same) |
| `UsernameNotOccupied` | Username doesn't exist |
| `UsernameInvalid` | Username format is wrong |
| `PhotoInvalidDimensions` | Photo too small or wrong size |
| `FilePartMissing` | Upload failed partway through |
| `ApiIdInvalid` | Wrong api_id / api_hash |
| `AboutTooLong` | Bio text over limit |

### 401 Unauthorized — session issues

| Error | Cause |
|---|---|
| `AuthKeyInvalid` | Session file is corrupt — delete `.session` and re-login |
| `AuthKeyUnregistered` | Session expired — delete `.session` and re-login |
| `SessionExpired` | Same as above |
| `SessionRevoked` | User terminated all sessions |
| `UserDeactivated` | Account banned/deleted |
| `UserDeactivatedBan` | Account banned by Telegram's anti-spam |
| `SessionPasswordNeeded` | 2FA is enabled — provide password |

### 403 Forbidden — permission denied

| Error | Cause |
|---|---|
| `ChatAdminRequired` | You need to be admin for this action |
| `ChatWriteForbidden` | You can't send messages here |
| `UserIsBlocked` | The user has blocked you |
| `UserPrivacyRestricted` | User's privacy settings block this action |
| `RightForbidden` | Trying to set admin rights you don't have |
| `ChatSendMediaForbidden` | Media sending disabled in chat |
| `ChatSendStickersForbidden` | Stickers disabled in chat |
| `VoiceMessagesForbidden` | User disabled voice messages from you |
| `MessageDeleteForbidden` | Can't delete this message (not author, or service msg) |
| `MessageAuthorRequired` | Not the message author |
| `PremiumAccountRequired` | Action requires Telegram Premium |
| `PrivacyPremiumRequired` | User restricted non-Premium users from messaging |

### 420 Flood — rate limiting

| Error | Attribute | Cause |
|---|---|---|
| `FloodWait` | `e.value` = seconds to wait | General rate limit |
| `FloodPremiumWait` | `e.value` = seconds | Rate limit, removable with Premium |
| `SlowmodeWait` | `e.value` = seconds | Group slow mode, wait before sending again |

### 500 / 503 — Telegram server errors

These are transient. Retry with exponential backoff:
```python
import asyncio
from pyrogram.errors import InternalServerError, ServiceUnavailable

for attempt in range(3):
    try:
        result = await client.get_chat(chat_id)
        break
    except (InternalServerError, ServiceUnavailable):
        await asyncio.sleep(2 ** attempt)
```

---

## Catching Specific Errors

```python
from pyrogram.errors import (
    FloodWait,
    UserIsBlocked,
    PeerIdInvalid,
    ChatWriteForbidden,
    UserPrivacyRestricted,
    MessageNotModified,
)

async def send_to_user(client, user_id, text):
    try:
        await client.send_message(user_id, text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await client.send_message(user_id, text)
    except UserIsBlocked:
        print(f"User {user_id} blocked us")
    except (PeerIdInvalid, UserPrivacyRestricted, ChatWriteForbidden) as e:
        print(f"Can't message {user_id}: {e}")
```

---

## Handling Errors in Handlers (ErrorHandler)

You can register a global error handler to catch unhandled exceptions from other handlers:

```python
@app.on_error()
async def on_error(client, update, exception):
    print(f"Error in handler: {type(exception).__name__}: {exception}")
    # update can be a Message, CallbackQuery, etc. depending on which handler raised
```

---

## Tips

- Always catch `FloodWait` when doing bulk operations (broadcasting, mass actions).
- `PeerIdInvalid` often means you haven't interacted with that user/chat yet — you need to resolve them first via search, join, or they need to message you.
- On `AuthKeyUnregistered` / `SessionExpired`, the right fix is deleting the `.session` file and re-authenticating. Never try to recover from these in code.
- `MessageNotModified` is harmless — you can safely swallow it when editing messages in loops.
- For userbot anti-ban hygiene: add random delays between bulk actions, use `sleep_threshold`, and avoid suspicious patterns (mass messaging strangers, etc.).
