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
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN_PY = ROOT / "runtime" / "brain.py"
MEMORY_FACTS = pathlib.Path.home() / ".openjarvis" / "memory_facts.jsonl"

MODEL_ID = "zero"
VERSION = "1.0"

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
    return (
        f"I am {MODEL_ID} — this repo's brain, answering directly. "
        f"{len(INDEX.entries)} facts across {len(INDEX.domains)} domains "
        f"({ROOT}/knowledge), plus persona and hard rules from {ROOT}/identity.\n\n"
        "Retrieval is exact, not generative: I return what is recorded and say so when "
        "something is not. No model weights, no Ollama, no external API."
    )


def answer(query: str) -> str:
    """Compose a grounded answer from the brain. Never invents."""
    INDEX.refresh()
    text = (query or "").strip()
    if not text:
        return "Ask me something and I will answer from the brain."

    n = norm(text)
    if n in GREETINGS:
        return (
            f"{MODEL_ID} here. {len(INDEX.entries)} facts across {len(INDEX.domains)} domains loaded. "
            "Ask away."
        )
    if any(cue in n for cue in IDENTITY_CUES):
        return _identity_answer()

    hits = INDEX.search(text, limit=5)
    if not hits:
        near = INDEX.near_domains(text)
        lines = ["That is not recorded, so I will not guess."]
        if near:
            lines.append("")
            lines.append("Closest domains I hold: " + ", ".join(near) + ".")
        else:
            sample = ", ".join(sorted(INDEX.domains)[:8])
            lines.append("")
            lines.append(f"Domains I hold include: {sample}, and {max(0, len(INDEX.domains) - 8)} more.")
        lines.append("")
        lines.append(f"Nothing in the brain matches: {', '.join(sorted(brain.terms(text))) or text}.")
        return "\n".join(lines)

    out = [f"{hits[0].text} — {hits[0].file}"]
    if len(hits) > 1:
        out.append("")
        out.append("Also recorded:")
        for h in hits[1:]:
            out.append(f"- {h.text} — {h.file}")

    gap = _coverage_gap(text, hits)
    if gap:
        out.append("")
        out.append("Not recorded: " + ", ".join(gap) + ".")
    return "\n".join(out)


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
        text = answer(query)
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if not body.get("stream"):
            self._send_json(200, {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
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
