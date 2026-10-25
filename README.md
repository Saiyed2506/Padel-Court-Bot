# Padel Court Bot

Watches **Padel Amigos Hamburg-Halstenbek** on Playtomic for free **indoor doubles
courts** in your time slots, and sends you a **Telegram** message with a link
as soon as one appears:

```
🎾 1 new free court slot(s) at Padel Amigos Hamburg-Halstenbek

⭐ Free: Tue 28 Oct, 17:00-18:30, Court 2 indoor, €48. Book: https://...
```

Default slots (edit in `config.toml`):

| Days    | Window        | Length | Matching start times              |
|---------|---------------|--------|-----------------------------------|
| Mon-Fri | 16:00 - 20:30 | 90 min | 16:00 ... 19:00 (⭐ 16:30, 17:00)  |
| Sat-Sun | 09:30 - 12:30 | 90 min | 09:30, 10:00, 10:30, 11:00        |

A game must **fit completely** inside the window. Courts open 14 days ahead,
so the bot watches today plus the next 14 days.

The bot only **reads** public availability. It never logs in to Playtomic and
never needs your Playtomic password.

---

## Before you start: risks, in plain words

- **No official API.** Playtomic only offers an official API to clubs. This bot
  reads the same public data the playtomic.com website shows to visitors who
  aren't logged in: the club page and its availability endpoint. (The app's
  API now needs a login; the old one is gone.) These can change or be blocked
  at any time. If checks keep failing, the bot sends you a ⚠️
  Telegram message so you don't think there are simply no free courts.
- **Terms of service: not fully clear.** An older Playtomic terms document
  (for club customers) bans "any type of robotic tool". I couldn't verify the
  current player terms. Read them yourself (Playtomic app → Profile →
  Settings → legal/terms). Low-frequency, read-only checking is close to
  refreshing the app by hand, but Playtomic hasn't approved it.
- **Gentle polling is built in.** About 90 requests per hour, random timing, a
  pause between requests, nothing at night, automatic backing off on errors.
  The config refuses intervals under 2 minutes.
- **Not truly instant.** A slot is found within about 5 minutes (20 for days
  4-13). Very popular cancelled slots can go faster than that.

---

## Easiest setup: double-click (Windows)

1. Download the code (log in to GitHub in your browser first):
   https://github.com/Saiyed2506/Padel-Court-Bot/archive/refs/heads/ccr-7f8b936d-chkl4x.zip
2. Open your Downloads folder, right-click the ZIP → **Extract All** → **Extract**.
3. In the extracted folder, double-click **`START-HERE.bat`**.
   - If Windows shows "Windows protected your PC", click **More info** → **Run anyway**.
   - If Python is missing, it opens the download page and tells you what to click.
     Install it, then double-click `START-HERE.bat` again.
4. Answer the questions in the black window. It walks you through creating
   the Telegram bot, finds the club, sends a test message and shows free slots
   to compare with the app.
5. When it says **All set**, double-click **`run-bot.bat`** to start watching.
   Keep that window open. Close it to stop.

The manual steps below do the same thing by hand.

## Manual setup on Windows (one time, about 20 minutes)

All commands go into **PowerShell**: press Start, type `powershell`, press Enter.

### 1. Check Python (version 3.11 or newer)

```powershell
py --version
```

- Shows `Python 3.11` or higher → good, continue.
- Error or older version → install Python from https://www.python.org/downloads/
  and **tick "Add python.exe to PATH"** in the installer. Then close and reopen
  PowerShell and run `py --version` again.

No other packages are needed. The bot uses only Python's built-in libraries.

### 2. Get the code

With Git installed:
```powershell
cd $HOME\Documents
git clone -b ccr-7f8b936d-chkl4x https://github.com/saiyed2506/padel-court-bot.git
cd padel-court-bot
```
Without Git: on GitHub, switch to the branch, click **Code → Download ZIP**,
unzip it into `Documents`, then `cd` into that folder in PowerShell.

All commands below are run **from this folder**.

### 3. Create your Telegram bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Choose a name (e.g. `My Padel Alerts`) and a username ending in `bot`.
3. BotFather replies with a **token** like `123456789:AAH...`. Treat it like a
   password.
4. Open your new bot in Telegram and press **Start**, or send it `hi`.
   This lets it message you.

### 4. Put your secrets in `.env`

```powershell
copy .env.example .env
notepad .env
```

Paste the token after `TELEGRAM_BOT_TOKEN=` and save. (Notepad sometimes saves
as `.env.txt`. Run `dir` to check the file is called exactly `.env`.)

Now find your chat ID:
```powershell
py -m padel_bot telegram-chat-id
```
Copy the printed `TELEGRAM_CHAT_ID=...` line into `.env`, save, then test:
```powershell
py -m padel_bot test-notify
```
You should get a Telegram message. **Tap the link in it.** If it doesn't open
the club in Playtomic, see "Booking link" below.

`.env` is listed in `.gitignore`, so git never uploads it.

### 5. Tell the bot which club

Find the club on playtomic.com in your browser (for example, Google
`playtomic padel amigos halstenbek`) and copy the address from the address bar.
It looks like `https://playtomic.com/clubs/<some-name>`. Then:

```powershell
py -m padel_bot set-club https://playtomic.com/clubs/<some-name>
```
It saves the link in `config.toml`, stores the club ID and courts in
`club.json`, and lists all padel courts, marking the watched ones
(`<- watched`). There should be **4 indoor doubles courts**.

### 6. Check that times are correct (important, once)

```powershell
py -m padel_bot check --all --days 1
```
This lists **every** free slot on the watched courts for today and tomorrow.
Compare a few against the Playtomic app:
- Times match → good.
- Everything is shifted by 1 or 2 hours → stop and tell me. The time zone
  conversion is then wrong.

Then see what currently matches your slots:
```powershell
py -m padel_bot check
```

### 7. Run it

```powershell
py -m padel_bot run
```

Leave the window open. Press **Ctrl+C** to stop. On the first run you get one
message listing every slot that is **already** free. After that, you only get
messages for **new** ones. If a slot gets booked and is cancelled later, you're
told again.

---

## Telling the bot your wishes (Telegram commands)

While `run-bot.bat` is running, send these to your bot in Telegram (or tap the
**/** menu button). Only your own chat is obeyed.

| Command | What it does |
|---|---|
| `/wishes` | Show what's being watched, numbered |
| `/wish tue 17:00` | Watch exactly 17:00-18:30 on Tuesdays (starred) |
| `/wish weekend 09:30-12:30` | Any 90-min game inside that window on Sat/Sun |
| `/wish mon-fri 16:00-20:30 prefer 16:30,17:00` | Window with starred favourite times |
| `/wish sat 10:00 120` | A 120-minute game |
| `/remove 2` | Delete wish number 2 |
| `/reset` | Back to the wishes in `config.toml` |
| `/now` | Free slots matching your wishes right now |
| `/status` | Is the bot running, when was the last check |
| `/pause`, `/resume` | Stop / restart alerts |

Days can be `mon`..`sun` (or German `mo`, `di`, ...), `mon-fri`, `sat,sun`,
`weekdays` or `weekend`. Wishes are saved in `wishes.json`, so they survive a
restart. After the first change they replace the windows in `config.toml`.

## Club events (Newcomer Club, Afterwork, classes)

The bot also watches club events whose **name contains a word you choose**:

- **Group classes** (Gruppenstunden) come from the club's public page. No login needed.
- **Open Play events** (like "Newcomer Club mit Coach David") have no public
  list, so the bot reads them with **your Playtomic login**. Double-click
  **`playtomic-login.bat`** once. Your password is sent to Playtomic once and
  **not saved**; only the login token Playtomic returns is kept in
  `playtomic_token.json` (never uploaded; keep it private). The bot only
  *reads* the event list; it never books or pays. `py -m padel_bot logout`
  deletes the token.

What you get in Telegram:

- **🆕 New date:** a watched event publishes a new date (checked every 30 min).
- **🟢 Spot free again:** a full event gets a cancellation.
- **📅 Weekly overview:** every Sunday at 18:00, all upcoming dates with spots left.

| Command | What it does |
|---|---|
| `/events` | Upcoming dates of watched events, with spots |
| `/watch Afterwork` | Also watch events with "Afterwork" in the name |
| `/unwatch 2` | Stop watching name number 2 |

Risk note: the logged-in part uses Playtomic's undocumented app login and
makes an automated (read-only) request about every 30 minutes during the day.
Playtomic could notice that and could restrict your account. It can also
stop working whenever Playtomic changes its app.

## Updating the bot

Close the bot window, double-click **`update.bat`**, then start `run-bot.bat`
again. It replaces only the program files. Your `.env`, `config.toml`, club
and wishes stay as they are.

## Keeping it running on a laptop

- The bot only works while the laptop is **on and awake**. Closed lid or sleep
  means no alerts. To keep it awake while plugged in: Settings → System →
  Power & battery → Screen and sleep → *"When plugged in, put my device to
  sleep after"* → **Never**. The screen may still turn off.
- After a restart, open PowerShell, `cd` into the folder and run
  `py -m padel_bot run` again. The bot remembers what it already told you
  (`state.json`), so you won't get duplicates.

---

## Settings you may want to change (`config.toml`)

| Setting | What it does |
|---|---|
| `[[watch.window]]` | Days, start/end and length of the games you want |
| `preferred_starts` | Marked ⭐ and listed first |
| `[notify] only_preferred = true` | Only alert for preferred start times |
| `[courts] exclude_names` | Skip particular courts by name |
| `[polling] quiet_hours` | No checks at night (default 23:00-07:00) |

**Quiet hours vs. new days.** I don't know at what time of day Playtomic
opens the 14th day for booking. If it's at midnight and slots get taken
before 07:00, shorten `quiet_hours` (e.g. `["01:00", "07:00"]`) or set it to `[]`.

**Booking link.** Playtomic has no documented link to one exact slot. The
default link opens the club page on the slot's day
(`{club_url}?date={date}`). If you prefer the app's share link, put it in
`booking_url`.

---

## Auto-booking (prepared, switched off)

`padel_bot/booking.py` contains the safety frame for booking automatically
later:

- `enabled = false` by default.
- `max_price_eur` is mandatory. Slots that cost more, or have an unknown price,
  are never booked.
- `dry_run = true` by default: it only logs what it *would* book.
- Every real booking needs your **confirmation**. There's no option to skip it.
- Real booking isn't implemented. The program refuses `dry_run = false`.

Real booking would mean logging in as you and paying through Playtomic's
undocumented payment API. That carries a **much higher risk** than watching:
your account could be suspended, it uses real money, and it's the part most
likely to break the terms. Try the notifier for a few weeks first. Tapping the
link only takes seconds.

---

## For the curious

```
padel_bot/
  playtomic.py  read-only API calls (club, courts, availability)
  rules.py      turns API data into slots and applies your windows
  watcher.py    polling loop, scheduling, error handling
  state.py      remembers which slots you were already told about
  notify.py     message text and Telegram sending
  setup_wizard.py  guided setup behind START-HERE.bat
  booking.py    auto-book safety rules (disabled)
  timeutil.py   Hamburg time zone (works on Windows without extra packages)
config.toml     your settings      .env  your secrets (not in git)
tests/          offline tests: py -m unittest -v
```

## Troubleshooting

| Message | Fix |
|---|---|
| `'py' is not recognized` | Install Python (step 1), then reopen PowerShell |
| `TELEGRAM_BOT_TOKEN is not set` | `.env` missing, misnamed (`.env.txt`?) or not in this folder |
| `Telegram ... chat not found` | Press Start in your bot chat, recheck `TELEGRAM_CHAT_ID` |
| `Playtomic problem: HTTP 403/404` | Playtomic changed or blocked the endpoint. Tell me. |
