"""
log_config.py — The ONE place that decides where logs go and how they look.

Rule: only entry points (agent.py __main__, test_scenarios.py __main__) call
setup_logging(). Every other module just does
    log = logging.getLogger(__name__)
and logs — it never decides destinations. That's what lets tests log quietly
to a file while an interactive run could also print to the console.

Every line is stamped automatically with file:function:line (via the format
string) and a per-question run ID, so one question's full journey through
agent -> hooks -> backend can be grepped with `grep run=<id>`.

Logged payloads are OUR request kwargs — the API key lives inside the client
object and is never part of them. (A real system would also mask customer
emails/PII in logs; our data is fake, so we don't.)
"""

import contextvars  # per-request variable (safe if requests ever run concurrently)
import json  # serialize payloads into log lines
import logging  # stdlib logging framework
import uuid  # random, collision-resistant run IDs
from pathlib import Path  # log file location anchored to this folder

# --- Run ID (correlation ID) -------------------------------------------------
# ContextVar, not a plain global: each concurrent request (thread/async task)
# gets its own value, so two customers' log lines never get mixed up.
# (Alternative: pass run_id into every function — explicit, but changes every
# signature just for logging.)
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")


def new_run_id() -> str:  # called once per user question by agent.run_agent
    rid = uuid.uuid4().hex[:8]  # short but unique enough to grep
    _run_id.set(rid)  # every log line from now on (in this context) carries it
    return rid


class RunIdFilter(logging.Filter):  # stamps the current run ID onto each record
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()  # makes %(run_id)s usable in the format string
        return True  # never drops a record; it only annotates


# --- JSON-safe payload dumping -----------------------------------------------
# Handles SDK pydantic objects (.model_dump()), the test suite's SimpleNamespace
# fakes (vars()), dicts and lists; anything else falls back to str() via
# default=str — so logging can never crash the agent.
def to_jsonable(obj):
    if hasattr(obj, "model_dump"):  # real SDK objects (Message, ToolUseBlock, Usage...)
        return obj.model_dump()
    if isinstance(obj, dict):  # recurse into dict values
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):  # recurse into sequences
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):  # SimpleNamespace fakes and plain objects
        return {k: to_jsonable(v) for k, v in vars(obj).items()}
    return obj  # str / int / float / bool / None pass through


def dump(obj) -> str:  # one-line JSON string for a log message
    return json.dumps(to_jsonable(obj), default=str, ensure_ascii=False)


# --- Setup (entry points only) -----------------------------------------------
# Every line: time | level | run id | file:function:line | message
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | run=%(run_id)s | %(filename)s:%(funcName)s:%(lineno)d | %(message)s"
DEFAULT_LOG_FILE = Path(__file__).parent / "logs" / "agent.log"  # anchored to this folder, not the CWD


def setup_logging(
    log_file: Path | str = DEFAULT_LOG_FILE,
    file_level: int = logging.DEBUG,       # file gets EVERYTHING (full requests/responses)
    console_level: int = logging.WARNING,  # console stays quiet: [tool] prints already show progress
) -> Path:
    root = logging.getLogger()  # root logger: every module's getLogger(__name__) propagates here
    if getattr(root, "_agent_logging_configured", False):  # idempotent: no duplicate handlers
        return Path(log_file)
    root.setLevel(logging.DEBUG)  # let records through; handlers decide what each one keeps

    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)  # create logs/ on first run
    formatter = logging.Formatter(LOG_FORMAT)
    run_filter = RunIdFilter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")  # appends across runs
    file_handler.setLevel(file_level)
    console_handler = logging.StreamHandler()  # stderr
    console_handler.setLevel(console_level)

    # Filter on HANDLERS, not a logger: a logger-level filter only sees records
    # created by that exact logger, so child loggers (backend, hooks) would
    # arrive without run_id. A handler-level filter sees every record it emits.
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.addFilter(run_filter)
        root.addHandler(handler)

    # SDK internals (raw HTTP) would drown our lines; use ANTHROPIC_LOG=debug for those.
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._agent_logging_configured = True
    logging.getLogger(__name__).info("logging configured -> %s", log_file)
    return log_file
