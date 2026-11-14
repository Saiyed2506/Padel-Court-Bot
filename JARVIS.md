# Jarvis: your personal assistant

Jarvis is an AI assistant that lives on your Windows PC and talks with you
through **Telegram**. Its "brain" is Claude (Anthropic's AI). It runs next to
your padel bot as a **separate program**, so a problem in Jarvis can't break
your bookings.

```
You: Remind me every weekday at 7:30 to take my vitamins
Jarvis: Done, Sam. Every weekday at 07:30, starting Monday 16 Nov.

You: Remember that my padel partner is Omar
Jarvis: Noted - Omar is your padel partner.

You: Will it rain in Hamburg tomorrow evening?
Jarvis: Light rain from about 18:00, 6°C (source: DWD forecast). Bring a jacket.
```

## The plan: built in stages

| Stage | What Jarvis can do | Status |
|---|---|---|
| 1 | Chat, remember facts about you, reminders (one-off and repeating), web search | **this version** |
| 2 | "Hey Jarvis" by voice at the PC, and voice messages in Telegram | next |
| 3 | Daily morning briefing (weather, calendar, padel games, news) | planned |
| 4 | Gmail + Outlook: email and calendar summaries, drafts. Asks before sending anything | planned |
| 5 | Control the padel bot in plain words ("book me a court Saturday evening") | planned |

Test each stage for a few days before we build the next one.

---

## Before you start: costs and risks, in plain words

- **It costs money per message.** Claude is paid per use, not by subscription.
  My estimate for the default model (Claude Opus 5.5) is about **$0.01-0.04 per
  message**. At 10-20 messages a day that's roughly **$5-20 a month**. I
  estimated this from the price list, not from real use: check `/cost` after a
  few days.
  - To halve it: set `model = "claude-sonnet-5-5"` in `jarvis.toml`.
  - Two brakes protect you. Jarvis' own **daily limit** (`daily_budget_usd`,
    default $1) and a **monthly limit you set at Anthropic** (setup step 1).
    The Anthropic one is the real protection, because Anthropic enforces it.
- **Your messages go to Anthropic** to be processed, so don't tell Jarvis
  anything you wouldn't put in an email. Jarvis is told never to save
  passwords or card numbers.
- **The PC must be on.** Jarvis runs on your PC. When the PC is off or asleep,
  Jarvis doesn't answer and reminders wait. When the PC is back, missed
  reminders arrive late, with a note saying so.
- **AI makes mistakes.** Jarvis can misunderstand a time or get a fact wrong.
  It always repeats a reminder's exact day and time, so check that line.
  Right now Jarvis **can't** spend money, send emails or contact anyone but
  you. Later stages will always ask you before doing things like that.
- **Only you.** Anyone can find a Telegram bot, but Jarvis only answers your
  chat and ignores everyone else.
- **Secrets** (API key, bot token) are in the `.env` file, which only your
  Windows user can read and which never goes to GitHub. If the key ever
  leaks, delete it in the Anthropic Console and make a new one.

---

## Setup (about 15 minutes, once)

1. Download this version (log in to GitHub in your browser first):
   https://github.com/Saiyed2506/Padel-Court-Bot/archive/refs/heads/ccr-f4f87bc7-e5xqex.zip
2. Right-click the ZIP → **Extract All**. Extract it to a **new folder**,
   e.g. `Documents\Jarvis`. Your running padel bot folder stays as it is.
3. Double-click **`JARVIS-SETUP.bat`**. If Windows warns, click
   **More info** → **Run anyway**. It installs one package (`anthropic`) and
   then walks you through:
   1. **Anthropic API key.** Create an account, add credit (e.g. $10), set a
      monthly limit, create a key.
   2. **A new Telegram bot for Jarvis**, via BotFather. It must be a new bot,
      not your padel bot, because two programs can't read the same bot's messages.
   3. **Say hello** to the new bot, so Jarvis knows your chat.
   4. **Your name and city.**
   5. **First hello.** Jarvis greets you in Telegram.
4. Double-click **`run-jarvis.bat`** and keep the window open. Close it to stop Jarvis.
5. Optional: **`autostart-jarvis.bat`** starts Jarvis whenever you log in to
   Windows. To keep a laptop awake while plugged in, run the padel bot's
   `autostart.bat` and answer question 2.

### Things to try first

- `Remind me in 5 minutes to stretch`, then wait for the ⏰ message
- `Every Sunday at 19:00 remind me to plan the week`
- `Remember that I prefer evening padel games`, then `/memories`
- `What's in the news about Hamburg today?`
- `/reminders`, `/cost`, `/new`, `/help`

---

## Settings (`jarvis.toml`)

Open it with Notepad. After a change, close the Jarvis window and start
`run-jarvis.bat` again.

| Setting | Default | What it does |
|---|---|---|
| `your_name`, `city` | from setup | how Jarvis addresses you; location for weather/local search |
| `model` | `claude-opus-5-5` | the AI. `claude-sonnet-5-5` ≈ half price, `claude-haiku-5-5` much cheaper but weaker |
| `effort` | `medium` | `low` = faster/cheaper, `high` = more careful on tricky requests |
| `web_search` | `true` | allow internet searches (about $0.01 each) |
| `daily_budget_usd` | `1.00` | Jarvis stops using the AI for the day at this estimated spend |
| `new_conversation_after_hours` | `6` | start a fresh chat after this much silence |
| `max_conversation_messages` | `60` | ...or when a chat gets this long (long chats cost more) |

---

## How it works (for learning)

```
 Telegram (your phone)
        │  your message
        ▼
 jarvis/app.py ──── checks it's really you, shows "typing..."
        │
        ▼
 jarvis/brain.py ── sends the conversation + Jarvis' instructions + tool list to Claude
        │              │
        │              ▼  Claude answers with text, OR asks to use a tool:
        │           "set_reminder(text='Call mum', when='2026-11-15 18:00')"
        ▼
 jarvis/tools.py ── runs the tool, sends the result back to Claude, repeat until text
        │
        ▼
 jarvis/store.py ── saves memories, reminders, spending in jarvis_data/ (JSON files)
```

Ideas worth understanding:

- **Claude has no memory of its own.** Each request sends the whole
  conversation again. That's why long chats cost more, and why Jarvis starts a
  fresh one after a while. Lasting knowledge goes into `memories.json`, which
  is put into Jarvis' instructions at the start of every conversation.
- **Claude can't do anything by itself.** It can only ask to use one of the
  tools we wrote (`jarvis/tools.py`). Our code decides what actually happens.
  That's the main safety idea behind every later stage: Jarvis only gets the
  abilities we deliberately give it.
- **The instructions (system prompt)** are in `build_system_prompt` in
  `jarvis/brain.py`. That's where Jarvis' personality and rules live. Read it.
- **Reminders don't need the AI.** The main loop in `jarvis/app.py` checks
  `reminders.json` about every 20 seconds and sends due ones directly.

| File | What's in it |
|---|---|
| `jarvis/app.py` | Telegram loop, `/commands`, firing reminders |
| `jarvis/brain.py` | talking to Claude, the instructions, cost estimate |
| `jarvis/tools.py` | the tools Claude may use |
| `jarvis/store.py` | saving memories, reminders, spending |
| `jarvis/setup_wizard.py` | the setup questions |
| `jarvis_data/` | your data, plus `jarvis.log` (what Jarvis did). Never uploaded |
| `tests/test_jarvis.py` | offline tests with a fake Claude: `py -m unittest -v` |

Testing without Telegram: `py -m jarvis chat` in PowerShell, from the folder.

---

## Troubleshooting

| You see | Do this |
|---|---|
| "My API key was rejected" | Run `JARVIS-SETUP.bat` again, paste a new key |
| "out of credit" | console.anthropic.com → Billing → add credit |
| "Today's AI budget is used up" | Wait until midnight, or raise `daily_budget_usd` |
| No answer at all | Is the `run-jarvis.bat` window open? Look at its last lines, or `jarvis_data\jarvis.log` |
| "I can't reach Anthropic" | Internet on the PC? Jarvis retries by itself |
| Reminder came late | The PC was off or asleep at that time |
| Jarvis forgot what we said earlier | A new conversation started (after 6 h silence). Ask it to *remember* important things |
