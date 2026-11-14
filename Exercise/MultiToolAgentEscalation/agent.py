"""
agent.py — The agent loop: the only file that talks to the Claude API and the
only one that imports all the others.

Each iteration:
  1. send `messages` + tools.TOOLS to Claude
  2. branch on response.stop_reason
       "end_turn" -> Claude is done; return its text
       "tool_use" -> run each requested tool (hooks.pre_tool_use -> backend),
                     append ALL results in ONE user message, loop again
       anything else -> handled explicitly, never silently

Manual loop on purpose (the SDK's beta Tool Runner would automate it): the
exercise is about checking stop_reason yourself.

Wiring:
  tools.TOOLS              -> what Claude may call (sent every request)
  hooks.pre_tool_use       -> policy gate before execution
  backend.TOOL_FUNCTIONS   -> name -> function that does the work
  errors.ToolError         -> caught in ONE place, returned as is_error results
"""

import inspect  # signature().bind() checks Claude's arguments without running the function
import json  # tool results travel to Claude as JSON strings
import logging  # stdlib logging; destinations are configured only in log_config.setup_logging
import sys  # argv for the CLI and stderr for developer tracebacks
import traceback  # prints full stack traces for unexpected bugs (to stderr only)

import anthropic  # official Claude SDK: client + typed API errors

import backend  # TOOL_FUNCTIONS (what actually runs) and the escalation queue
import hooks  # pre_tool_use: the policy gate run before every tool
import log_config  # run IDs, JSON-safe dump(), and setup_logging for the entry point
from errors import ErrorCategory, ToolError  # structured errors we catch and build
from tools import TOOLS  # the tool schemas sent to Claude on every request

log = logging.getLogger(__name__)  # this module's logger ("agent"); never configures handlers itself

# --- Config -----------------------------------------------------------------
MODEL = "claude-haiku-5-5"  # dev choice (cheap); Sonnet joins later for the tool-selection eval
EFFORT = "medium"           # Haiku 5.5's default — set explicitly so it isn't a hidden assumption
MAX_TOKENS = 16000          # non-streaming default; lower risks truncated replies
MAX_ITERATIONS = 10         # runaway guard: caps API round-trips per request (cost + infinite retries)

# No dollar threshold here: hooks.py is the single source of truth for policy.
SYSTEM_PROMPT = """\
You are a customer support agent for an online store. You can look up orders, \
issue refunds, and escalate cases to a human agent using the provided tools.

Guidelines:
- Address EVERY part of the customer's request; multi-part requests are common.
- Verify before acting: look up an order before refunding it.
- Never invent order IDs, emails, or amounts. If something you need is missing, ask.
- Tool errors are structured (errorCategory, isRetryable, description). If \
isRetryable is true you may retry the same call once; otherwise follow the \
description's guidance instead of repeating the call.
- If a refund is blocked by policy, follow the instructions in the error and \
share the ticket ID with the customer. Never claim a blocked or escalated \
request is resolved.
- Be concise and state facts (amounts, statuses, ticket IDs) explicitly."""


# --- Tool execution: the single catch point ---------------------------------
# Returns (content, is_error): content is ALWAYS a JSON string so Claude sees
# one consistent shape; is_error maps onto tool_result.is_error.
#   unknown tool         -> VALIDATION (checked first; no point gating a ghost)
#   bad/missing kwargs   -> VALIDATION via signature.bind() — NOT a blanket
#                           `except TypeError`, which would also swallow real
#                           bugs inside the function and blame Claude for them
#   hook block           -> POLICY_BLOCKED (ticket already filed by the hook)
#   backend ToolError    -> its own category
#   anything else        -> INTERNAL to Claude; full traceback to stderr for devs
def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:  # executes ONE tool call requested by Claude
    log.info("tool call: %s input=%s", name, log_config.dump(tool_input))  # what Claude asked to run
    try:  # everything below may raise; the excepts turn it into a tool_result
        fn = backend.TOOL_FUNCTIONS.get(name)  # find the Python function for this tool name
        if fn is None:  # Claude asked for a tool we don't have
            raise ToolError(  # tell it which tools exist
                ErrorCategory.VALIDATION,  # bad input
                f"No tool named '{name}'. Available: {', '.join(backend.TOOL_FUNCTIONS)}.",  # list valid names
            )
        if not isinstance(tool_input, dict):  # arguments must be a JSON object (dict)
            raise ToolError(ErrorCategory.VALIDATION, "Tool input must be a JSON object.")  # reject other shapes

        try:  # argument check isolated so only bind() errors count as "bad args"
            inspect.signature(fn).bind(**tool_input)  # checks args WITHOUT running fn
        except TypeError as e:  # missing / unexpected / misspelled argument
            raise ToolError(ErrorCategory.VALIDATION, f"Invalid arguments for {name}: {e}.")  # report it to Claude

        hooks.pre_tool_use(name, tool_input)  # may raise POLICY_BLOCKED / VALIDATION
        result = fn(**tool_input)  # actually run the tool
        log.info("tool ok: %s result=%s", name, log_config.dump(result))  # exactly what goes back to Claude
        return json.dumps(result), False  # success: JSON result, not an error

    except ToolError as e:  # expected failure from validation, hook, or backend
        log.info("tool error: %s -> %s", name, log_config.dump(e.to_dict()))  # expected outcome (INFO, not WARNING): sent to Claude
        return json.dumps(e.to_dict()), True  # structured error, flagged is_error
    except Exception:  # anything else = a bug on our side
        log.exception("tool crashed (INTERNAL): %s", name)  # full traceback into the log file
        traceback.print_exc(file=sys.stderr)  # for developers, never sent to Claude
        err = ToolError(ErrorCategory.INTERNAL, "Unexpected server error. Do not retry; escalate if needed.")  # bland message
        return json.dumps(err.to_dict()), True  # flagged is_error, no internals leaked


# --- The loop ---------------------------------------------------------------
def _text_of(content) -> str:  # extracts the human-readable reply from a response's blocks
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()  # join text blocks only


# Returns {text, stop, iterations, messages}. Every exit path names itself in
# `stop`, so tests can assert WHY the agent stopped, not just what it said.
#   client  — injectable: tests pass a scripted fake (no API, no cost,
#             deterministic). Real client is created lazily, only if omitted.
#   history — pass a previous run's `messages` to continue the SAME
#             conversation (e.g. "any update?" still sees the ticket ID).
def run_agent(user_message: str, client=None, history: list | None = None, verbose: bool = True) -> dict:  # one customer turn
    client = client or anthropic.Anthropic()  # use the injected (fake) client, else create a real one now
    messages = list(history or []) + [{"role": "user", "content": user_message}]  # prior conversation + new message
    run_id = log_config.new_run_id()  # every log line for this question now carries run=<id>
    log.info("=== new question (history=%d msgs): %s", len(history or []), user_message)  # what the customer asked

    def done(stop: str, text: str, i: int) -> dict:  # single exit helper so every return has the same shape
        log.info("=== done: stop=%s api_calls=%d run=%s reply=%s", stop, i, run_id, text)  # final outcome
        if verbose:  # optional progress output
            print(f"  [stop] {stop} after {i} API call(s)")  # show why and when the loop ended
        return {"text": text, "stop": stop, "iterations": i, "messages": messages}  # result + full history

    for i in range(1, MAX_ITERATIONS + 1):  # bounded loop: at most MAX_ITERATIONS API calls
        # Full request. tools/system are identical every call -> full text on call 1 only (keeps the log readable).
        if i == 1:
            log.debug("API request #1 system=%s", log_config.dump(SYSTEM_PROMPT))  # behaviour rules sent
            log.debug("API request #1 tools=%s", log_config.dump(TOOLS))  # full tool schemas sent
        log.info("API request #%d -> model=%s effort=%s max_tokens=%d messages=%d",
                 i, MODEL, EFFORT, MAX_TOKENS, len(messages))  # one-line summary
        log.debug("API request #%d messages=%s", i, log_config.dump(messages))  # entire conversation sent
        response = client.messages.create(  # ask Claude for its next step
            model=MODEL,  # which Claude model
            max_tokens=MAX_TOKENS,  # cap on reply length
            system=SYSTEM_PROMPT,  # behaviour rules
            tools=TOOLS,  # the tool menu
            messages=messages,  # the whole conversation so far (API is stateless)
            # SDK 0.73 predates a typed `output_config` kwarg -> send via extra_body.
            extra_body={"output_config": {"effort": EFFORT}},  # thinking/effort level
        )
        # Append the FULL content every time (tool_use + thinking blocks too):
        # trimming to text would corrupt history for the next call.
        messages.append({"role": "assistant", "content": response.content})  # record Claude's turn
        stop = response.stop_reason  # why Claude stopped generating
        usage = getattr(response, "usage", None)  # token counts (absent on scripted fakes)
        log.info("API response #%d <- stop_reason=%s blocks=%s usage=%s request_id=%s",
                 i, stop, [getattr(b, "type", "?") for b in response.content],
                 log_config.dump(usage) if usage else None,
                 getattr(response, "_request_id", None))  # summary: why it stopped, what it sent, cost
        log.debug("API response #%d content=%s", i, log_config.dump(response.content))  # every block in full

        if stop == "tool_use":  # Claude wants one or more tools run
            results = []  # tool_result blocks to send back
            for block in response.content:  # in order: tools mutate state
                if getattr(block, "type", None) != "tool_use":  # skip text/thinking blocks
                    continue  # move to the next block
                content, is_error = run_tool(block.name, block.input)  # execute via hook -> backend
                if verbose:  # optional trace of each tool call
                    flag = "ERROR" if is_error else "ok"  # short status label
                    print(f"  [tool] {block.name}({json.dumps(block.input)}) -> {flag}: {content[:140]}")  # call + result preview
                results.append({  # build the tool_result Claude will read
                    "type": "tool_result",  # block type the API expects
                    "tool_use_id": block.id,  # must match the tool_use block
                    "content": content,  # JSON result or structured error
                    "is_error": is_error,  # tells Claude whether it failed
                })
            # ALL results in ONE user message (splitting discourages parallel calls).
            messages.append({"role": "user", "content": results})  # results go back as the user turn
            continue  # loop: let Claude react to the results

        if stop == "end_turn":  # Claude finished its answer
            return done("end_turn", _text_of(response.content), i)  # return the final reply

        if stop == "pause_turn":  # server-tool pause (we have none); documented fix: resend
            continue  # resend the conversation to let it continue

        if stop == "max_tokens":  # reply was cut off by the length cap
            # Truncated — a cut-off tool_use has partial input and must NOT be run.
            return done("max_tokens", _text_of(response.content), i)  # stop and report truncation

        if stop == "refusal":  # model declined for safety reasons
            return done("refusal", _text_of(response.content), i)  # report it, never pretend success

        return done(f"unexpected:{stop}", _text_of(response.content), i)  # any unknown reason is surfaced, not ignored

    return done("max_iterations", "", MAX_ITERATIONS)  # runaway guard tripped


# --- Entry point (ALWAYS the real API; mock vs --live lives in test_scenarios.py)
#   python3 agent.py "Refund ORD-10001, it arrived broken"   -> one request
#   python3 agent.py                                         -> interactive chat;
#       `messages` carried across turns, so follow-ups see earlier tickets.
# No up-front ANTHROPIC_API_KEY check: the SDK also resolves other credential
# sources (e.g. an `ant auth login` profile). SDK errors are caught specific-first.
# The SDK does not read .env — export the key in your own shell.
def _turn(prompt: str, client, history: list | None) -> list | None:  # run one CLI turn and print the outcome
    seen = len(backend.ESCALATION_QUEUE)  # remember queue size so we can show only NEW tickets
    try:  # API problems are caught below, most specific first
        log.info("Calling run_agent in agent.py with arguments - ",prompt, ", anthropic client and history - ",history)
        result = run_agent(prompt, client=client, history=history)  # run the agent loop
    except anthropic.AuthenticationError:  # bad or missing credentials
        log.error("API authentication failed")  # never logs the key itself
        print("! Authentication failed: export ANTHROPIC_API_KEY (or run `ant auth login`).")  # how to fix
        return history  # keep the conversation unchanged
    except anthropic.APIConnectionError:  # network problem
        log.error("API connection failed", exc_info=True)  # traceback into the log file
        print("! Could not reach the API (network). Try again.")  # transient; user can retry
        return history  # keep the conversation unchanged
    except anthropic.APIStatusError as e:  # any other HTTP error from the API
        log.error("API status error %s: %s", e.status_code, e.message)  # status + server message
        print(f"! API error {e.status_code}: {e.message}")  # show status code + message
        return history  # keep the conversation unchanged

    print(f"\nagent> {result['text'] or '(no text)'}")  # show Claude's reply
    log.info(f"Claude's reply - {result['text'] or '(no text)'}")
    for t in backend.ESCALATION_QUEUE[seen:]:  # tickets filed during this turn
        log.info(f"tickets filed during this turn - [ticket] {t['ticket_id']} ({t['reason_category']}) {t['summary'][:100]}")
        print(f"  [ticket] {t['ticket_id']} ({t['reason_category']}) {t['summary'][:100]}")  # one line per new ticket
    log.info("Returning messages from the result - run_agent function - ",result["messages"])
    return result["messages"]  # updated history for the next turn


if __name__ == "__main__":  # only when run as a script, not when imported by tests
    from dotenv import load_dotenv  # python-dotenv: loads KEY=value lines from the project's secrets file
    load_dotenv()  # searches up from this folder; puts ANTHROPIC_API_KEY into the environment for the SDK
    log_file = log_config.setup_logging()  # entry point decides destinations: full detail to logs/agent.log
    print(f"(logging everything to {log_file})")  # tell the user where to look
    client = anthropic.Anthropic()  # one real client reused for every turn
    if len(sys.argv) > 1:  # a prompt was passed on the command line
        log.info("Calling _turn from If, with argument - "," ".join(sys.argv[1:]), "and anthropic client and None")
        _turn(" ".join(sys.argv[1:]), client, None)  # one-shot request, fresh conversation
    else:  # no prompt -> interactive chat
        print(f"Support agent ({MODEL}). Empty line or 'exit' to quit.")  # usage banner
        history = None  # conversation starts empty
        while True:  # keep chatting until the user quits
            try:  # reading input can be interrupted
                prompt = input("\nyou> ").strip()  # read the next customer message
            except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C
                break  # leave the chat cleanly
            if prompt in ("", "exit", "quit"):  # explicit quit commands
                break  # leave the chat
            log.info("Calling _turn from If, with argument - prompt - ",prompt, "and anthropic client and history - ",history)
            history = _turn(prompt, client, history)  # run the turn, carry history forward
