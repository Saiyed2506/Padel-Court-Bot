"""Start Jarvis: `py -m jarvis run` (or setup / chat). The .bat files do this for you."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from padel_bot.config import load_dotenv
from padel_bot.timeutil import club_timezone

from .config import DATA_DIR, ENV_FILE, ConfigError, load_settings, require_env
from .store import Store

log = logging.getLogger("jarvis")


def setup_logging() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(DATA_DIR / "jarvis.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m. %H:%M:%S",
                        handlers=handlers)
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_brain():
    import anthropic

    from .brain import Brain

    settings = load_settings()
    client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
    tz = club_timezone()
    store = Store(DATA_DIR, tz)
    return Brain(client, settings, store, DATA_DIR), store, tz


def cmd_run() -> int:
    from .app import Jarvis, JarvisTelegram

    brain, store, tz = make_brain()
    telegram = JarvisTelegram(require_env("JARVIS_TELEGRAM_TOKEN"), require_env("JARVIS_CHAT_ID"))
    Jarvis(telegram, brain, store, tz).run_forever()
    return 0


def cmd_chat() -> int:
    """Talk to Jarvis in this window instead of Telegram - handy for testing."""
    from .brain import BudgetReached, explain_error

    brain, store, tz = make_brain()
    print("Chat with Jarvis here. Empty line to quit.")
    while True:
        text = input("\nYou: ").strip()
        if not text:
            return 0
        try:
            reply = brain.ask(text, datetime.now(tz))
            print(f"\nJarvis: {reply.text}\n   (about ${reply.cost_usd:.3f})")
        except BudgetReached:
            print("Today's budget is used up (daily_budget_usd in jarvis.toml).")
        except Exception as exc:
            print(explain_error(exc))


def cmd_setup() -> int:
    from .setup_wizard import run_setup

    def first_hello(telegram) -> None:
        brain, _, tz = make_brain()
        brain.start_new_conversation(datetime.now(tz), keep_recap=False)
        reply = brain.ask("Hello Jarvis! You've just been set up. Greet me in two short sentences "
                          "and give one example of what I can ask you.", datetime.now(tz))
        telegram.send(reply.text)
        print(f"Jarvis said: {reply.text}")

    return run_setup(first_hello)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal assistant on Telegram.")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "setup", "chat"])
    args = parser.parse_args(argv)
    load_dotenv(ENV_FILE)
    setup_logging()
    try:
        return {"run": cmd_run, "setup": cmd_setup, "chat": cmd_chat}[args.command]()
    except ConfigError as exc:
        print(f"\nProblem: {exc}")
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
