"""
hooks.py — Interceptor that runs BEFORE every tool call the agent makes.

Same idea as a Claude Code PreToolUse hook (cf. .claude/hooks/block_env.py),
except our own agent.py invokes it, between "Claude requested a tool" and
backend.TOOL_FUNCTIONS[name](**input).

Contract: pre_tool_use(tool_name, tool_input)
    returns None            -> allow
    raises ToolError(...)   -> block (agent.py's single `except ToolError`
                               handles hook blocks and backend errors alike)

Policy lives HERE, not in backend.py: backend enforces what's possible, the
hook enforces what the agent is allowed to do on its own.
"""

import logging  # policy decisions are logged; destinations configured in log_config

import backend  # used to look up order totals and to file escalation tickets
from errors import ErrorCategory, ToolError  # raising ToolError is how the hook blocks a call

log = logging.getLogger(__name__)  # this module's logger ("hooks")

# --- Policy threshold -------------------------------------------------------
# Single source of truth (tools.py descriptions deliberately omit the number).
# Strictly ABOVE blocks: exactly $500.00 is allowed (`>`, not `>=`), matching
# the exercise wording "above a threshold". ORD-10005 ($500.00) tests this edge.
REFUND_APPROVAL_THRESHOLD = 500.00  # refunds strictly above this need a human


# --- Effective amount -------------------------------------------------------
# Closes the bypass: tool_input.get("amount", 0) would read an omitted amount
# (= FULL refund) as 0 and let it through. Instead compute what WILL be refunded.
#   - amount given, numeric      -> use it
#   - amount given, non-numeric  -> VALIDATION (can't compare it to a threshold)
#   - amount omitted             -> look up order_total via backend.get_order
# Conservative on purpose: get_order hides refunded_total, so an omitted amount
# is judged against the FULL total, not what's left. Overestimating is the safe
# direction (worst case: an unnecessary human review, never a missed one).
# If the lookup fails (timeout/not found), its ToolError propagates: the refund
# does NOT run (fail closed) and Claude gets the accurate reason.
def _effective_refund_amount(tool_input: dict) -> float:  # how much WOULD this call refund?
    amount = tool_input.get("amount")  # None when Claude omitted it (= full refund)
    if amount is not None:  # Claude supplied an explicit amount
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):  # not a real number (bool sneaks in as int)
            raise ToolError(  # can't compare a non-number to the threshold -> reject
                ErrorCategory.VALIDATION,  # bad input
                f"Refund amount must be a number in USD; got {amount!r}.",  # echo the bad value
            )
        return float(amount)  # explicit amount is the effective amount

    order = backend.get_order(tool_input.get("order_id"))  # may raise ToolError
    return float(order["order_total"])  # omitted amount -> judge against the full order total


# --- The interceptor --------------------------------------------------------
# Over threshold: the hook FILES the escalation itself (deterministic — every
# block reaches a human), then raises POLICY_BLOCKED. agent.py appends that
# error as a tool_result to `messages`; since the API is stateless and the full
# history is resent every call, this description IS Claude's lasting memory of
# the escalation — so it carries the ticket ID and what to do/not do next.
def pre_tool_use(tool_name: str, tool_input: dict) -> None:  # called by agent.run_tool before EVERY tool runs
    if tool_name != "process_refund":  # only refunds move money
        log.debug("hook skip: %s is not gated", tool_name)  # record that the gate saw it
        return  # read-only / escalation tools carry no money risk

    amount = _effective_refund_amount(tool_input)  # may raise (fail closed)
    source = "given" if tool_input.get("amount") is not None else "looked up (amount omitted)"  # explain the number
    log.info("hook check: process_refund %s effective_amount=%.2f (%s) threshold=%.2f",
             tool_input.get("order_id"), amount, source, REFUND_APPROVAL_THRESHOLD)  # the inputs to the decision
    if amount <= REFUND_APPROVAL_THRESHOLD:  # at or under the limit
        log.info("hook ALLOW: %.2f <= %.2f", amount, REFUND_APPROVAL_THRESHOLD)  # decision on record
        return  # allow

    order_id = tool_input.get("order_id")  # which order the blocked refund was for

    # Enrich the ticket; if the lookup fails, escalate anyway with what we have
    # (the safety net must not fail).
    try:  # best-effort lookup for ticket details
        order = backend.get_order(order_id)  # status/total/email for the human
    except ToolError:  # lookup failed (timeout / not found)
        order = {}  # carry on with no extra details
    facts = f"status {order.get('status', 'unknown')}"  # always include a status (or 'unknown')
    if "order_total" in order:  # total is only known if the lookup worked
        facts += f", order total ${order['order_total']:.2f}"  # add it to the facts line

    ticket = backend.escalate_to_human(  # file the ticket ourselves (deterministic routing)
        reason_category="refund_over_limit",  # fixed category for policy blocks
        summary=(  # factual, standalone note for a human with no context
            f"Agent attempted a ${amount:.2f} refund on {order_id} "  # what was tried
            f"(reason: {tool_input.get('reason', 'unspecified')}); {facts}. "  # why + verified facts
            f"Blocked by policy: refunds above ${REFUND_APPROVAL_THRESHOLD:.2f} need "  # which rule fired
            "human approval. Auto-filed by the refund policy hook; NO refund was issued."  # nothing to undo
        ),
        order_id=order_id,  # link ticket to the order
        customer_email=order.get("customer_email"),  # contact, if the lookup found it
        requested_amount=amount,  # the amount awaiting approval
    )
    log.info("hook BLOCK: %.2f > %.2f -> escalated as %s", amount, REFUND_APPROVAL_THRESHOLD, ticket["ticket_id"])  # decision + ticket

    raise ToolError(  # block the call; this message becomes Claude's memory of the escalation
        ErrorCategory.POLICY_BLOCKED,  # not retryable
        f"Refund NOT issued: ${amount:.2f} exceeds the ${REFUND_APPROVAL_THRESHOLD:.2f} "  # what happened and why
        f"limit for automatic refunds. It has ALREADY been escalated to a human as "  # escalation already done
        f"ticket {ticket['ticket_id']}. Do NOT call escalate_to_human for this refund, "  # avoid duplicate tickets
        "and do NOT retry it or split it into smaller refunds on this order. Tell the "  # avoid retries/splits
        f"customer their request is with a human agent (ticket {ticket['ticket_id']}) "  # what to tell the customer
        "who will follow up; do not say it is resolved.",  # honest status
    )
