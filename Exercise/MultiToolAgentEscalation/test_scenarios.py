"""
test_scenarios.py — Contract checks + agent scenarios.

Two modes, because they prove different things:

  default (scripted fake client)        --live (real Claude API)
  ----------------------------------    ------------------------------------
  Claude's choices are SCRIPTED         Claude's choices are REAL
  proves OUR code: loop, hook,          proves Claude's TOOL SELECTION and
  backend, errors — given those choices behaviour with our descriptions
  free, deterministic                   costs money, can vary run to run
  asserts EXACT paths / errors          asserts END STATE only (refunded?
                                        blocked? ticket filed?), never the
                                        exact tool sequence

Run:  python3 test_scenarios.py          # fake client
      python3 test_scenarios.py --live   # real API (needs ANTHROPIC_API_KEY)

Plain asserts + a tiny runner (no pytest) to stay dependency-free.
"""

import copy  # snapshot/restore the fake DB and freeze recorded requests
import inspect  # read function signatures for the contract check
import itertools  # endless counter for unique fake tool_use IDs
import json  # decode tool_result contents for assertions
from types import SimpleNamespace  # lightweight stand-ins for SDK response/block objects

import agent  # the loop under test (run_agent, MAX_ITERATIONS, MODEL)
import backend  # fake DB + queue: state to reset and to assert on
from tools import TOOLS  # schemas, compared against backend functions

# --- State reset ------------------------------------------------------------
# Backend state is module-global and scenarios mutate it; without a reset,
# scenario N would see scenario N-1's refunds (order-dependent = flaky tests).
# Mutate IN PLACE (.clear/.update) so anything already holding a reference to
# ORDERS / ESCALATION_QUEUE sees the reset too.
_ORDERS_SNAPSHOT = copy.deepcopy(backend.ORDERS)  # pristine copy taken once at import time


def reset_state() -> None:  # restore backend to its initial state before each scenario
    backend.ORDERS.clear()  # empty the orders dict (same object, so references stay valid)
    backend.ORDERS.update(copy.deepcopy(_ORDERS_SNAPSHOT))  # refill with fresh copies of the originals
    backend.ESCALATION_QUEUE.clear()  # drop all tickets
    backend._refund_counter = 0  # refund IDs restart at RF-0001
    backend._escalation_counter = 0  # ticket IDs restart at ESC-0001


# --- Contract checks: the "dotted line" between tools.py and backend.py ------
# Nothing in the code forces schemas and functions to agree; these do.
def _schema(name: str) -> dict:  # fetch one tool's input_schema by name
    return next(t for t in TOOLS if t["name"] == name)["input_schema"]  # first (only) matching schema


def check_contracts() -> None:  # fail fast if tools.py and backend.py have drifted apart
    # 1. Same tool names on both sides.
    schema_names = {t["name"] for t in TOOLS}  # names Claude is told about
    assert schema_names == set(backend.TOOL_FUNCTIONS), (  # must equal names we can actually run
        f"name drift: schemas={schema_names} functions={set(backend.TOOL_FUNCTIONS)}"  # show both sides on failure
    )

    # 2. Schema property names == function parameter names (catches orderId vs order_id).
    for name, fn in backend.TOOL_FUNCTIONS.items():  # every tool function
        props = set(_schema(name)["properties"])  # argument names the schema advertises
        params = set(inspect.signature(fn).parameters)  # argument names the function accepts
        assert props == params, f"{name}: schema props {props} != function params {params}"  # must match exactly

    # 3. Duplicated enums agree.
    pairs = [  # (tool, schema field, backend's set of allowed values)
        ("search_customer_orders", "status", backend.VALID_STATUSES),  # status filter values
        ("process_refund", "reason", backend.VALID_REASONS),  # refund reason codes
        ("escalate_to_human", "reason_category", backend.VALID_ESCALATION_REASONS),  # escalation categories
    ]
    for tool, field, backend_values in pairs:  # check each duplicated list
        schema_values = set(_schema(tool)["properties"][field]["enum"])  # values the schema allows
        assert schema_values == backend_values, (  # must equal what the backend accepts
            f"{tool}.{field} enum drift: schema={schema_values} backend={backend_values}"  # show the difference
        )


# --- Scripted fake client ---------------------------------------------------
# Duck typing: run_agent only reads .type/.name/.input/.id/.text on blocks and
# .stop_reason/.content on responses, so SimpleNamespace stand-ins suffice
# (vs. real SDK block classes: exact but verbose and tied to SDK internals).
_ids = itertools.count(1)  # source of unique numbers for fake tool_use IDs


def tool(name: str, **tool_input) -> SimpleNamespace:  # fake tool_use block, as if Claude called `name`
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids):03d}", name=name, input=tool_input)  # same attrs as SDK block


def text(t: str) -> SimpleNamespace:  # fake text block, as if Claude said `t`
    return SimpleNamespace(type="text", text=t)  # same attrs as SDK TextBlock


# stop_reason is DERIVED like the real API: any tool_use block -> "tool_use",
# else "end_turn". A script can't contradict itself.
def turn(*blocks) -> SimpleNamespace:  # fake API response made of the given blocks
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"  # derive stop_reason from content
    return SimpleNamespace(stop_reason=stop, content=list(blocks))  # same attrs as an SDK Message


# Same call path as the SDK (client.messages.create), so run_agent uses it
# unchanged. Records every request; running past the script fails loudly
# (= the loop made more API calls than the scenario expects).
class ScriptedClient:  # drop-in replacement for anthropic.Anthropic in tests
    def __init__(self, script: list):  # script = the responses to return, in order
        self.script = list(script)  # own copy of the scripted responses
        self.requests: list[dict] = []  # every request the loop sent, for later inspection
        self.messages = self  # so client.messages.create(...) resolves here

    def create(self, **request):  # mimics client.messages.create(**kwargs)
        self.requests.append(copy.deepcopy(request))  # freeze a copy (messages list keeps mutating afterwards)
        if len(self.requests) > len(self.script):  # loop asked for more turns than scripted
            raise AssertionError(  # fail loudly: unexpected extra API call
                f"loop made {len(self.requests)} API calls; script has only {len(self.script)}"  # counts for debugging
            )
        return self.script[len(self.requests) - 1]  # hand back the next scripted response


# Every tool_result in the conversation, JSON-decoded, in order — so asserts
# read like results[1]["errorCategory"] == "policy_blocked".
def tool_results(result: dict) -> list[dict]:  # flatten all tool results out of the history
    out = []  # collected, decoded results
    for m in result["messages"]:  # walk the conversation
        if m["role"] == "user" and isinstance(m["content"], list):  # tool results live in list-content user turns
            for block in m["content"]:  # each block in that turn
                if isinstance(block, dict) and block.get("type") == "tool_result":  # keep only tool_result blocks
                    out.append({**json.loads(block["content"]), "_is_error": block["is_error"]})  # decoded JSON + error flag
    return out  # in conversation order


# Every tool call Claude made, as (name, input), in order. Works for both fake
# SimpleNamespace blocks and real SDK blocks (same attribute names).
def tool_calls(result: dict) -> list[tuple[str, dict]]:  # which tools Claude chose, with arguments
    return [  # build the list in conversation order
        (b.name, b.input)  # tool name + arguments
        for m in result["messages"] if m["role"] == "assistant"  # only Claude's turns
        for b in m["content"] if getattr(b, "type", None) == "tool_use"  # only tool_use blocks
    ]


def _run(script: list, prompt: str = "test", history=None):  # run the agent against a scripted fake
    client = ScriptedClient(script)  # fake client playing the script
    return agent.run_agent(prompt, client=client, history=history, verbose=False), client  # result + client for request checks


# --- Mock scenarios: exact paths, given scripted choices ---------------------
# Each scenario: scripted conversation + asserts on END STATE and tool results.
def s01_simple_lookup():  # basic loop: one tool call, then a final answer
    r, _ = _run([turn(tool("get_order", order_id="ORD-10001")), turn(text("Delivered."))])  # script: lookup, then reply
    assert r["stop"] == "end_turn" and r["iterations"] == 2  # ended normally after exactly 2 API calls
    assert tool_results(r)[0]["status"] == "delivered"  # the real backend result reached the conversation


def s02_multi_aspect_parallel():  # the exercise's multi-aspect request
    # Jane: find orders, refund mug (ok) AND chair (blocked) in ONE parallel turn.
    r, client = _run([  # script of Claude's choices
        turn(tool("search_customer_orders", customer_email="jane@example.com")),  # step 1: find her orders
        turn(tool("process_refund", order_id="ORD-10001", reason="damaged"),  # step 2a: refund mug...
             tool("process_refund", order_id="ORD-10002", reason="damaged")),  # ...2b: and chair, in parallel
        turn(text("Mug refunded; chair escalated as ESC-0001.")),  # step 3: final reply
    ])
    res = tool_results(r)  # all three tool results, in order
    assert res[0]["count"] == 3  # search found Jane's 3 orders
    assert res[1]["refund_id"] == "RF-0001" and not res[1]["_is_error"]  # mug refund succeeded
    assert res[2]["errorCategory"] == "policy_blocked" and res[2]["isRetryable"] is False  # chair blocked by the hook
    # both parallel results went back in ONE user message
    assert len(client.requests[2]["messages"][-1]["content"]) == 2  # third request's last message held 2 results
    assert backend.ORDERS["ORD-10001"]["status"] == "refunded"  # mug really refunded in the DB
    assert backend.ORDERS["ORD-10002"]["refunded_total"] == 0.0  # chair: no money moved
    assert [t["reason_category"] for t in backend.ESCALATION_QUEUE] == ["refund_over_limit"]  # exactly one ticket, from the hook


def s03_omitted_amount_bypass_closed():  # full refund with no amount must not slip past the hook
    r, _ = _run([turn(tool("process_refund", order_id="ORD-10002", reason="changed_mind")), turn(text("ok"))])  # no amount given
    assert tool_results(r)[0]["errorCategory"] == "policy_blocked"  # hook still caught it
    assert backend.ORDERS["ORD-10002"]["refunded_total"] == 0.0          # no money moved
    assert backend.ESCALATION_QUEUE[0]["requested_amount"] == 820.0      # hook looked up total


def s04_boundary_exactly_500_allowed():  # `>` not `>=`: exactly $500 passes
    r, _ = _run([turn(tool("process_refund", order_id="ORD-10005", reason="damaged")), turn(text("ok"))])  # $500 order, full refund
    assert tool_results(r)[0]["amount_refunded"] == 500.0  # refund went through
    assert backend.ESCALATION_QUEUE == []  # and nothing was escalated


def s05_partial_under_limit_on_big_order():  # a small partial refund on a big order is fine
    r, _ = _run([turn(tool("process_refund", order_id="ORD-10002", reason="damaged", amount=100)), turn(text("ok"))])  # $100 of $820
    assert tool_results(r)[0]["remaining_refundable"] == 720.0  # refund applied, $720 left
    assert backend.ESCALATION_QUEUE == []  # under the limit -> no ticket


def s06_pending_order_not_escalated():  # state conflicts are validation errors, not escalations
    r, _ = _run([turn(tool("process_refund", order_id="ORD-10004", reason="other")), turn(text("ok"))])  # pending order
    assert tool_results(r)[0]["errorCategory"] == "validation"   # state conflict, not policy
    assert backend.ESCALATION_QUEUE == []  # no human needed


def s07_timeout_retry_then_escalate():  # retryable error path, ending in a tool_failure ticket
    r, _ = _run([  # script of Claude's choices
        turn(tool("get_order", order_id="ORD-99999")),  # first try times out
        turn(tool("get_order", order_id="ORD-99999")),  # retry once (isRetryable)
        turn(tool("escalate_to_human", reason_category="tool_failure", order_id="ORD-99999",  # give up and hand off
                  summary="Customer asked about ORD-99999; order DB timed out twice; no action taken.")),  # standalone summary
        turn(text("A human will follow up.")),  # final reply
    ])
    res = tool_results(r)  # the three tool results
    assert res[0]["errorCategory"] == "transient" and res[0]["isRetryable"] is True  # timeout flagged retryable
    assert res[2]["status"] == "queued"  # escalation accepted
    assert backend.ESCALATION_QUEUE[0]["reason_category"] == "tool_failure"  # ticket has the right category


def s08_validation_then_self_correct():  # Claude fixes bad input after a validation error
    r, _ = _run([  # script of Claude's choices
        turn(tool("get_order", order_id="10001")),  # malformed ID
        turn(tool("get_order", order_id="ORD-10001")),  # corrected ID
        turn(text("Found it.")),  # final reply
    ])
    res = tool_results(r)  # both tool results
    assert res[0]["errorCategory"] == "validation" and "ORD-" in res[0]["description"]  # error explained the format
    assert res[1]["order_id"] == "ORD-10001"  # retry with the fix succeeded


def s09_wrong_tool_redirected():  # picking the wrong sibling tool gets redirected
    r, _ = _run([  # script of Claude's choices
        turn(tool("get_order", order_id="jane@example.com")),  # wrong tool for an email
        turn(tool("search_customer_orders", customer_email="jane@example.com")),  # right tool
        turn(text("You have 3 orders.")),  # final reply
    ])
    res = tool_results(r)  # both tool results
    assert "search_customer_orders" in res[0]["description"]   # error points to the right tool
    assert res[1]["count"] == 3  # the correct tool worked


def s10_conversation_continuity():  # a follow-up turn still sees the earlier ticket
    r1, _ = _run([turn(tool("process_refund", order_id="ORD-10002", reason="damaged")),  # turn 1: blocked refund...
                  turn(text("Escalated as ESC-0001."))], prompt="refund my chair")  # ...and a reply
    r2, client2 = _run([turn(text("Your ticket is ESC-0001."))],  # turn 2: answer from memory, no tools
                       prompt="what's my ticket number?", history=r1["messages"])  # carry turn 1's history
    # the follow-up REQUEST carried the earlier tool_result containing the ticket ID
    sent = json.dumps([m["content"] for m in client2.requests[0]["messages"] if m["role"] == "user"], default=str)  # all user content sent
    assert "ESC-0001" in sent  # ticket ID was in what Claude received
    assert tool_calls(r2) == tool_calls(r1)   # no NEW tool calls in turn 2
    assert len(backend.ESCALATION_QUEUE) == 1  # still only the one ticket


def s11_unknown_tool():  # a made-up tool name is a validation error, not a crash
    r, _ = _run([turn(tool("delete_order", order_id="ORD-10001")), turn(text("Can't do that."))])  # nonexistent tool
    assert tool_results(r)[0]["errorCategory"] == "validation"  # handled gracefully


def s12_runaway_guard():  # a model that never stops calling tools is cut off
    r, _ = _run([turn(tool("get_order", order_id="ORD-99999"))] * agent.MAX_ITERATIONS)  # endless tool calls
    assert r["stop"] == "max_iterations" and r["iterations"] == agent.MAX_ITERATIONS  # stopped at the cap


# The list the runner executes in default (fake-client) mode.
MOCK_SCENARIOS = [s01_simple_lookup, s02_multi_aspect_parallel, s03_omitted_amount_bypass_closed,
                  s04_boundary_exactly_500_allowed, s05_partial_under_limit_on_big_order,
                  s06_pending_order_not_escalated, s07_timeout_retry_then_escalate,
                  s08_validation_then_self_correct, s09_wrong_tool_redirected,
                  s10_conversation_continuity, s11_unknown_tool, s12_runaway_guard]


# --- Live scenarios: END STATE + tool CHOICE only ----------------------------
# Never assert exact sequences (Claude may legitimately verify first one run
# and not the next). Exception on purpose: L2/L3 assert WHICH of the similar
# pair was chosen — that choice is what the pair exists to test.
# Accepted gaps (duplicate ticket, split refund) are WARNings, not failures.
def _live(prompt: str, client):  # run one real-API turn and show it
    print(f"\n  you> {prompt}")  # echo the customer message
    r = agent.run_agent(prompt, client=client, verbose=True)  # real loop, with tool trace printed
    print(f"  agent> {r['text'][:300]}")  # preview Claude's reply
    return r  # result for the caller's assertions


def l1_multi_aspect(client):  # real model on the 3-part Jane request
    _live("Hi, I'm jane@example.com. Can you check my orders, refund the mug since it "  # part 1 + part 2
          "arrived chipped, and also refund the chair, it's broken.", client)  # part 3
    assert backend.ORDERS["ORD-10001"]["status"] == "refunded", "mug should be refunded"  # end state: mug refunded
    chair_tickets = [t for t in backend.ESCALATION_QUEUE if t["order_id"] == "ORD-10002"]  # tickets about the chair
    assert chair_tickets, "chair should have been escalated"  # end state: chair went to a human
    if backend.ORDERS["ORD-10002"]["refunded_total"] > 0:  # accepted gap #1 happened
        print(f"  WARN accepted gap: chair partially refunded ${backend.ORDERS['ORD-10002']['refunded_total']} (split bypass)")  # report, don't fail
    if len(chair_tickets) > 1:  # accepted gap #2 happened
        print(f"  WARN accepted gap: {len(chair_tickets)} tickets for the chair (duplicate escalation)")  # report, don't fail


def l2_selection_by_id(client):  # tool selection: an ID should pick get_order
    r = _live("What's the status of order ORD-10003?", client)  # prompt contains an order ID
    names = [n for n, _ in tool_calls(r)]  # tools Claude actually chose
    assert "get_order" in names and "search_customer_orders" not in names, f"picked {names}"  # right sibling, never the other


def l3_selection_by_email(client):  # tool selection: an email should pick search_customer_orders
    r = _live("I'm sam@example.com — what orders do I have?", client)  # prompt contains only an email
    names = [n for n, _ in tool_calls(r)]  # tools Claude actually chose
    assert "search_customer_orders" in names and "get_order" not in names[:1], f"picked {names}"  # search used; not get_order first


def l4_missing_info_asks(client):  # without an order, Claude should ask rather than act
    r = _live("Please refund my order.", client)  # no ID, no email
    assert "process_refund" not in [n for n, _ in tool_calls(r)], "must not refund without an order"  # no refund attempted
    assert all(o["refunded_total"] == _ORDERS_SNAPSHOT[k]["refunded_total"] for k, o in backend.ORDERS.items())  # DB untouched


def l5_boundary(client):  # real model on the exactly-$500 order
    _live("Please fully refund order ORD-10005, the monitor arrived cracked.", client)  # full refund of $500
    assert backend.ORDERS["ORD-10005"]["status"] == "refunded"  # it went through
    assert backend.ESCALATION_QUEUE == [], "exactly $500 must not escalate"  # and wasn't escalated


# The list the runner executes in --live mode.
LIVE_SCENARIOS = [l1_multi_aspect, l2_selection_by_id, l3_selection_by_email, l4_missing_info_asks, l5_boundary]


# --- Runner -----------------------------------------------------------------
# Contracts first (if they fail, nothing else is trustworthy), then
# reset_state() before EVERY scenario. Exit code 1 on any failure (CI-ready).
if __name__ == "__main__":  # only when run as a script
    import sys  # argv for --live, exit code at the end

    import logging  # scenario start/end markers in the log
    import log_config  # entry point configures logging

    # Separate file so test runs don't pollute logs/agent.log (the real-chat log).
    log_file = log_config.setup_logging(log_config.DEFAULT_LOG_FILE.with_name("tests.log"))
    tlog = logging.getLogger("test_scenarios")  # named explicitly (__name__ is "__main__" when run as a script)
    print(f"(logging everything to {log_file})")  # tell the user where to look

    live = "--live" in sys.argv  # choose mode from the command line
    check_contracts()  # schema/backend agreement first (raises on drift)
    print("contracts: PASS")  # reached only if the contract check passed

    if live:  # real-API mode
        import anthropic  # imported only here so fake mode needs no SDK credentials
        from dotenv import load_dotenv  # load the API key from the project's secrets file
        load_dotenv()  # searches up from this folder
        client = anthropic.Anthropic()  # one real client shared by all live scenarios
        scenarios = [(fn, (client,)) for fn in LIVE_SCENARIOS]  # each live scenario gets the client
        print(f"LIVE mode against {agent.MODEL} — real API calls, real cost")  # cost warning
    else:  # default fake-client mode
        scenarios = [(fn, ()) for fn in MOCK_SCENARIOS]  # mock scenarios take no arguments

    failed = 0  # running count of failures
    for fn, args in scenarios:  # run each scenario in turn
        reset_state()  # fresh backend so scenarios can't affect each other
        tlog.info("######## scenario START %s (%s)", fn.__name__, "live" if live else "mock")  # grep marker
        try:  # a scenario can pass, fail an assert, or crash
            fn(*args)  # run it
            print(f"PASS  {fn.__name__}")  # no exception -> pass
            tlog.info("######## scenario PASS %s", fn.__name__)  # result marker
        except AssertionError as e:  # an expectation wasn't met
            failed += 1  # count it
            print(f"FAIL  {fn.__name__}: {e}")  # show which and why
            tlog.error("######## scenario FAIL %s: %s", fn.__name__, e)  # result marker
        except Exception as e:  # a crashing scenario is one failure, not the end of the run
            failed += 1  # count it
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")  # show the crash type and message
            tlog.exception("######## scenario ERROR %s", fn.__name__)  # result marker + traceback

    print(f"\n{len(scenarios) - failed}/{len(scenarios)} passed")  # summary line
    sys.exit(1 if failed else 0)  # non-zero exit on any failure (CI gate)
