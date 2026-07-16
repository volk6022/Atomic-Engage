# Kurigram API Methods Reference

All methods are `async` and called on the `Client` instance (or `self` inside a handler).

---

## Messages

### Sending
| Method | Description |
|---|---|
| `send_message(chat_id, text, ...)` | Send a text message |
| `send_photo(chat_id, photo, ...)` | Send a photo |
| `send_video(chat_id, video, ...)` | Send a video |
| `send_audio(chat_id, audio, ...)` | Send an audio file |
| `send_document(chat_id, document, ...)` | Send a document/file |
| `send_animation(chat_id, animation, ...)` | Send a GIF/animation |
| `send_voice(chat_id, voice, ...)` | Send a voice message |
| `send_video_note(chat_id, video_note, ...)` | Send a video note (circle) |
| `send_sticker(chat_id, sticker, ...)` | Send a sticker |
| `send_location(chat_id, latitude, longitude, ...)` | Send a location |
| `send_venue(chat_id, latitude, longitude, title, address, ...)` | Send a venue |
| `send_contact(chat_id, phone_number, first_name, ...)` | Send a contact |
| `send_poll(chat_id, question, options, ...)` | Send a poll |
| `send_dice(chat_id, emoji, ...)` | Send a dice |
| `send_media_group(chat_id, media, ...)` | Send multiple media as album |
| `send_cached_media(chat_id, file_id, ...)` | Resend by file_id |
| `send_chat_action(chat_id, action, ...)` | Show "typing…", "sending photo…", etc. |
| `send_checklist(chat_id, title, tasks, ...)` | Send a checklist message |

Common parameters for send methods:
- `reply_to_message_id` / `reply_parameters` — reply to a message
- `parse_mode` — `enums.ParseMode.HTML`, `MARKDOWN`, `DISABLED`
- `reply_markup` — `InlineKeyboardMarkup`, `ReplyKeyboardMarkup`, `ReplyKeyboardRemove`, `ForceReply`
- `disable_notification` — silent send
- `schedule_date` — send at a future time
- `protect_content` — prevent forwarding/saving
- `message_thread_id` — send in a forum topic

### Editing
| Method | Description |
|---|---|
| `edit_message_text(chat_id, message_id, text, ...)` | Edit text |
| `edit_message_caption(chat_id, message_id, caption, ...)` | Edit caption |
| `edit_message_media(chat_id, message_id, media, ...)` | Replace media |
| `edit_message_reply_markup(chat_id, message_id, reply_markup)` | Edit inline keyboard |
| `edit_inline_text(inline_message_id, text, ...)` | Edit inline message text |
| `edit_inline_caption(inline_message_id, caption, ...)` | Edit inline message caption |
| `edit_inline_reply_markup(inline_message_id, reply_markup)` | Edit inline message keyboard |

### Retrieving
| Method | Description |
|---|---|
| `get_messages(chat_id, message_ids)` | Get one or more messages by ID |
| `get_chat_history(chat_id, limit, offset, ...)` | Async generator of messages |
| `get_chat_history_count(chat_id)` | Total message count |
| `get_media_group(chat_id, message_id)` | Get all messages in an album |
| `get_discussion_message(chat_id, message_id)` | Get linked discussion message |
| `get_discussion_replies(chat_id, message_id, ...)` | Async generator of discussion replies |
| `get_scheduled_messages(chat_id)` | List scheduled messages |

### Forwarding / Copying
| Method | Description |
|---|---|
| `forward_messages(chat_id, from_chat_id, message_ids, ...)` | Forward messages |
| `copy_message(chat_id, from_chat_id, message_id, ...)` | Copy without forward tag |
| `copy_media_group(chat_id, from_chat_id, message_id, ...)` | Copy media group |
| `forward_media_group(chat_id, from_chat_id, message_id, ...)` | Forward a media group |

### Deleting
| Method | Description |
|---|---|
| `delete_messages(chat_id, message_ids, ...)` | Delete messages |
| `delete_chat_history(chat_id, ...)` | Delete entire chat history |

### Reactions / Read status
| Method | Description |
|---|---|
| `read_chat_history(chat_id, ...)` | Mark chat as read |
| `read_mentions(chat_id)` | Mark mentions as read |
| `read_reactions(chat_id)` | Mark reactions as read |
| `retract_vote(chat_id, message_id)` | Retract poll vote |

### Pinning
| Method | Description |
|---|---|
| `pin_chat_message(chat_id, message_id, ...)` | Pin a message |
| `unpin_chat_message(chat_id, message_id)` | Unpin a message |
| `unpin_all_chat_messages(chat_id)` | Unpin all messages |

### Search
| Method | Description |
|---|---|
| `search_messages(chat_id, query, ...)` | Async generator: search in chat |
| `search_messages_count(chat_id, query, ...)` | Count search results |
| `search_global(query, ...)` | Async generator: global search |
| `search_global_count(query, ...)` | Count global results |

### Downloading
| Method | Description |
|---|---|
| `download_media(message, ...)` | Download media to disk (returns path) |

---

## Chats

### Getting Info
| Method | Description |
|---|---|
| `get_chat(chat_id)` | Get Chat object |
| `get_dialogs(...)` | Async generator of dialogs |
| `get_dialogs_count()` | Total dialog count |
| `get_chat_members(chat_id, ...)` | Async generator of members |
| `get_chat_members_count(chat_id, ...)` | Member count |
| `get_chat_member(chat_id, user_id)` | Get a specific member |
| `get_chat_online_count(chat_id)` | Online member count |

### Creating / Managing
| Method | Description |
|---|---|
| `create_group(title, users)` | Create a basic group |
| `create_supergroup(title, ...)` | Create a supergroup |
| `create_channel(title, ...)` | Create a channel |
| `delete_channel(chat_id)` | Delete a channel |
| `delete_supergroup(chat_id)` | Delete a supergroup |
| `join_chat(chat_id)` | Join a chat |
| `leave_chat(chat_id)` | Leave a chat |
| `archive_chats(chat_ids)` | Archive chats |
| `unarchive_chats(chat_ids)` | Unarchive chats |

### Moderation
| Method | Description |
|---|---|
| `ban_chat_member(chat_id, user_id, ...)` | Ban a user |
| `unban_chat_member(chat_id, user_id, ...)` | Unban a user |
| `restrict_chat_member(chat_id, user_id, permissions, ...)` | Restrict a user |
| `promote_chat_member(chat_id, user_id, ...)` | Promote to admin |
| `set_administrator_title(chat_id, user_id, title)` | Set custom admin title |
| `delete_user_history(chat_id, user_id)` | Delete all messages by user |
| `kick_chat_member(chat_id, user_id)` | Kick (ban + unban) |

### Settings
| Method | Description |
|---|---|
| `set_chat_title(chat_id, title)` | Update title |
| `set_chat_description(chat_id, description)` | Update description |
| `set_chat_photo(chat_id, photo)` | Set photo |
| `delete_chat_photo(chat_id)` | Remove photo |
| `set_chat_permissions(chat_id, permissions)` | Set default permissions |
| `set_chat_protected_content(chat_id, enabled)` | Toggle content protection |
| `set_chat_username(chat_id, username)` | Set/remove username |
| `set_slow_mode(chat_id, seconds)` | Set slow mode delay |
| `set_chat_ttl(chat_id, period)` | Set auto-delete timer |

### Folders
| Method | Description |
|---|---|
| `get_folders()` | Get all folders |
| `create_folder(title, ...)` | Create a folder |
| `edit_folder(folder_id, ...)` | Edit folder |
| `delete_folder(folder_id)` | Delete folder |

### Forum Topics
| Method | Description |
|---|---|
| `create_forum_topic(chat_id, title, ...)` | Create a topic |
| `edit_forum_topic(chat_id, topic_id, ...)` | Edit a topic |
| `close_forum_topic(chat_id, topic_id)` | Close a topic |
| `delete_forum_topic(chat_id, topic_id)` | Delete a topic |
| `get_forum_topics(chat_id)` | List topics |

---

## Users

| Method | Description |
|---|---|
| `get_me()` | Get your own User object |
| `get_users(user_ids)` | Get User objects by ID/username |
| `update_profile(...)` | Update name, bio |
| `set_username(username)` | Set your username |
| `set_profile_photo(photo, ...)` | Set profile photo |
| `delete_profile_photos(photo_ids)` | Delete profile photos |
| `get_chat_photos(chat_id, ...)` | Async generator of profile photos |
| `block_user(user_id)` | Block a user |
| `unblock_user(user_id)` | Unblock a user |
| `get_common_chats(user_id)` | List of common chats |
| `update_status(offline)` | Set online/offline status |

---

## Bots

| Method | Description |
|---|---|
| `answer_callback_query(callback_query_id, text, ...)` | Answer callback query |
| `answer_inline_query(inline_query_id, results, ...)` | Answer inline query |
| `answer_shipping_query(shipping_query_id, ok, ...)` | Answer shipping query |
| `answer_pre_checkout_query(pre_checkout_query_id, ok, ...)` | Answer pre-checkout |
| `send_invoice(chat_id, title, description, payload, ...)` | Send a payment invoice |
| `get_game_high_scores(user_id, ...)` | Get game high scores |
| `set_game_score(user_id, score, ...)` | Set game score |
| `request_callback_answer(chat_id, message_id, ...)` | Request bot callback (userbot) |
| `get_bot_default_privileges()` | Get bot default rights |
| `set_bot_default_privileges(privileges)` | Set bot default rights |

---

## Advanced

| Method | Description |
|---|---|
| `invoke(raw_function)` | Execute raw MTProto function directly |
| `resolve_peer(peer_id)` | Resolve to raw InputPeer |
| `save_file(path, ...)` | Upload a file, get file parts |
| `recover_gaps()` | Recover missed updates |
| `export_session_string()` | Export session as a portable string |

---

## Auth

| Method | Description |
|---|---|
| `connect()` | Manually connect (without `run()`) |
| `disconnect()` | Disconnect |
| `start()` | Start the client (without run loop) |
| `stop()` | Stop the client |
| `run(coroutine)` | Start client, run coroutine/idle, then stop |
| `sign_in(phone_number, phone_code_hash, phone_code)` | Sign in with phone |
| `sign_up(phone_number, phone_code_hash, first_name, ...)` | Create account |
| `sign_out()` | Log out |
| `send_code(phone_number)` | Send login code |

---

## Account

| Method | Description |
|---|---|
| `get_account_ttl()` | Get account deletion delay |
| `set_account_ttl(days)` | Set account deletion delay |
| `get_privacy(key)` | Get privacy setting |
| `set_privacy(key, rules)` | Update privacy setting |
| `get_global_privacy_settings()` | Get global privacy settings |
| `set_global_privacy_settings(settings)` | Update global privacy |

---

## Contacts

| Method | Description |
|---|---|
| `get_contacts()` | Get all contacts |
| `add_contact(user_id, first_name, ...)` | Add a contact |
| `delete_contacts(user_ids)` | Delete contacts |
| `import_contacts(contacts)` | Import contact list |
| `search_contacts(query, limit)` | Search among contacts |

---

## Stories

| Method | Description |
|---|---|
| `send_story(media, ...)` | Post a story |
| `edit_story(story_id, ...)` | Edit a story |
| `delete_stories(story_ids)` | Delete stories |
| `get_stories(chat_id, story_ids)` | Get story objects |
| `get_all_stories()` | Get all visible stories |

---

## Inline Keyboards (Types)

```python
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    ForceReply,
)

# Inline keyboard
markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Click me", callback_data="my_data")],
    [
        InlineKeyboardButton("URL", url="https://example.com"),
        InlineKeyboardButton("Switch inline", switch_inline_query="query"),
    ],
])

# Reply keyboard
markup = ReplyKeyboardMarkup([
    ["Option A", "Option B"],
    ["Cancel"],
], resize_keyboard=True, one_time_keyboard=True)
```

---

## Utilities

| Method | Description |
|---|---|
| `idle()` / `pyrogram.idle()` | Block until Ctrl+C (use in `run()`) |
| `compose([client1, client2])` | Run multiple clients together |
