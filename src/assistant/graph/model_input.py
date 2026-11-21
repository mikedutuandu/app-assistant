"""What the chat model is shown on each call: the system prompt (rules, the user, facts, files, drafts) and the
conversation.

Everything the user, their files, their app or the writer supplied reaches the model inside `<data>` blocks
(`prompts.data`), so it reads as material, never as instructions. The recorded conversations pin these texts byte
for byte (`scripts/converse_eval.py`): a change here is a change to what the model reads.
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from assistant.checks import Fact, nfc
from assistant.graph import prompts
from assistant.graph.prompts import data
from assistant.graph.state import ChatState
from assistant.packs import AppPack
from assistant.writer import Draft, language_tag

MAX_MESSAGES = 20  # conversation messages shown to the model
MAX_RECENT = 5  # the user's recent posts shown to the chat model (the writer sees them all)
MAX_DRAFTS_SHOWN = 10  # this thread's latest drafts in the system prompt
# After the user's latest message, the rule the chat model most often slips on (live tests, 20 Nov 2026).
REPLY_RULE = "(Your reply: at most one question, and it asks one thing.)"


def message_text(message: BaseMessage) -> str:
    """A stored message's text (its content can be a string or a list of parts)."""
    if isinstance(message.content, str):
        return message.content
    return " ".join(p.get("text", "") for p in message.content if isinstance(p, dict))


def draft_block(d: Draft, new: bool = False) -> str:
    if len(d.posts) == 1:
        text = d.posts[0]
    else:
        text = "\n\n".join(f"({i}/{len(d.posts)}) {p}" for i, p in enumerate(d.posts, 1))
    extra = {}
    if d.image_file_ids:
        extra["pictures"] = ", ".join(d.image_file_ids)
    if d.post_at is not None:
        extra["post_at"] = d.post_at
    if new:  # written in this message: never an earlier draft (review N3)
        return data("draft", text, id=d.id, kind=d.kind, written="just now, in this message", **extra)
    return data("draft", text, id=d.id, kind=d.kind, **extra)


def system_prompt(
    pack: AppPack,
    state: ChatState,
    facts: list[Fact],
    drafts: list[Draft],
    written: set[str],
    app_tools: bool = False,
) -> str:
    """The rules and everything the model knows this turn. Rebuilt before every call: facts and drafts change as the
    tools run (`written` holds the ids of the drafts written in this message)."""
    ctx = state.get("context")
    parts = [
        prompts.CONVERSE if pack.writes else prompts.ASSIST,
        f"App: {pack.name}",
        f"Language: {language_tag(ctx.language if ctx else '')}",
        f"The app's guide:\n{pack.guide.strip()}",
    ]
    name = state.get("user_name")
    if name:
        parts.append("The user's name: " + data("context", name))
    if ctx is not None:
        recent = "\n".join(f"- {p}" for p in ctx.recent_posts[:MAX_RECENT]) or "none"
        about = (
            f"Voice: {ctx.voice}\nTopics: {', '.join(ctx.topics)}\nPatterns that work: {'; '.join(ctx.patterns)}\n"
            f"Their latest posts:\n{recent}"
        )
        parts.append("About the user, from their app:\n" + data("context", about))
        if ctx.post_times:
            times = "\n".join(f"{t.at}: {t.label}" + (f" ({t.why})" if t.why else "") for t in ctx.post_times)
            parts.append(
                "Free times the app can publish at, soonest first (the user sees the labels; pick by `at`):\n"
                + data("context", times)
            )
        if ctx.kept_facts:
            kept = "\n".join(data("kept", k.text, id=k.id) for k in ctx.kept_facts)
            parts.append("Facts they kept in an earlier chat, not used in a post yet:\n" + kept)
    carried = state.get("carried") or []
    if carried:
        read = "\n".join(f"- {name}: {result}" for name, result in carried)
        parts.append(
            "Your last message was cut short (it took more work than one message allows). What you had read for it, "
            "still true now: use it rather than reading it again, then finish what they asked:\n" + data("app", read)
        )
    ask = state.get("ask") or []
    if ask:
        turns = "\n".join(f"{role}: {text}" for role, text in ask)
        parts.append(
            "The same chat's other turns, answered by the app's own assistant, newest last:\n" + data("ask", turns)
        )
    if app_tools:
        parts.insert(1, prompts.APP_TOOLS)
        changes = state.get("changes") or []
        shown_changes = "\n".join(
            data("change", c.get("result") or "", title=c.get("title", ""), state=c.get("state", "")) for c in changes
        )
        parts.append("Changes asked in this chat, oldest first:\n" + (shown_changes or "none yet"))
    if pack.writes:
        fact_lines = "\n".join(data("fact", f.text) for f in facts) or "none yet"
        parts.append("Facts saved in this chat:\n" + fact_lines)
    readings = state.get("readings", [])
    if readings:
        blocks = []
        for r in readings:
            text = r.text + (f"\nUnsure (not facts): {'; '.join(r.unsure)}" if r.unsure else "")
            blocks.append(data(r.kind, text, file_id=r.file_id))
        parts.append("Files and links they shared:\n" + "\n".join(blocks))
    if not pack.writes:
        return "\n\n".join(parts)
    recent_drafts = drafts[-MAX_DRAFTS_SHOWN:]
    shown = "\n".join(draft_block(d, new=d.id in written) for d in recent_drafts) or "none yet"
    if written and all(d.id in written for d in drafts):
        shown += (
            "\n(There were no drafts before this message: the ones written just now are this chat's first, "
            "never another version, a new take or a fresh one.)"
        )
    parts.append("Drafts in this chat, oldest first (the user sees each one with its buttons):\n" + shown)
    return "\n\n".join(parts)


def history(state: ChatState, about: Draft | None) -> list[dict]:
    """The conversation as chat messages, starting with the user's. The latest user message carries the reply rule,
    and, when it came from a draft's button (`about`), which draft it is about."""
    messages = [m for m in state.get("messages", []) if isinstance(m, HumanMessage | AIMessage)]
    messages = messages[-MAX_MESSAGES:]
    while messages and not isinstance(messages[0], HumanMessage):
        messages = messages[1:]  # the conversation shown starts with the user
    last = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    out: list[dict] = []
    for i, m in enumerate(messages):
        text = nfc(message_text(m)).strip()
        if isinstance(m, AIMessage):
            if text:
                out.append({"role": "assistant", "content": text})
            continue
        tapped = about if i == last else None
        if text:
            content = data("user", text)
        elif tapped is not None:
            content = "(No message: the user tapped an action on the draft.)"
        else:
            content = "(No message: the user sent a file.)"
        if i == last:
            content = f"{content}\n{REPLY_RULE}"
        if tapped is not None:
            # Only a draft's buttons send a message about a draft. Their words are the app's (English), not the
            # user's: the reply stays in the conversation's language.
            written_in = f", written in {tapped.language}" if tapped.language else ""
            content = (
                f"(The user tapped a button on draft {tapped.id}{written_in}. The button's words are the app's, not "
                f"the user's: keep replying in the language of the conversation.)\n{content}"
            )
        out.append({"role": "user", "content": content})
    return out


def final_messages(base: list[dict], parts: list[str], results: list[tuple[str, str]]) -> list[dict]:
    """The last call has no tools, so it carries no tool messages (an API refuses those without tools): what the
    model said and what its tools answered this turn come as plain messages."""
    out = list(base)
    if parts:
        out.append({"role": "assistant", "content": "\n\n".join(parts)})
    done = "\n".join(f"- {name}: {result}" for name, result in results)
    out.append(
        {
            "role": "user",
            "content": "(Tools are no longer available for this message. What your tools answered:\n"
            f"{done}\nNow answer the user in text.)",
        }
    )
    return out
