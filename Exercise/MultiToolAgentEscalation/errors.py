"""
errors.py — Structured error responses shared by hooks.py and backend.py.

Every failure the agent sees has the same shape:
    {"errorCategory": ..., "isRetryable": ..., "description": ...}

isRetryable means: "the EXACT same call, resent unchanged, might succeed."
  - transient (timeout)   -> True   (cause was temporary)
  - policy_blocked        -> False  (deterministic: same input, same "no")
  - validation            -> False  (resend fails again; Claude must FIX the
                                     input — `description` says what to fix)

Usage (preview — backend.py / hooks.py / agent.py are built next):

    # 1) RAISE side — backend.py (or hooks.py) hits a problem:
    from errors import ToolError, ErrorCategory

    def get_order(order_id: str) -> dict:
        if not re.fullmatch(r"ORD-\\d{5}", order_id):
            raise ToolError(ErrorCategory.VALIDATION,
                            f"'{order_id}' is not a valid ID; expected 'ORD-' + 5 digits.")
        if order_id not in ORDERS:
            raise ToolError(ErrorCategory.NOT_FOUND, f"No order with ID {order_id}.")
        return ORDERS[order_id]

    # 2) CATCH side — agent.py, once, around every tool call:
    try:
        result = run_tool(block.name, block.input)    # hooks.py then backend.py
        content, is_error = json.dumps(result), False
    except ToolError as e:
        content, is_error = json.dumps(e.to_dict()), True
    except Exception:                                 # real bug -> never leak a traceback
        content = json.dumps(ToolError(ErrorCategory.INTERNAL,
                                       "Unexpected server error.").to_dict())
        is_error = True

    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                         "content": content, "is_error": is_error})

    # 3) What Claude then reads in that tool_result:
    #    {"errorCategory": "validation", "isRetryable": false,
    #     "description": "'12345' is not a valid ID; expected 'ORD-' + 5 digits."}
"""

from enum import Enum  # base class for a fixed, named set of error categories


# --- Categories -------------------------------------------------------------
# str-Enum so values serialize straight into the JSON tool_result.
class ErrorCategory(str, Enum):  # inherits str so each member IS its string value (JSON-friendly)
    VALIDATION = "validation"          # bad input (malformed ID, amount > total)
    NOT_FOUND = "not_found"            # well-formed input, no such record
    TRANSIENT = "transient"            # timeout / temporary outage
    POLICY_BLOCKED = "policy_blocked"  # interceptor hook refused -> escalate
    INTERNAL = "internal"              # unexpected bug on our side


# Single source of truth: retryability is derived from the category, never set
# ad hoc at each raise site (one forgotten flag = an infinite retry loop).
RETRYABLE = {  # lookup table: category -> "would the identical call possibly succeed if resent?"
    ErrorCategory.VALIDATION: False,  # same bad input fails again; Claude must change it
    ErrorCategory.NOT_FOUND: False,  # record still won't exist on a resend
    ErrorCategory.TRANSIENT: True,  # temporary cause; a resend may work
    ErrorCategory.POLICY_BLOCKED: False,  # policy is deterministic; resending gets the same "no"
    ErrorCategory.INTERNAL: False,  # our bug; retrying won't fix it
}


# --- The connector ----------------------------------------------------------
# backend.py / hooks.py RAISE this; agent.py CATCHES it in one place and sends
# to_dict() back to Claude as a tool_result with is_error=True. Raising (vs.
# returning error dicts) means no caller can forget to check a return value.
class ToolError(Exception):  # an exception type so failures jump straight to agent.py's single catch
    def __init__(self, category: ErrorCategory, description: str):  # caller states WHAT kind and WHAT to do
        super().__init__(description)  # keeps normal Exception behaviour (str(e), tracebacks) working
        self.category = category  # remembered so to_dict() can look up retryability
        self.description = description  # human/Claude-readable guidance on what to do next

    def to_dict(self) -> dict:  # converts the error into the JSON shape Claude receives
        return {  # the three fields the exercise requires
            "errorCategory": self.category.value,  # plain string like "transient"
            "isRetryable": RETRYABLE[self.category],  # derived, never hand-set
            "description": self.description,  # guidance text passed through unchanged
        }
