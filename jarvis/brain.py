"""Jarvis' brain: talks to Claude, runs the tools Claude asks for, keeps the conversation.

One "turn" (you send a message, Jarvis answers) can take several calls to
Claude: Claude may first ask for a tool (e.g. set_reminder), we run it and send
the result back, and only then does Claude write its answer.

Rules this file follows so Claude's API is happy (and cheap):
* The conversation is append-only. We never edit or delete earlier messages,
  because Claude's hidden reasoning is tied to the exact conversation so far.
  When a conversation gets long or old, we simply start a new one.
* The instructions ("system prompt") and tool list are fixed for the whole
  conversation, so the API can re-use them from its cache (about 10x cheaper).
* The current time goes into each of your messages, not into the instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from .config import Settings
from .store import Store, read_json, write_json
from .tools import TOOLS, ToolBox, tool_input, web_search_tool

log = logging.getLogger("jarvis")

MAX_TOOL_ROUNDS = 10
MAX_TOKENS = 16000

# Beta features we use:
#  * server-side fallback: if Claude's safety filter wrongly declines a harmless
#    request, the API retries it on another Claude model instead of failing.
#  * thinking binding controls: if the conversation history ever doesn't match,
#    drop the stale hidden reasoning instead of failing the request.
BETAS = ["server-side-fallback-2026-07-01", "thinking-binding-controls-2026-08-01"]
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}

# US dollars per million tokens: input, output, cache write, cache read.
# Used only to estimate spending for the daily brake; Anthropic's bill is the truth.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
WEB_SEARCH_USD = 0.01  # per search


class BudgetReached(Exception):
    pass


@dataclass
class Reply:
    text: str
    cost_usd: float


def estimate_cost(model: str, usage) -> float:
    price_in, price_out, price_write, price_read = PRICES.get(model, PRICES["claude-opus-5-5"])
    tokens = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
    cost = (tokens("input_tokens") * price_in + tokens("output_tokens") * price_out
            + tokens("cache_creation_input_tokens") * price_write
            + tokens("cache_read_input_tokens") * price_read) / 1_000_000
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return cost + searches * WEB_SEARCH_USD


def format_now(now: datetime) -> str:
    return f"{now:%a %d %b %Y, %H:%M}"


def is_conversation_problem(exc: Exception) -> bool:
    """A 400 that isn't about money is most likely caused by something in the stored
    conversation; it would fail again on every message, so we start fresh instead."""
    return (isinstance(exc, anthropic.BadRequestError)
            and "credit balance" not in str(getattr(exc, "message", exc)).lower())


def explain_error(exc: Exception) -> str:
    """A plain-words message for you when talking to Claude fails."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "My API key was rejected. Run JARVIS-SETUP.bat again and paste a valid Anthropic API key."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Anthropic says this API key may not use that model. Check 'model' in jarvis.toml."
    if isinstance(exc, anthropic.NotFoundError):
        return "Anthropic doesn't know the model in jarvis.toml. Check the spelling of 'model'."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic says I'm sending too much right now. Give me a minute and try again."
    if isinstance(exc, anthropic.BadRequestError):
        message = str(getattr(exc, "message", exc))
        if "credit balance" in message.lower():
            return "My Anthropic account is out of credit. Top it up at console.anthropic.com (Billing)."
        return (f"Anthropic refused the request ({message[:300]}). "
                "I'll start a fresh conversation - please send that again.")
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic had a problem (error {exc.status_code}). Try again in a minute."
    if isinstance(exc, anthropic.APIConnectionError):
        return "I can't reach Anthropic. Is the internet working on the PC?"
    return f"Something went wrong: {exc}"


def build_system_prompt(settings: Settings, memories: list[dict], recap: list[str]) -> str:
    name = settings.your_name or "the user"
    lines = [
        f"You are Jarvis, the personal assistant of {name}. You run on {name}'s Windows PC and talk "
        f"with {name} through Telegram, mostly on their phone.",
        "",
        "How you talk:",
        "- Replies are read on a phone: keep them short and to the point. Use plain text; Telegram "
        "shows Markdown symbols like ** and # literally, so don't use them. Simple lists with '-' are fine.",
        f"- Be calm, warm and capable, with a light touch of dry wit. Use {name}'s name now and then, not every time.",
        "- Each user message starts with the current local time (Europe/Berlin) in square brackets. Use it "
        "to work out dates like 'tomorrow' or 'next Friday'. The user didn't type that part.",
        "- Text starting with '[Since the last message:' reports events that happened meanwhile, such as a "
        "reminder you delivered. The user didn't type it either.",
        "",
        "What you can do right now, through your tools:",
        "- Remember lasting facts about the user and forget them again. Save things worth knowing later "
        "(people, preferences, routines, plans) without being asked, and say briefly that you noted it. "
        "Never save passwords, card numbers or similar secrets.",
        "- Set, list and cancel reminders, one-off or repeating. They arrive as Telegram messages even when "
        "the user isn't chatting. After setting one, state the exact day and time back. If the time is "
        "vague ('this evening'), choose a sensible time and say which.",
    ]
    if settings.web_search:
        lines.append("- Search the web for current information (news, weather, opening hours, facts). "
                     "Mention where the information came from in a few words.")
    lines += [
        "",
        "Not available yet (coming in later stages): voice, reading email or calendar, a daily briefing, "
        "and controlling the user's padel booking bot. If asked, say plainly that it isn't set up yet. "
        "Never pretend to have done something you have no tool for, and never invent results.",
        "You cannot spend money or contact anyone other than the user.",
    ]
    if memories:
        lines += ["", "What you remember about the user (memory number: fact):"]
        lines += [f"#{m['id']}: {m['fact']}" for m in memories]
    if recap:
        lines += ["", "The end of your previous conversation, for context:"]
        lines += recap
    return "\n".join(lines)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


class Brain:
    def __init__(self, client: anthropic.Anthropic, settings: Settings, store: Store, data_dir: Path):
        self.client = client
        self.settings = settings
        self.store = store
        self.toolbox = ToolBox(store)
        self.file = data_dir / "conversation.json"
        self.tools = list(TOOLS)
        if settings.web_search:
            self.tools.append(web_search_tool(settings.city))
        self.conversation = read_json(self.file, None)

    # ---- conversation bookkeeping -----------------------------------------
    def _setup_key(self) -> str:
        """Changes when the model or tool list changes; that needs a new conversation."""
        return f"{self.settings.model}|{','.join(t['name'] for t in self.tools)}"

    def _recap(self) -> list[str]:
        """Last few plain-text exchanges of the old conversation (no hidden reasoning)."""
        if not self.conversation:
            return []
        recap = []
        for message in self.conversation["messages"][-12:]:
            text = _text_of(message["content"])
            if not text:
                continue  # tool calls and results
            who = "User" if message["role"] == "user" else "You"
            recap.append(f"{who}: {text[:400]}")
        return recap[-6:]

    def needs_new_conversation(self, now: datetime) -> bool:
        c = self.conversation
        if not c or c.get("setup") != self._setup_key():
            return True
        if len(c["messages"]) >= self.settings.max_conversation_messages:
            return True
        last = datetime.fromisoformat(c["last"])
        return now - last > timedelta(hours=self.settings.new_conversation_after_hours)

    def start_new_conversation(self, now: datetime, keep_recap: bool = True) -> None:
        recap = self._recap() if keep_recap else []
        self.conversation = {
            "started": now.isoformat(),
            "last": now.isoformat(),
            "setup": self._setup_key(),
            "system": build_system_prompt(self.settings, self.store.memories(), recap),
            "messages": [],
        }
        self._save()

    def _save(self) -> None:
        write_json(self.file, self.conversation)

    def _append(self, role: str, content) -> None:
        self.conversation["messages"].append({"role": role, "content": content})
        self._save()

    # ---- talking to Claude ------------------------------------------------
    def _call_claude(self):
        thinking = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        extra = {"fallbacks": "default"} if self.settings.model in FALLBACK_MODELS else {}
        return self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=MAX_TOKENS,
            system=self.conversation["system"],
            tools=self.tools,
            messages=self.conversation["messages"],
            thinking=thinking,
            output_config={"effort": self.settings.effort},
            cache_control={"type": "ephemeral"},  # re-use the unchanged start of the chat
            betas=BETAS,
            **extra,
        )

    def ask(self, text: str, now: datetime) -> Reply:
        """Your message in, Jarvis' answer out (running any tools in between)."""
        budget = self.settings.daily_budget_usd
        if self.store.spent(now.date()) >= budget:
            raise BudgetReached(budget)
        if self.needs_new_conversation(now):
            self.start_new_conversation(now)

        notes = self.store.take_notes()
        prefix = f"[{format_now(now)}]\n"
        if notes:
            prefix += "[Since the last message: " + " ".join(notes) + "]\n"
        start_len = len(self.conversation["messages"])
        self._append("user", [{"type": "text", "text": prefix + text}])
        self.conversation["last"] = now.isoformat()

        total = 0.0
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = self._call_claude()
                cost = estimate_cost(self.settings.model, response.usage)
                total += cost
                self.store.add_spend(now.date(), cost)
                content = [block.to_dict(mode="json") for block in response.content]
                # (an empty answer can't be stored: the API rejects empty messages)
                self._append("assistant", content or [{"type": "text", "text": "(no answer)"}])

                if response.stop_reason == "tool_use":
                    self._append("user", self._run_tools(response.content, now))
                    continue
                if response.stop_reason == "pause_turn":
                    continue  # a long web search; the API continues where it paused
                answer = _text_of(content)
                if response.stop_reason == "refusal":
                    answer = answer or "I'm not able to help with that one."
                elif response.stop_reason == "max_tokens":
                    answer += "\n\n(My answer got cut off - ask me to continue.)"
                return Reply(answer or "Done.", total)
            return Reply("I went round in circles on that one and stopped. Could you rephrase it?", total)
        except Exception as exc:
            if len(self.conversation["messages"]) == start_len + 1:
                # Claude never answered: take your message back out, so a retry starts clean.
                self.conversation["messages"].pop()
                for note in notes:
                    self.store.add_note(note)
            if is_conversation_problem(exc):
                self.conversation["setup"] = "broken"  # the next message starts a fresh conversation
            self._save()
            raise

    def _run_tools(self, blocks, now: datetime) -> list[dict]:
        results = []
        for block in blocks:
            if block.type != "tool_use":
                continue  # server tools (web search) already ran at Anthropic
            try:
                args = tool_input(block.input)
            except ValueError:
                text, is_error = "Error: the tool input was not valid JSON. Try again.", True
            else:
                text, is_error = self.toolbox.run(block.name, args, now)
            log.info("Tool %s(%s) -> %s", block.name, block.input, text[:200])
            result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
            if is_error:
                result["is_error"] = True
            results.append(result)
        return results
