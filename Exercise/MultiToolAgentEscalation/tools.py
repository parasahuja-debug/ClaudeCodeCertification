"""
tools.py — Tool schemas for the support/refunds agent.

These dicts are what Claude actually sees: it never reads our Python code,
only each tool's name, description, and input_schema. Tool selection quality
therefore lives almost entirely in the description text.

Shape matches the Anthropic Messages API `tools` param. An MCP server exposes
the same structure (key spelled `inputSchema`), so these port over directly.

Planned tools:
  1. get_order               — fetch ONE order by its exact ID
  2. search_customer_orders  — find orders by customer email   (similar pair with #1)
  3. process_refund          — refund an order (amount optional)
  4. escalate_to_human       — hand off to a human agent
"""

# --- 1. get_order -----------------------------------------------------------
# Deliberately "similar" to search_customer_orders. The description states both
# when to use it AND when not to, so Claude can disambiguate between the two.
GET_ORDER = {  # schema dict for the "look up one order by ID" tool
    "name": "get_order",  # the name Claude emits in tool_use; must match a key in backend.TOOL_FUNCTIONS
    # The text Claude reads to decide WHEN to pick this tool (and when not to).
    "description": (
        "Fetch the full details of a single order when you already know its exact "  # what it does + precondition
        "order ID (format: 'ORD-' followed by 5 digits, e.g. 'ORD-10421'). "  # ID format with an example
        "Returns: order_id, customer_email, status (pending | shipped | delivered | "  # promise: fields returned
        "cancelled | refunded), order_total in USD, line items, and order date. "
        "Use this to check an order's status or total before taking action on it. "  # nudges verify-before-act
        "Do NOT use this to look up orders by customer email or name — use "  # negative rule vs. sibling tool
        "search_customer_orders for that. If the user has not given an order ID, "
        "do not guess one."  # discourages hallucinated IDs
    ),
    "input_schema": {  # JSON Schema describing the arguments Claude must send
        "type": "object",  # arguments arrive as one JSON object
        "properties": {  # the allowed argument names
            "order_id": {  # the single argument this tool takes
                "type": "string",  # IDs are text like 'ORD-10421'
                "description": "Exact order ID, e.g. 'ORD-10421'.",  # per-field hint for Claude
                "pattern": "^ORD-\\d{5}$",  # documents the format (guidance only; backend enforces it)
            },
        },
        "required": ["order_id"],  # Claude must always supply an order_id
    },
}

# --- 2. search_customer_orders ----------------------------------------------
# The "similar" sibling of get_order. Mirrors its description: says when to use
# it AND points back to get_order when an ID is known, so the boundary between
# the two is stated from both sides. Returns summaries only (no line items) to
# keep context small and to create a natural search -> get_order two-step flow.
SEARCH_CUSTOMER_ORDERS = {  # schema dict for the "find orders by email" tool
    "name": "search_customer_orders",  # tool name Claude calls; matches backend function key
    # When-to-use text; points back to get_order so the pair's boundary is stated both ways.
    "description": (
        "Find a customer's orders using their email address, when you do NOT have "  # precondition: no ID
        "an exact order ID. Returns a list of order summaries (order_id, status, "  # what comes back
        "order_total in USD, order date), newest first — no line items. The list "  # summaries only, sorted
        "may be empty if the email has no orders. "  # empty is a normal answer, not an error
        "If the user already gave an order ID (format 'ORD-' + 5 digits), use "  # redirect to sibling tool
        "get_order instead. If a summary isn't enough (e.g. you need line items), "
        "call get_order with the order_id from the results."  # suggests the two-step flow
    ),
    "input_schema": {  # arguments Claude must send
        "type": "object",  # one JSON object of arguments
        "properties": {  # allowed argument names
            "customer_email": {  # who to search for
                "type": "string",  # emails are text
                "description": "The customer's email address, e.g. 'jane@example.com'.",  # hint with example
                "format": "email",  # documents expected format (backend validates loosely)
            },
            "status": {  # optional filter to narrow results
                "type": "string",  # status names are text
                "description": "Optional filter: only return orders with this status.",  # explains optionality
                "enum": ["pending", "shipped", "delivered", "cancelled", "refunded"],  # allowed values; must match backend.VALID_STATUSES
            },
        },
        "required": ["customer_email"],  # email mandatory; status may be omitted
    },
}

# --- 3. process_refund ------------------------------------------------------
# The tool the interceptor hook guards. `amount` is deliberately OPTIONAL
# (omitted = full refund), so the hook cannot just read tool_input["amount"]:
# it must compute the effective amount (looking up the order total when amount
# is missing) or a full refund would bypass the threshold. The threshold value
# itself lives only in hooks.py — the description mentions the behaviour, not
# the number, so there's a single source of truth.
PROCESS_REFUND = {  # schema dict for the money-moving tool (guarded by hooks.py)
    "name": "process_refund",  # tool name; hooks.pre_tool_use intercepts calls with this name
    # Tells Claude how to call it, to verify first, and how to react if policy blocks it.
    "description": (
        "Issue a refund for an order. Provide `amount` (USD) for a partial refund; "  # partial-refund usage
        "omit it to refund the full order total. Before calling, confirm the order "  # omitted = full refund
        "exists and is refundable (status delivered or shipped) using get_order. "  # verify-before-act
        "Large refunds may be blocked by policy and require human approval: if the "  # warns of the hook (no $ number)
        "result says escalation is required, do NOT retry — call escalate_to_human "  # how to react to a block
        "with the order details instead."
    ),
    "input_schema": {  # arguments Claude must send
        "type": "object",  # one JSON object of arguments
        "properties": {  # allowed argument names
            "order_id": {  # which order to refund
                "type": "string",  # IDs are text
                "description": "Exact order ID, e.g. 'ORD-10421'.",  # hint with example
                "pattern": "^ORD-\\d{5}$",  # same ID format as get_order
            },
            "amount": {  # optional partial amount
                "type": "number",  # dollars as a number
                "description": "Refund amount in USD. Omit for a full refund.",  # explains omission semantics
                "exclusiveMinimum": 0,  # must be > 0 (backend re-checks)
            },
            "reason": {  # why the refund happens (audit trail)
                "type": "string",  # reason code as text
                "description": "Why the refund is being issued.",  # hint for Claude
                "enum": ["damaged", "not_received", "wrong_item", "changed_mind", "other"],  # fixed codes; must match backend.VALID_REASONS
            },
        },
        "required": ["order_id", "reason"],  # amount intentionally NOT required
    },
}

# --- 4. escalate_to_human ---------------------------------------------------
# The human receiving this shares NONE of Claude's context, so `summary` is the
# whole handoff: what the customer wants, what was verified, what was tried
# (including blocked actions). Facts only — no fraud/"genuine" verdicts, which
# Claude can't support and a human might over-trust. Order fields are optional
# because not every escalation is about a specific order or a refund.
ESCALATE_TO_HUMAN = {  # schema dict for the human-handoff tool
    "name": "escalate_to_human",  # tool name; matches backend function key
    # When to escalate, what the standalone summary must contain, and what to tell the customer.
    "description": (
        "Hand the case to a human support agent. Use when: a refund is blocked by "  # trigger 1
        "policy and needs approval; the customer explicitly asks for a human; the "  # triggers 2-3
        "request needs a policy exception; or tools keep failing with non-retryable "  # trigger 4
        "errors. The human has NOT seen this conversation, so `summary` must stand "  # why summary matters
        "alone: (1) what the customer wants, (2) facts you verified (order status, "  # required summary parts
        "total, dates), (3) actions already taken or blocked. State facts only — do "
        "not speculate about fraud or the customer's honesty. After escalating, tell "  # facts-not-verdicts rule
        "the customer a human will follow up; never say the issue is resolved."  # honest customer messaging
    ),
    "input_schema": {  # arguments Claude must send
        "type": "object",  # one JSON object of arguments
        "properties": {  # allowed argument names
            "reason_category": {  # coarse reason for routing/reporting
                "type": "string",  # category code as text
                "description": "Primary reason for escalating.",  # hint for Claude
                "enum": [  # allowed categories; must match backend.VALID_ESCALATION_REASONS
                    "refund_over_limit",  # the hook's case
                    "customer_requested_human",  # customer asked for a person
                    "policy_exception",  # needs a rule bent
                    "tool_failure",  # tools keep failing
                    "other",  # catch-all
                ],
            },
            "summary": {  # the standalone handoff note
                "type": "string",  # free text
                "description": "Standalone handoff note: request, verified facts, actions taken/blocked.",  # what to include
            },
            "order_id": {  # optional related order
                "type": "string",  # IDs are text
                "description": "Related order ID, if any.",  # optional because not every case has one
                "pattern": "^ORD-\\d{5}$",  # same ID format as other tools
            },
            "customer_email": {  # optional contact
                "type": "string",  # emails are text
                "description": "Customer's email, if known.",  # optional
                "format": "email",  # documents expected format
            },
            "requested_amount": {  # optional amount at stake
                "type": "number",  # dollars as a number
                "description": "Refund amount (USD) the customer wants, if applicable.",  # optional
                "exclusiveMinimum": 0,  # must be > 0 when given
            },
        },
        "required": ["reason_category", "summary"],  # only these two are mandatory
    },
}

# --- Registry ---------------------------------------------------------------
# Passed as `tools=TOOLS` to client.messages.create(). Kept explicit (rather than
# auto-collecting module dicts) so this list is also the readable answer to
# "what is the agent allowed to call?".
TOOLS = [GET_ORDER, SEARCH_CUSTOMER_ORDERS, PROCESS_REFUND, ESCALATE_TO_HUMAN]  # the full menu sent to Claude on every request
