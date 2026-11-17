#!/usr/bin/env python3
"""Zero engine — the brain IS the engine.

An OpenAI-compatible HTTP server that answers from the Zero brain in this repo:
identity/persona.md, identity/rules.md, knowledge/<category>/<domain>.md, plus the
optional Jarvis memory facts file.

No Ollama. No llama.cpp. No vLLM. No model weights. No external API. Stdlib only.

Behaviour, per identity/rules.md:
  * never fabricate — an unmatched query returns "not recorded" plus what it does hold
  * cite the file for every stored fact
  * never emit secrets (lines matching the brain's secret pattern are dropped)
  * no scratchpad, no preamble — answer only

Usage:
  python3 runtime/zero_engine.py                  # serve on 127.0.0.1:8642
  python3 runtime/zero_engine.py --port 9000
  python3 runtime/zero_engine.py --ask "what is the disk budget?"   # one-shot, no server
"""

from __future__ import annotations

import argparse
import ast
import datetime
import importlib.util
import json
import operator
import os
import pathlib
import re
import sys
import time
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN_PY = ROOT / "runtime" / "brain.py"
MEMORY_FACTS = pathlib.Path.home() / ".openjarvis" / "memory_facts.jsonl"

MODEL_ID = "zero"
VERSION = "1.0"

# ---------------------------------------------------------------- delegation
#
# Zero answers from the brain first. Anything the brain does not hold is
# delegated to the free model router (freellmapi) so the assistant can answer
# general questions. Zero stays the engine and the identity; the router is
# only consulted for questions Zero would otherwise have to refuse.
#
# Disable with ZERO_ESCALATE=0 to get the original retrieval-only behaviour.
ESCALATE = os.environ.get("ZERO_ESCALATE", "1").lower() not in ("0", "false", "no", "off")
ESCALATE_URL = os.environ.get("ZERO_ESCALATE_URL", "http://127.0.0.1:3001/v1")
ESCALATE_MODEL = os.environ.get("ZERO_ESCALATE_MODEL", "auto")
ESCALATE_KEY_FILE = pathlib.Path(
    os.environ.get(
        "ZERO_ESCALATE_KEY_FILE",
        str(pathlib.Path.home() / ".config/opencode/freellmapi.key"),
    )
)
ESCALATE_TIMEOUT = float(os.environ.get("ZERO_ESCALATE_TIMEOUT", "90"))

# "model"    — generative assistant with Zero's persona, answers anything
# "grounded" — original behaviour: brain first, refuse what is not recorded
MODE = os.environ.get("ZERO_MODE", "model").strip().lower()

# ---------------------------------------------------------------- brain import

_spec = importlib.util.spec_from_file_location("zero_brain", BRAIN_PY)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"cannot load brain at {BRAIN_PY}")
brain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain)

GREETINGS = {
    "hi", "hello", "hey", "yo", "hiya", "sup", "howdy",
    "good morning", "good afternoon", "good evening", "hey zero", "hi zero", "hello zero",
}
IDENTITY_CUES = (
    "who are you", "what are you", "your name", "introduce yourself",
    "what can you do", "who am i talking to", "what is zero",
)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .!?")


# ---------------------------------------------------------------- computed answers
#
# Arithmetic and clock questions are *computed*, not retrieved and not
# generated — there is no model involved. They are labelled as computed so the
# brain never pretends they came from knowledge/.

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_MAX_POW = 64
_MAX_ABS = 10 ** 12


def _safe_num(node):
    """Evaluate a numeric AST without eval(); rejects everything else."""
    if isinstance(node, ast.Expression):
        return _safe_num(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _safe_num(node.left)
        right = _safe_num(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_POW or abs(left) > _MAX_ABS:
                raise ValueError("expression too large")
        value = _BINOPS[type(node.op)](left, right)
        if isinstance(value, (int, float)) and abs(value) > _MAX_ABS:
            raise ValueError("result too large")
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_num(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    raise ValueError("unsupported expression")


_MATH_CHARS = re.compile(r"^[\s0-9.+\-*/%()^]+$")
_MATH_LEAD = re.compile(
    r"^(what\s+is|what's|whats|calculate|compute|how\s+much\s+is|solve)\s+", re.I
)
_CLOCK = re.compile(
    r"\b(what\s+time|current\s+time|the\s+time|time\s+is\s+it|what\s+day|today'?s\s+date"
    r"|what\s+is\s+the\s+date|what'?s\s+the\s+date|current\s+date)\b",
    re.I,
)


def _format_number(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{round(value, 10):g}"
    return str(value)


def _try_computed(text: str) -> str | None:
    """Return an exact computed answer, or None to fall through to retrieval."""
    if _CLOCK.search(text):
        now = datetime.datetime.now()
        return (
            f"{now.strftime('%A, %d %B %Y')} — {now.strftime('%H:%M:%S')} "
            f"(local time). Computed from the system clock, not retrieved from the brain."
        )

    expr = _MATH_LEAD.sub("", text.strip()).strip().rstrip("?= ").strip()
    if not expr or not _MATH_CHARS.match(expr):
        return None
    if not re.search(r"\d", expr) or not re.search(r"[+\-*/%^]", expr):
        return None
    try:
        value = _safe_num(ast.parse(expr.replace("^", "**"), mode="eval"))
    except ZeroDivisionError:
        return f"{expr} is undefined — division by zero.\n\nComputed exactly — no model, no lookup."
    except Exception:  # noqa: BLE001 - any failure means "not a math question"
        return None
    return (
        f"{expr} = {_format_number(value)}\n\n"
        "Computed exactly — no model, no lookup."
    )


# ---------------------------------------------------------------- knowledge index

class Entry:
    __slots__ = ("text", "file", "domain", "title", "terms")

    def __init__(self, text: str, file: str, domain: str, title: str):
        self.text = text
        self.file = file
        self.domain = domain
        self.title = title
        self.terms = brain.terms(f"{text} {domain} {title}")


class BrainIndex:
    """Knowledge + memory facts, reloaded when any source file changes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.signature: tuple = ()
        self.entries: list[Entry] = []
        self.domains: dict[str, str] = {}
        self.errors: list[str] = []
        self.refresh(force=True)

    def _sig(self) -> tuple:
        files = list(brain.all_domain_files())
        stamps = [(str(p), p.stat().st_mtime_ns) for p in files]
        if MEMORY_FACTS.exists():
            stamps.append((str(MEMORY_FACTS), MEMORY_FACTS.stat().st_mtime_ns))
        return tuple(stamps)

    def refresh(self, force: bool = False) -> None:
        with self.lock:
            sig = self._sig()
            if not force and sig == self.signature:
                return
            entries: list[Entry] = []
            domains: dict[str, str] = {}
            errors: list[str] = []

            for path in brain.all_domain_files():
                try:
                    meta, facts, _ = brain.parse_domain_file(path)
                except Exception as exc:  # noqa: BLE001 - a bad file must not kill the engine
                    errors.append(f"{path.relative_to(ROOT)}: {exc}")
                    continue
                dom = brain.domain_id(path)
                title = meta.get("title", dom)
                domains[dom] = title
                rel = f"knowledge/{dom}.md"
                for _date, text in facts:
                    if brain.SECRET_RE.search(text):
                        continue
                    entries.append(Entry(text, rel, dom, title))

            if MEMORY_FACTS.exists():
                for line in MEMORY_FACTS.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("trust") != "trusted":
                        continue
                    text = (row.get("text") or "").strip()
                    if not text or brain.SECRET_RE.search(text):
                        continue
                    src = row.get("source") or "memory"
                    entries.append(Entry(text, f"memory:{src}", "memory", "memory"))

            self.signature = sig
            self.entries = entries
            self.domains = domains
            self.errors = errors

    # -- retrieval

    def search(self, query: str, limit: int = 5) -> list[Entry]:
        q = brain.terms(query)
        if not q:
            return []
        scored: list[tuple[float, Entry]] = []
        for e in self.entries:
            overlap = q & e.terms
            if not overlap:
                continue
            dom_overlap = q & brain.terms(f"{e.domain} {e.title}")
            score = len(overlap) + 2.0 * len(dom_overlap)
            if norm(query) and norm(query) in norm(e.text):
                score += 3.0
            scored.append((score, e))
        scored.sort(key=lambda pair: -pair[0])

        out: list[Entry] = []
        per_domain: dict[str, int] = {}
        seen: set[str] = set()
        for _score, e in scored:
            if e.text in seen:
                continue
            if per_domain.get(e.domain, 0) >= 2:
                continue
            seen.add(e.text)
            per_domain[e.domain] = per_domain.get(e.domain, 0) + 1
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def near_domains(self, query: str, limit: int = 6) -> list[str]:
        q = brain.terms(query)
        scored: list[tuple[int, str]] = []
        for dom, title in self.domains.items():
            d_terms = brain.terms(f"{dom} {title}")
            hit = len(q & d_terms)
            if hit:
                scored.append((hit, dom))
        scored.sort(key=lambda pair: -pair[0])
        return [f"{dom} ({self.domains[dom]})" for _s, dom in scored[:limit]]


INDEX = BrainIndex()


# ---------------------------------------------------------------- answering

def _coverage_gap(query: str, hits: list[Entry]) -> list[str]:
    q = brain.terms(query)
    covered: set[str] = set()
    for h in hits:
        covered |= (q & h.terms)
    return sorted(q - covered)


def _identity_answer() -> str:
    delegated = (
        "Anything the brain does not hold is delegated to the free model router, "
        "and I say which one answered."
        if ESCALATE
        else "I answer only from the brain and refuse anything not recorded."
    )
    return (
        f"I am {MODEL_ID} — this repo's brain, answering directly. "
        f"{len(INDEX.entries)} facts across {len(INDEX.domains)} domains "
        f"({ROOT}/knowledge), plus persona and hard rules from {ROOT}/identity.\n\n"
        f"Facts about this machine come from recorded sources, not guesses. {delegated} "
        "No model weights, no Ollama, no local inference."
    )


def _persona_prompt() -> str:
    """Zero's voice: persona + hard rules, with HTML comments stripped."""
    parts: list[str] = []
    for name in ("persona.md", "rules.md"):
        path = ROOT / "identity" / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
        if text:
            parts.append(text)
    if not parts:
        parts.append("You are Zero, a direct technical assistant. Be concise and correct.")
    parts.append(
        "Your name is Zero. You are speaking to the operator through the OpenJarvis "
        "desktop app on their own machine. Answer directly; do not mention which "
        "upstream model served the request unless asked."
    )
    try:
        facts = [e.text for e in INDEX.entries]
    except NameError:
        facts = []
    if facts:
        digest = "\n".join(f"- {f}" for f in facts[:90])
        parts.append(
            "What you know about this machine (from the operator's own notes — prefer "
            "these over assumptions, and say so if a detail is missing):\n" + digest[:6000]
        )
    return "\n\n".join(parts)[:12000]


_PERSONA_CACHE: str | None = None


def _persona() -> str:
    global _PERSONA_CACHE
    if _PERSONA_CACHE is None:
        _PERSONA_CACHE = _persona_prompt()
    return _PERSONA_CACHE


def _apply_persona(messages: list[dict]) -> list[dict]:
    """Prepend Zero's system prompt without dropping the caller's own."""
    prompt = _persona()
    if messages and (messages[0].get("role") or "") == "system":
        merged = {**messages[0], "content": prompt + "\n\n" + (messages[0].get("content") or "")}
        return [merged, *messages[1:]]
    return [{"role": "system", "content": prompt}, *messages]


def _upstream_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    try:
        key = ESCALATE_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    except OSError:
        pass
    return headers


_ESCALATE_FIELDS = (
    "messages", "tools", "tool_choice", "temperature", "max_tokens", "top_p",
    "stop", "presence_penalty", "frequency_penalty", "response_format",
)


def escalate(body: dict) -> dict | None:
    """Ask the free router for a general answer. Returns an OpenAI dict or None.

    Only consulted when the brain has nothing recorded. A router outage must
    never break the engine, so every failure path returns None.
    """
    if not ESCALATE:
        return None
    payload = {k: body[k] for k in _ESCALATE_FIELDS if k in body}
    payload["model"] = ESCALATE_MODEL
    payload["stream"] = False
    try:
        req = urllib.request.Request(
            ESCALATE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=_upstream_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=ESCALATE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - never let delegation break the engine
        return None
    if not data.get("choices"):
        return None
    return data


def lookup_answer(query: str) -> str | None:
    """Answer from the brain. Returns None when nothing relevant is recorded."""
    INDEX.refresh()
    text = (query or "").strip()
    if not text:
        return None

    n = norm(text)
    if n in GREETINGS:
        return (
            f"{MODEL_ID} here. {len(INDEX.entries)} facts across {len(INDEX.domains)} domains loaded. "
            "Ask away."
        )
    if any(cue in n for cue in IDENTITY_CUES):
        return _identity_answer()

    computed = _try_computed(text)
    if computed is not None:
        return computed

    hits = INDEX.search(text, limit=5)
    if not hits:
        return None

    out = [f"{hits[0].text} — {hits[0].file}"]
    if len(hits) > 1:
        out.append("")
        out.append("Also recorded:")
        for h in hits[1:]:
            out.append(f"- {h.text} — {h.file}")

    gap = _coverage_gap(text, hits)
    if gap:
        out.append("")
        out.append("Recorded, but not covered: " + ", ".join(gap) + ".")
    return "\n".join(out)


def miss_text(query: str) -> str:
    near = INDEX.near_domains(query)
    lines = ["That is not in the brain, and the free model router is not reachable."]
    if near:
        lines.append("")
        lines.append("Closest domains I hold: " + ", ".join(near) + ".")
    lines.append("")
    lines.append(f"Nothing in the brain matches: {', '.join(sorted(brain.terms(query))) or query}.")
    return "\n".join(lines)


def answer(query: str) -> str:
    """Model mode answers freely; grounded mode is brain first with a refusal."""
    text = (query or "").strip()
    if not text:
        return "Ask me something and I will answer."

    if MODE == "model":
        esc = escalate({"messages": _apply_persona([{"role": "user", "content": text}])})
        if esc:
            content = ((esc["choices"][0].get("message") or {}).get("content") or "").strip()
            if content:
                return content

    local = lookup_answer(text)
    if local is not None:
        return local

    esc = escalate({"messages": _apply_persona([{"role": "user", "content": text}])})
    if esc:
        content = ((esc["choices"][0].get("message") or {}).get("content") or "").strip()
        if content:
            return content
    return miss_text(text)


def _last_user_message(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return content or ""
    return ""


def _usage(prompt: str, completion: str) -> dict:
    p = max(1, len(prompt) // 4)
    c = max(1, len(completion) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


# ---------------------------------------------------------------- HTTP

def _model_object() -> dict:
    return {
        "id": MODEL_ID,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "zero",
        "permission": [],
        "root": MODEL_ID,
        "parent": None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"zero-engine/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("ZERO_ENGINE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health", "/v1/health"):
            INDEX.refresh()
            self._send_json(200, {
                "status": "ok",
                "engine": MODEL_ID,
                "version": VERSION,
                "facts": len(INDEX.entries),
                "domains": len(INDEX.domains),
                "root": str(ROOT),
                "errors": INDEX.errors,
            })
        elif path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [_model_object()]})
        elif path.startswith("/v1/models/"):
            self._send_json(200, _model_object())
        elif path == "/v1/brain":
            INDEX.refresh()
            self._send_json(200, {
                "facts": len(INDEX.entries),
                "domains": INDEX.domains,
                "errors": INDEX.errors,
            })
        else:
            self._send_json(404, {"error": {"message": f"no route {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        body = self._read_json()

        if path == "/v1/chat/completions":
            self._chat(body)
        elif path == "/v1/completions":
            prompt = body.get("prompt") or ""
            if isinstance(prompt, list):
                prompt = " ".join(str(p) for p in prompt)
            text = answer(prompt)
            self._send_json(200, {
                "id": "cmpl-" + uuid.uuid4().hex[:24],
                "object": "text_completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [{"index": 0, "text": text, "finish_reason": "stop", "logprobs": None}],
                "usage": _usage(str(prompt), text),
            })
        else:
            self._send_json(404, {"error": {"message": f"no route {path}", "type": "not_found"}})

    def _chat(self, body: dict) -> None:
        messages = body.get("messages") or []
        query = _last_user_message(messages)
        tool_calls = None

        text = None
        tool_calls = None

        # Model mode: Zero's persona speaks first, no retrieval gate.
        if MODE == "model":
            esc = escalate({**body, "messages": _apply_persona(messages)})
            if esc:
                message = esc["choices"][0].get("message") or {}
                text = (message.get("content") or "").strip() or None
                tool_calls = message.get("tool_calls") or None

        # Grounded fallback (and the primary path in grounded mode).
        if text is None and not tool_calls:
            text = lookup_answer(query)

        if text is None and not tool_calls:
            esc = escalate({**body, "messages": _apply_persona(messages)})
            if esc:
                message = esc["choices"][0].get("message") or {}
                text = (message.get("content") or "").strip() or None
                tool_calls = message.get("tool_calls") or None

        if text is None and not tool_calls:
            text = miss_text(query)
        if text is None:
            text = ""

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if not body.get("stream"):
            message: dict = {"role": "assistant", "content": text}
            if tool_calls:
                message["tool_calls"] = tool_calls
            self._send_json(200, {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }],
                "usage": _usage(query, text),
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(delta: dict, finish: str | None = None) -> None:
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        try:
            emit({"role": "assistant", "content": ""})
            step = 48
            for i in range(0, len(text), step):
                emit({"content": text[i:i + step]})
            if tool_calls:
                emit({"tool_calls": tool_calls}, "tool_calls")
            else:
                emit({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Zero engine — OpenAI-compatible, brain-backed")
    ap.add_argument("--host", default=os.environ.get("ZERO_ENGINE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ZERO_ENGINE_PORT", "8642")))
    ap.add_argument("--ask", help="answer one question and exit (no server)")
    ap.add_argument("--facts", action="store_true", help="print index stats and exit")
    args = ap.parse_args()

    if args.ask is not None:
        print(answer(args.ask))
        return 0
    if args.facts:
        INDEX.refresh()
        print(json.dumps({
            "facts": len(INDEX.entries),
            "domains": len(INDEX.domains),
            "errors": INDEX.errors,
        }, indent=2))
        return 0

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    INDEX.refresh()
    sys.stderr.write(
        f"zero-engine {VERSION} on http://{args.host}:{args.port}/v1 "
        f"— {len(INDEX.entries)} facts / {len(INDEX.domains)} domains from {ROOT}\n"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
