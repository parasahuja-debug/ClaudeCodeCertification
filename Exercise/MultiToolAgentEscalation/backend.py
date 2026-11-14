"""
backend.py — Fake order system behind the tools (stands in for a real DB +
payments service).

tools.py says WHAT Claude may call; this file DOES the work: validates inputs,
reads/writes the in-memory "database", and raises errors.ToolError on failure.
It knows nothing about policy thresholds — that's hooks.py's job, which runs
BEFORE anything here.

In-memory (not SQLite) so tests are deterministic and failures like timeouts
can be triggered on demand via a magic order ID.
"""

import copy  # deepcopy: hand out copies so callers can't mutate the fake DB
import logging  # function-level logs; destinations configured in log_config
import re  # regexes for order-ID and email format checks

from errors import ErrorCategory, ToolError  # the shared structured-error vocabulary

# Writes (refunds, tickets) log at INFO — they're what you audit; reads at DEBUG.
# Raised ToolErrors are NOT logged here: agent.run_tool logs them where caught (no duplicates).
log = logging.getLogger(__name__)  # this module's logger ("backend")

# --- Fake data --------------------------------------------------------------
# Keyed by order_id = primary key -> O(1) lookup for get_order. Email search is
# a full scan (a real DB would index customer_email; at 5 rows it's moot).
# Money is float for JSON simplicity; real systems use integer cents / Decimal.
# `refunded_total` is internal bookkeeping (not in any schema) so partial
# refunds can't exceed what's left on the order.
# Each order exists to trigger a specific test scenario (see right-hand notes).
ORDERS = {  # the in-memory "orders table": order_id -> order record
    "ORD-10001": {  # cheap -> full refund allowed
        "order_id": "ORD-10001",  # primary key, repeated inside the record for convenience
        "customer_email": "jane@example.com",  # owner; used by search_customer_orders
        "status": "delivered",  # lifecycle state; decides whether a refund is possible
        "order_total": 45.50,  # what the customer paid (USD)
        "items": [{"sku": "MUG-01", "name": "Ceramic mug", "qty": 1, "price": 45.50}],  # line items get_order returns
        "order_date": "2026-10-28",  # ISO date so text sorting = chronological sorting
        "refunded_total": 0.0,  # internal: how much has already been refunded
    },
    # The remaining orders use the same field layout; notes mark what each one tests.
    "ORD-10002": {  # over threshold -> full refund BLOCKED by hook; partial $100 OK
        "order_id": "ORD-10002",  # primary key
        "customer_email": "jane@example.com",  # Jane's second order (multi-order search)
        "status": "delivered",  # refundable state
        "order_total": 820.00,  # above $500 -> a full refund trips the hook
        "items": [{"sku": "CHAIR-07", "name": "Ergonomic chair", "qty": 1, "price": 820.00}],  # line items
        "order_date": "2026-11-02",  # newest of Jane's orders
        "refunded_total": 0.0,  # nothing refunded yet
    },
    "ORD-10003": {  # already refunded -> validation error
        "order_id": "ORD-10003",  # primary key
        "customer_email": "jane@example.com",  # Jane's third order
        "status": "refunded",  # terminal state -> refund attempts must fail
        "order_total": 60.00,  # paid amount
        "items": [{"sku": "LAMP-02", "name": "Desk lamp", "qty": 1, "price": 60.00}],  # line items
        "order_date": "2026-09-15",  # oldest of Jane's orders
        "refunded_total": 60.00,  # fully refunded already (nothing remaining)
    },
    "ORD-10004": {  # not shipped yet -> not refundable
        "order_id": "ORD-10004",  # primary key
        "customer_email": "sam@example.com",  # different customer
        "status": "pending",  # not shipped -> not a refundable state
        "order_total": 120.00,  # paid amount
        "items": [{"sku": "KB-11", "name": "Mechanical keyboard", "qty": 1, "price": 120.00}],  # line items
        "order_date": "2026-11-12",  # order date
        "refunded_total": 0.0,  # nothing refunded
    },
    "ORD-10005": {  # exactly at threshold -> boundary test (> vs >=)
        "order_id": "ORD-10005",  # primary key
        "customer_email": "sam@example.com",  # Sam's second order
        "status": "delivered",  # refundable state
        "order_total": 500.00,  # exactly the threshold -> proves `>` lets it through
        "items": [{"sku": "MON-27", "name": "27-inch monitor", "qty": 1, "price": 500.00}],  # line items
        "order_date": "2026-11-05",  # order date
        "refunded_total": 0.0,  # nothing refunded
    },
}

# Not stored in ORDERS: any call using this ID simulates a DB timeout
# (TRANSIENT, isRetryable=True) so the retry path is testable on demand.
TIMEOUT_ORDER_ID = "ORD-99999"  # magic ID that always "times out"

# Internal bookkeeping fields never returned to Claude (keeps output = schema promise).
_INTERNAL_FIELDS = {"refunded_total"}  # stripped from get_order's output


# --- Shared validation ------------------------------------------------------
# The deterministic check the schema's `pattern` can't guarantee (non-strict
# tool use treats schemas as guidance). Reused by process_refund.
def _validate_order_id(order_id) -> None:  # raises if the ID is malformed; returns nothing if OK
    if not isinstance(order_id, str) or not re.fullmatch(r"ORD-\d{5}", order_id):  # wrong type or wrong format
        raise ToolError(  # stop here and tell Claude how to fix the input
            ErrorCategory.VALIDATION,  # bad input -> not retryable as-is
            f"'{order_id}' is not a valid order ID; expected 'ORD-' followed by "  # echo the bad value + correct format
            "5 digits (e.g. 'ORD-10421'). If you only have an email, use "
            "search_customer_orders.",  # redirect to the sibling tool
        )


# --- Tool: get_order --------------------------------------------------------
# Check order: format (VALIDATION) -> simulated timeout (TRANSIENT) -> exists
# (NOT_FOUND). Returns a deep copy minus internal fields so callers can't mutate
# the "database" and Claude sees exactly what the schema description promises.
def get_order(order_id: str) -> dict:  # implementation behind the get_order tool
    log.debug("get_order(order_id=%r)", order_id)  # input as received
    _validate_order_id(order_id)  # 1) reject malformed IDs first (cheapest, most certain check)

    if order_id == TIMEOUT_ORDER_ID:  # 2) simulated outage for testing the retry path
        raise ToolError(  # report a temporary failure
            ErrorCategory.TRANSIENT,  # retryable category
            "Order database timed out. This is temporary; retrying the same call may succeed.",  # tells Claude a retry is OK
        )

    if order_id not in ORDERS:  # 3) well-formed but no such order
        raise ToolError(  # report "doesn't exist" (distinct from "malformed")
            ErrorCategory.NOT_FOUND,  # not retryable; ask the customer instead
            f"No order exists with ID {order_id}. Ask the customer to double-check it, "  # suggested next step
            "or look up their orders by email with search_customer_orders.",  # alternative tool
        )

    order = copy.deepcopy(ORDERS[order_id])  # copy so the caller can't change the stored record
    for field in _INTERNAL_FIELDS:  # walk the internal-only fields
        order.pop(field, None)  # remove each so output matches the schema's promise
    log.debug("get_order -> %s status=%s total=%.2f", order_id, order["status"], order["order_total"])  # what was found
    return order  # the public view of the order


# --- Tool: search_customer_orders -------------------------------------------
# Mirrors the enum in tools.py's schema; test_scenarios.py asserts they match
# (backend owns the domain values, so it doesn't import them from the schema).
VALID_STATUSES = {"pending", "shipped", "delivered", "cancelled", "refunded"}  # allowed status filter values

# Deliberately loose: catches "a name/ID where an email belongs", not RFC 5322.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")  # something@something.something


# Empty result = SUCCESS (count 0), not NOT_FOUND: "no orders" is a valid answer,
# and flagging it is_error could make Claude retry/escalate a normal outcome.
# Returns a dict (not a bare list) so count is explicit and fields can be added.
def search_customer_orders(customer_email: str, status: str | None = None) -> dict:  # implementation behind search tool
    log.debug("search_customer_orders(customer_email=%r, status=%r)", customer_email, status)  # input as received
    if not isinstance(customer_email, str) or not _EMAIL_RE.fullmatch(customer_email.strip()):  # not email-shaped
        raise ToolError(  # reject and point Claude at the right tool
            ErrorCategory.VALIDATION,  # bad input
            f"'{customer_email}' is not a valid email address. If you have an order "  # echo the bad value
            "ID instead (e.g. 'ORD-10421'), use get_order.",  # redirect to sibling tool
        )
    if status is not None and status not in VALID_STATUSES:  # a filter was given but isn't a known status
        raise ToolError(  # reject with the list of valid options
            ErrorCategory.VALIDATION,  # bad input
            f"Unknown status '{status}'. Use one of: {', '.join(sorted(VALID_STATUSES))}, "  # allowed values
            "or omit status to return all orders.",  # or drop the filter
        )

    email = customer_email.strip().lower()  # case-insensitive match
    matches = [  # collect this customer's orders (optionally filtered by status)
        o for o in ORDERS.values()  # full scan over every stored order
        if o["customer_email"].lower() == email and (status is None or o["status"] == status)  # owner match + filter
    ]
    # ISO 'YYYY-MM-DD' strings sort chronologically as plain text.
    matches.sort(key=lambda o: o["order_date"], reverse=True)  # newest first, as the description promises
    log.debug("search_customer_orders -> %d match(es): %s", len(matches), [o["order_id"] for o in matches])  # what was found

    return {  # wrapped result (not a bare list)
        "customer_email": email,  # echo the normalized email that was searched
        "count": len(matches),  # explicit count; 0 is a valid answer
        "orders": [  # summaries only, to keep context small
            {k: o[k] for k in ("order_id", "status", "order_total", "order_date")}  # pick summary fields
            for o in matches  # one summary per matched order
        ],
    }


# --- Tool: process_refund ---------------------------------------------------
# Backend enforces what's POSSIBLE (data integrity), NOT the $ threshold — that
# policy lives in hooks.py, which intercepts the agent's calls before they get
# here. Keeping it out means a human-approved large refund can still run.
REFUNDABLE_STATUSES = {"delivered", "shipped"}  # only these states can be refunded
VALID_REASONS = {"damaged", "not_received", "wrong_item", "changed_mind", "other"}  # allowed reason codes

_refund_counter = 0  # deterministic refund IDs (RF-0001...) for repeatable tests


def process_refund(order_id: str, reason: str, amount: float | None = None) -> dict:  # implementation behind refund tool
    global _refund_counter  # we increment the module-level counter below

    log.debug("process_refund(order_id=%r, reason=%r, amount=%r)", order_id, reason, amount)  # input as received
    _validate_order_id(order_id)  # reject malformed IDs (shared check)

    # NOTE: our simulated timeout fires BEFORE any write, so retrying is safe.
    # A real timeout could happen AFTER money moved -> a blind retry would
    # double-refund. Real payment APIs use idempotency keys to prevent that.
    if order_id == TIMEOUT_ORDER_ID:  # simulated payments outage
        raise ToolError(  # temporary failure, nothing written
            ErrorCategory.TRANSIENT,  # retryable
            "Payments service timed out before the refund was applied. No money moved; "  # reassures: no partial effect
            "retrying the same call may succeed.",
        )
    if order_id not in ORDERS:  # well-formed but unknown order
        raise ToolError(ErrorCategory.NOT_FOUND, f"No order exists with ID {order_id}.")  # report not found

    if reason not in VALID_REASONS:  # reason code must be one we recognise
        raise ToolError(  # reject with allowed values
            ErrorCategory.VALIDATION,  # bad input
            f"Unknown reason '{reason}'. Use one of: {', '.join(sorted(VALID_REASONS))}.",  # list valid reasons
        )

    order = ORDERS[order_id]  # real reference: this function is allowed to write

    # State conflict -> VALIDATION, not POLICY_BLOCKED (that would signal
    # "escalate", but a pending order needs no human).
    if order["status"] not in REFUNDABLE_STATUSES:  # e.g. pending / cancelled / already refunded
        raise ToolError(  # explain why it can't be refunded
            ErrorCategory.VALIDATION,  # not escalation-worthy
            f"Order {order_id} has status '{order['status']}' and cannot be refunded; "  # current state
            f"only {' or '.join(sorted(REFUNDABLE_STATUSES))} orders are refundable.",  # which states would work
        )

    remaining = round(order["order_total"] - order["refunded_total"], 2)  # money still refundable, in cents precision

    if amount is None:  # caller omitted amount
        amount = remaining  # omitted = refund everything left
    # bool is a subclass of int in Python, so exclude it explicitly.
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:  # not a positive real number
        raise ToolError(ErrorCategory.VALIDATION, f"Refund amount must be a positive number; got {amount!r}.")  # reject
    amount = round(float(amount), 2)  # compare in cents, sidestep float noise
    if amount > remaining:  # can't refund more than what's left
        raise ToolError(  # reject with the actual remaining figure
            ErrorCategory.VALIDATION,  # bad input
            f"Refund of ${amount:.2f} exceeds the ${remaining:.2f} still refundable on {order_id}.",  # shows the limit
        )

    # --- write ---
    order["refunded_total"] = round(order["refunded_total"] + amount, 2)  # record the money going back
    if order["refunded_total"] >= order["order_total"]:  # everything has now been refunded
        order["status"] = "refunded"  # partial refunds leave status unchanged

    _refund_counter += 1  # next sequential refund number
    log.info("REFUND APPLIED RF-%04d: %s amount=%.2f reason=%s refunded_total=%.2f status=%s",
             _refund_counter, order_id, amount, reason, order["refunded_total"], order["status"])  # audit: money moved
    return {  # receipt Claude reports back to the customer
        "refund_id": f"RF-{_refund_counter:04d}",  # e.g. RF-0001
        "order_id": order_id,  # which order was refunded
        "amount_refunded": amount,  # how much this call refunded
        "reason": reason,  # echo the reason code
        "remaining_refundable": round(order["order_total"] - order["refunded_total"], 2),  # what's left afterwards
        "order_status": order["status"],  # new status (refunded only if fully refunded)
    }


# --- Tool: escalate_to_human ------------------------------------------------
# In-memory stand-in for a ticketing system; tests inspect it to see exactly
# what Claude handed off. hooks.py also calls escalate_to_human() directly when
# it blocks a refund, so blocked refunds always land here.
ESCALATION_QUEUE: list[dict] = []  # the fake ticket queue (list of ticket dicts)

VALID_ESCALATION_REASONS = {  # allowed reason_category values
    "refund_over_limit", "customer_requested_human", "policy_exception", "tool_failure", "other",  # mirrors tools.py enum
}
_MIN_SUMMARY_CHARS = 40  # cheap guard against "refund"-style non-summaries

_escalation_counter = 0  # deterministic ticket IDs (ESC-0001...)


# The safety net must be the HARDEST path to fail: only the two required fields
# are validated. Optional fields are stored as-is — a human seeing a typo'd
# order ID beats an escalation bouncing and the customer being left stranded.
def escalate_to_human(  # implementation behind the escalation tool (also called directly by hooks.py)
    reason_category: str,  # required: why we're escalating
    summary: str,  # required: standalone handoff note
    order_id: str | None = None,  # optional related order (not validated on purpose)
    customer_email: str | None = None,  # optional contact (not validated on purpose)
    requested_amount: float | None = None,  # optional amount at stake
) -> dict:
    global _escalation_counter  # we increment the module-level counter below

    if reason_category not in VALID_ESCALATION_REASONS:  # unknown category
        raise ToolError(  # reject with allowed values
            ErrorCategory.VALIDATION,  # bad input
            f"Unknown reason_category '{reason_category}'. Use one of: "  # echo bad value
            f"{', '.join(sorted(VALID_ESCALATION_REASONS))}.",  # list valid categories
        )
    if not isinstance(summary, str) or len(summary.strip()) < _MIN_SUMMARY_CHARS:  # missing or too short to be useful
        raise ToolError(  # ask for a proper handoff note
            ErrorCategory.VALIDATION,  # bad input
            "Summary too short for a human with no context. Include: (1) what the "  # what a good summary contains
            "customer wants, (2) facts you verified, (3) actions taken or blocked.",
        )

    _escalation_counter += 1  # next sequential ticket number
    ticket = {  # the record a human agent would see
        "ticket_id": f"ESC-{_escalation_counter:04d}",  # e.g. ESC-0001
        "reason_category": reason_category,  # routing category
        "summary": summary.strip(),  # handoff note without stray whitespace
        "order_id": order_id,  # stored as given (may be None or even a typo)
        "customer_email": customer_email,  # stored as given
        "requested_amount": requested_amount,  # stored as given
    }
    ESCALATION_QUEUE.append(ticket)  # "file" the ticket
    log.info("TICKET FILED %s: reason=%s order=%s amount=%s summary=%s", ticket["ticket_id"],
             reason_category, order_id, requested_amount, ticket["summary"])  # audit: handed to a human

    # "queued", never "resolved" — matches the tool description's instruction.
    return {"ticket_id": ticket["ticket_id"], "status": "queued", "queue_position": len(ESCALATION_QUEUE)}  # receipt for Claude


# --- Dispatch map -----------------------------------------------------------
# agent.py runs any tool Claude requests via TOOL_FUNCTIONS[name](**input).
# A dict (vs. an if/elif chain) means adding a tool never touches the loop.
# agent.py must turn an unknown name or bad kwargs (TypeError) into VALIDATION
# errors. test_scenarios.py asserts these keys == the names in tools.TOOLS.
TOOL_FUNCTIONS = {  # tool name (as Claude says it) -> Python function that runs it
    "get_order": get_order,  # lookup by ID
    "search_customer_orders": search_customer_orders,  # lookup by email
    "process_refund": process_refund,  # money-moving action (hook-guarded)
    "escalate_to_human": escalate_to_human,  # human handoff
}
