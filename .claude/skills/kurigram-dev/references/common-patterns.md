# Kurigram Common Patterns

---

## 1. Minimal Bot (echo bot)

```python
from pyrogram import Client, filters

app = Client(
    "my_bot",
    api_id=12345,
    api_hash="0123456789abcdef0123456789abcdef",
    bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
)

@app.on_message(filters.private & filters.text)
async def echo(client, message):
    await message.reply(message.text)

app.run()
```

---

## 2. Userbot (user account)

```python
from pyrogram import Client

app = Client(
    "my_account",           # session file name (.session)
    api_id=12345,
    api_hash="...",
    # no bot_token → user account
)

app.run()
```

Config via `config.ini` (recommended for credentials):
```ini
[pyrogram]
api_id = 12345
api_hash = 0123456789abcdef0123456789abcdef
```
Then just `Client("my_account")` with no api_id/api_hash args.

---

## 3. Command Handler with Arguments

```python
@app.on_message(filters.command("ban") & filters.group)
async def ban_user(client, message):
    # /ban @username 24h reason here
    if len(message.command) < 2:
        return await message.reply("Usage: /ban @username [reason]")
    
    target = message.command[1]       # @username or user_id
    reason = " ".join(message.command[2:]) if len(message.command) > 2 else "No reason"
    
    try:
        await client.ban_chat_member(message.chat.id, target)
        await message.reply(f"Banned {target}. Reason: {reason}")
    except Exception as e:
        await message.reply(f"Failed: {e}")
```

---

## 4. Inline Keyboard + Callback Handler

```python
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

@app.on_message(filters.command("menu"))
async def menu(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Option A", callback_data="option_a")],
        [InlineKeyboardButton("Option B", callback_data="option_b")],
    ])
    await message.reply("Choose an option:", reply_markup=keyboard)

@app.on_callback_query(filters.regex(r"^option_"))
async def handle_option(client, callback_query):
    data = callback_query.data   # "option_a" or "option_b"
    await callback_query.answer(f"You chose {data}!", show_alert=False)
    await callback_query.message.edit_text(f"Selected: **{data}**")
```

---

## 5. Inline Query Handler

```python
from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent

@app.on_inline_query()
async def inline_handler(client, inline_query):
    query = inline_query.query.strip()
    
    results = [
        InlineQueryResultArticle(
            title=f"Result for: {query}",
            input_message_content=InputTextMessageContent(
                message_text=f"You searched: **{query}**"
            ),
            description="Tap to send",
        )
    ]
    await inline_query.answer(results, cache_time=10)
```

---

## 6. FloodWait Handling

```python
import asyncio
from pyrogram.errors import FloodWait

async def safe_send(client, chat_id, text):
    while True:
        try:
            return await client.send_message(chat_id, text)
        except FloodWait as e:
            print(f"FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
```

For batch sending with delay:
```python
import asyncio
from pyrogram.errors import FloodWait

chats = [-100111, -100222, -100333]

for chat_id in chats:
    try:
        await app.send_message(chat_id, "Broadcast message")
        await asyncio.sleep(0.5)   # polite delay
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception as e:
        print(f"Failed for {chat_id}: {e}")
```

The client has a built-in `sleep_threshold` (default: 10s). FloodWaits below this value are handled automatically.

---

## 7. Iterating Chat History

```python
# Async generator — memory efficient
async for message in app.get_chat_history("somechat", limit=100):
    if message.text:
        print(message.text)

# Collect into list (careful with large chats!)
messages = [m async for m in app.get_chat_history("somechat", limit=200)]
```

Paginating all history:
```python
count = 0
async for message in app.get_chat_history(chat_id):
    count += 1
    # process message
print(f"Total processed: {count}")
```

---

## 8. Iterating Chat Members

```python
# All members
async for member in app.get_chat_members(chat_id):
    user = member.user
    print(f"{user.id} | {user.first_name} | @{user.username}")

# Admins only
from pyrogram.enums import ChatMembersFilter
async for admin in app.get_chat_members(chat_id, filter=ChatMembersFilter.ADMINISTRATORS):
    print(admin.user.first_name)
```

---

## 9. Downloading Media

```python
@app.on_message(filters.photo | filters.document)
async def download_file(client, message):
    # Download to default dir (current working directory)
    path = await message.download()
    
    # Download to specific path
    path = await message.download(file_name="/tmp/myfile.jpg")
    
    # With progress callback
    async def progress(current, total):
        print(f"{current * 100 / total:.1f}%")
    
    path = await message.download(progress=progress)
    await message.reply(f"Downloaded to: {path}")
```

---

## 10. Sending Media

```python
# Photo — from file path, URL, or file_id
await app.send_photo(chat_id, "photo.jpg", caption="**Bold caption**")
await app.send_photo(chat_id, "https://example.com/photo.jpg")

# With progress
async def progress(current, total):
    print(f"Uploading: {current * 100 / total:.1f}%")

await app.send_document(chat_id, "bigfile.zip", progress=progress)

# Media group (album)
from pyrogram.types import InputMediaPhoto, InputMediaVideo
await app.send_media_group(chat_id, [
    InputMediaPhoto("photo1.jpg"),
    InputMediaPhoto("photo2.jpg", caption="Last photo"),
])
```

---

## 11. Multiple Clients (compose)

```python
from pyrogram import compose

app1 = Client("account1", api_id=..., api_hash=...)
app2 = Client("account2", api_id=..., api_hash=...)

@app1.on_message(filters.private)
async def handle1(client, message):
    await message.reply("From account 1")

@app2.on_message(filters.private)
async def handle2(client, message):
    await message.reply("From account 2")

compose([app1, app2])
```

---

## 12. Plugin System (organize handlers in files)

Project structure:
```
mybot/
├── main.py
└── plugins/
    ├── start.py
    ├── admin.py
    └── helpers.py
```

`main.py`:
```python
app = Client("mybot", ..., plugins=dict(root="plugins"))
app.run()
```

`plugins/start.py` (handlers auto-registered):
```python
from pyrogram import Client, filters

@Client.on_message(filters.command("start"))
async def start(client, message):
    await message.reply("Hello!")
```

---

## 13. Using `async with` (context manager)

```python
async def main():
    async with Client("my_account", api_id=..., api_hash=...) as app:
        # Client is started and available here
        me = await app.get_me()
        print(f"Logged in as {me.first_name}")
        
        await app.send_message("me", "Hello from script!")
        # Iterating works too
        async for dialog in app.get_dialogs(limit=5):
            print(dialog.chat.title)
    # Client automatically stopped here

import asyncio
asyncio.run(main())
```

---

## 14. State / Conversation Management

Kurigram doesn't include a built-in FSM, but a simple dict works for small bots:

```python
waiting_for = {}  # user_id -> "step_name"

@app.on_message(filters.command("setname"))
async def ask_name(client, message):
    waiting_for[message.from_user.id] = "name"
    await message.reply("What's your name?")

@app.on_message(filters.private & filters.text & ~filters.command([]))
async def handle_text(client, message):
    uid = message.from_user.id
    state = waiting_for.get(uid)
    
    if state == "name":
        del waiting_for[uid]
        await message.reply(f"Nice to meet you, {message.text}!")
```

For production, use libraries like `pyrogram-fsm`, or implement Redis/DB-backed state.

---

## 15. Contributing to Kurigram: Adding a New Method

Methods live in `pyrogram/methods/<category>/`. Each is a standalone class with one method.

**Example — adding `get_user_bio`:**

1. Create `pyrogram/methods/users/get_user_bio.py`:
```python
from typing import Union
import pyrogram
from pyrogram import raw

class GetUserBio:
    async def get_user_bio(
        self: "pyrogram.Client",
        user_id: Union[int, str],
    ) -> str:
        """Get the bio/about text of a user.
        
        Parameters:
            user_id: Target user ID or username.
            
        Returns:
            The user's bio string, or empty string if none.
        """
        peer = await self.resolve_peer(user_id)
        r = await self.invoke(raw.functions.users.GetFullUser(id=peer))
        return r.full_user.about or ""
```

2. Add to `pyrogram/methods/users/__init__.py`:
```python
from .get_user_bio import GetUserBio
```

3. Add to the `Users` mixin class in the same file:
```python
class Users(
    ...,
    GetUserBio,
):
    pass
```

That's it — the method is now available as `client.get_user_bio(user_id)`.

---

## 16. Adding a New Type

Types live in `pyrogram/types/<category>/`. Inherit from `Object`.

```python
from pyrogram import types

class MyNewType(types.Object):
    def __init__(
        self,
        *,
        client: "pyrogram.Client" = None,
        field_one: str,
        field_two: int = 0,
    ):
        super().__init__(client)
        self.field_one = field_one
        self.field_two = field_two

    @staticmethod
    def _parse(client, raw_obj) -> "MyNewType":
        return MyNewType(
            client=client,
            field_one=raw_obj.some_field,
            field_two=getattr(raw_obj, "other_field", 0),
        )
```

Then export it from the category's `__init__.py` and from `pyrogram/types/__init__.py`.
