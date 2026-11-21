# How it works

A guide to the code for someone opening it for the first time, including someone new to LangGraph. Start here, then
read the files in the order of [Reading order](#reading-order).

## One message, start to end

```
browser ──POST /v1/chat/{app}/messages──► api/chat.py ──► api/turn.py: the turn runs as a background task;
                                                           its events stream back to the browser (SSE)
                                                                   │
                         ┌─────────────────── the graph (graph/build.py) ───────────────────┐
                         │                                                                   │
                         │  START → read_files → load_context → call_model ⇄ run_tools → END │
                         │                                                                   │
                         └───────────────────────────────────────────────────────────────────┘
                                │              │                │             │
                       files/read.py      tools.py:        llm.py:       writer.py: writes each draft
                       (pictures, PDFs,   the app's         OpenRouter    and checks it (numbers, rules,
                       voice, links)      /context          (every call   repeats, claims); tools.py
                                                            and its cost) saves facts in the app
```

1. **`read_files`** starts the turn: reads the files sent with the message and up to two links in its text.
2. **`load_context`** asks the host app for the user's voice, topics and recent posts (kept for 10 minutes).
3. **`call_model`** calls the chat model once, with the conversation and three tools it may call. Its text streams
   to the browser as it comes.
4. If the model called tools, **`run_tools`** runs them and the graph goes back to `call_model` with their results.
   If it didn't, the reply is done.

The tools are `save_facts` (keep what the user confirmed), `write_drafts` and `revise_draft` (the writer model writes,
every draft is checked, and each one that passes appears under the reply). After 4 calls with tools, a last call
without tools makes the model answer in text.

## What LangGraph does here, and what is plain Python

LangGraph runs the steps and remembers each conversation:

- **The graph** (`graph/build.py`) is the list of steps and the arrows between them. An arrow can depend on the state:
  after `call_model`, the graph goes to `run_tools` only when the model asked for tools (`_after_model`).
- **The state** (`graph/state.py`) is everything the bot remembers about one chat: the messages, the facts, the
  drafts, the files read, the app's context. Each step gets the state and returns the fields it changes. `messages`
  has a reducer (`add_messages`), so a step returns only the new message and LangGraph appends it.
- **The checkpointer** saves the state in Postgres after every step, keyed by `thread_id` (`{app}:{workspace}:{chat}`).
  The next message on the same chat starts from it. Nothing else stores the conversation.

The rest is plain Python, on purpose:

- **Money** (`graph/turn.py`). Every model call goes through `Turn`: the message's cost cap is checked first, and the
  call's cost is recorded the moment it ends (even a failed call's), so a turn that is cut off has still recorded
  what it spent.
- **The turn's work** (`graph/agent.py`). `call_model` and `run_tools` share an `AgentTurn`: this turn's messages with
  their tool calls and results, and the reply as shown so far. The thread keeps only what the user saw: the reply's
  text, the facts and the drafts.
- **The reply on screen** (`graph/reply_text.py`). After its tools the model sometimes repeats what it already said;
  a later round's text is held back and shown only for what it adds.
- **Model calls** (`llm.py`). A small OpenRouter client, not LangChain's models: it streams, retries once, falls back
  to the pack's other models, and returns the cost OpenRouter charged.

## What a step looks like

```python
async def call_model(state: ChatState, turn: Turn) -> dict:
    ...
    return {"facts": [...], "drafts": [...], "tools_pending": True}
```

`state` is the chat as LangGraph keeps it; `turn` is this message's view: the app's pack, the model client, the event
stream to the browser, and what the turn has spent. `graph/turn.py` (`bind`) adapts it to LangGraph's
`node(state) -> dict` and adds the turn's cost to what the step returns.

## Where the safety rules live

| Rule | Where |
|---|---|
| A post's numbers come only from the user | `checks.py` (`unsupported_numbers`), applied in `save_facts` and to every draft in `writer.py` |
| What the user, their files or app supplied is data, never instructions | `graph/prompts.py` (`data()`): everything goes inside `<data>` blocks |
| The bot never publishes | there is no publishing tool: a draft reaches the app only when the user taps Save |
| One workspace never sees another's | `auth.py` (signed session tokens), and every query is scoped by app and workspace |
| A message's spending has a ceiling | `graph/turn.py` (`gate`), checked before every model call |

## Reading order

1. `graph/state.py`: what the bot remembers (5 minutes).
2. `graph/build.py`: the steps and arrows.
3. `graph/nodes.py`: each step in a few lines.
4. `graph/agent.py`: one round of the conversation agent, and its three tools.
5. `graph/agent_tools.py`, `graph/model_input.py`, `graph/prompts.py`: what the model is told.
6. `writer.py`: how a draft is written and checked.
7. `api/turn.py`: how a message becomes a turn, its events and its charges.

The contract with host apps is [integration.md](integration.md); packs are [packs.md](packs.md).
