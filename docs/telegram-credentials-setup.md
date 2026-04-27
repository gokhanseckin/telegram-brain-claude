# Telegram Credentials Setup

You need two separate things: a **Telegram App** (for the userbot that reads your messages) and a **Telegram Bot** (for delivering Briefs and handling commands).

---

## 1. Create a Telegram App (API ID & Hash)

The userbot uses [Telethon](https://docs.telethon.dev) and requires an `api_id` + `api_hash` from Telegram's developer portal.

### Steps

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Click **API development tools**.
3. Fill in the form:
   | Field | What to enter |
   |---|---|
   | **App title** | `TG Brain` |
   | **Short name** | Something unique, e.g. `tgbrain_yourname` (5–32 chars, alphanumeric) |
   | **URL** | Leave blank |
   | **Platform** | Select **Other** |
   | **Description** | `Personal Telegram userbot for message ingestion` |
4. Click **Create application**.

### Common errors

- **Silent "error" on submit** — most likely causes:
  - Short name already taken → try a more unique name
  - VPN/proxy active → disable it and retry
  - Session expired → log out and log back in to my.telegram.org
  - Account too new → wait a day and try again
- **Already have an app** — you can only have one per account; scroll up on the page to see your existing `api_id` and `api_hash`.

### Result

You'll see your credentials on the next page:

```
App api_id:   1234567
App api_hash: abcdef1234567890abcdef1234567890
```

Add them to `.env`:

```env
TBC_TG_API_ID=1234567
TBC_TG_API_HASH=abcdef1234567890abcdef1234567890
```

---

## 2. Create a Telegram Bot (Bot Token)

The bot delivers Briefs and responds to commands. It is created via [@BotFather](https://t.me/BotFather).

### Steps

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`.
3. Follow the prompts:
   - **Name**: `TG Brain` (display name, anything)
   - **Username**: must end in `bot`, e.g. `tgbrain_yourname_bot`
4. BotFather replies with your token:

```
Use this token to access the HTTP API:
7123456789:AAF_abc123xyz...
```

Add it to `.env`:

```env
TBC_TG_BOT_TOKEN=7123456789:AAF_abc123xyz...
```

### Also set your owner user ID

Your bot needs to know who you are so it only responds to you.

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram.
2. It replies with your numeric user ID (e.g. `123456789`).

Add to `.env`:

```env
TBC_TG_OWNER_USER_ID=123456789
```

---

## 3. Full `.env` credentials block

```env
# Telegram userbot (Telethon)
TBC_TG_API_ID=1234567
TBC_TG_API_HASH=abcdef1234567890abcdef1234567890
TBC_TG_SESSION_PATH=/var/lib/tbc/session

# Telegram bot (aiogram)
TBC_TG_BOT_TOKEN=7123456789:AAF_abc123xyz...
TBC_TG_OWNER_USER_ID=123456789
```

Copy `.env.example` to `.env` first:

```bash
cp .env.example .env
```

Then fill in the values above alongside the other required variables (see `.env.example` for the full list).
