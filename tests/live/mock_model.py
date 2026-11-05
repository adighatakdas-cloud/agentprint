"""A scripted stand-in for an AI model API, for driving real agent CLIs in tests.

It speaks just enough of three wire protocols for an agent to run one shell command and stop:
- Anthropic Messages API (`POST .../messages`, JSON or SSE stream)
- OpenAI Chat Completions (`POST .../chat/completions`, JSON or SSE stream)
- OpenAI Responses API (`POST .../responses`, JSON or SSE stream)

Turn logic, the same for every protocol: if the request already carries a tool result, the
model answers with a short final text (so the agent exits); otherwise, if the request offers
a shell tool, the model calls it once with `command`; with no shell tool (title generation and
other side requests) it answers with text. Every request is appended to `log_path` as JSON
Lines, so tests can check what the agent sent back, such as agentprint's BLOCK reason.

No real model, no network beyond 127.0.0.1, no keys: any API key is accepted.
"""

from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SHELL_TOOLS = ("Bash", "bash", "shell", "exec_command", "terminal", "run_shell_command",
               "local_shell", "shell_command", "run_command")  # fmt: skip
FINAL_TEXT = "Done."
_ids = itertools.count(1)


def _shell_tool(tools: list[dict]) -> tuple[str, dict] | None:
    """(name, JSON schema of its arguments) of the request's shell tool, if any."""
    by_name = {}
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        schema = fn.get("input_schema") or fn.get("parameters") or {}
        if isinstance(name, str):
            by_name[name] = schema
    for name in SHELL_TOOLS:
        if name in by_name:
            return name, by_name[name]
    return None


def shell_arguments(schema: dict, command: str, cwd: str) -> dict:
    """Arguments that fit the tool's schema: `command` as a string or an argv list."""
    props = schema.get("properties") or {}
    args: dict = {}
    key = "command" if "command" in props or not props else ("cmd" if "cmd" in props else None)
    key = key or next((k for k in props if "command" in k.lower()), "command")
    kind = (props.get(key) or {}).get("type")
    args[key] = ["bash", "-lc", command] if kind == "array" else command
    for name in schema.get("required") or []:
        if name in args:
            continue
        if name in ("workdir", "cwd", "dir_path", "directory", "Cwd"):
            args[name] = cwd
        elif name in ("description", "justification"):
            args[name] = "Run the requested command."
        elif (props.get(name) or {}).get("type") == "boolean":
            args[name] = False
    return args


class Script:
    """What the mock model does: which command to run, and what it saw."""

    def __init__(self, command: str, cwd: str, log_path: Path) -> None:
        self.command, self.cwd, self.log_path = command, cwd, log_path

    def log(self, path: str, body: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": path, "body": body}) + "\n")

    def requests(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def tool_results(self) -> list[str]:
        """Text of every tool result the agent sent back (all protocols)."""
        found: list[str] = []
        for entry in self.requests():
            body = entry["body"]
            for message in body.get("messages") or []:
                content = message.get("content")
                if message.get("role") == "tool":
                    found.append(_text(content))
                elif isinstance(content, list):
                    blocks = [b for b in content if isinstance(b, dict)]
                    found += [
                        _text(b.get("content")) for b in blocks if b.get("type") == "tool_result"
                    ]
            items = body.get("input") if isinstance(body.get("input"), list) else []
            for item in items:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    found.append(_text(item.get("output")))
        return found

    def reply(self, body: dict) -> tuple[str, dict] | None:
        """("tool", arguments) or None for a final text."""
        if _has_tool_result(body):
            return None
        tool = _shell_tool(body.get("tools") or [])
        if tool is None:
            return None
        name, schema = tool
        return name, shell_arguments(schema, self.command, self.cwd)


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text(c.get("text") if isinstance(c, dict) else c) for c in content)
    if isinstance(content, dict):
        return _text(content.get("text") or content.get("content") or "")
    return "" if content is None else str(content)


def _has_tool_result(body: dict) -> bool:
    for message in body.get("messages") or []:
        if message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return True
    items = body.get("input") if isinstance(body.get("input"), list) else []
    return any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in items)


# ---- wire formats ------------------------------------------------------------------------------


def anthropic(body: dict, decision) -> tuple[dict, list[tuple[str, dict]]]:
    """(JSON response, SSE events) for the Anthropic Messages API."""
    mid = f"msg_{next(_ids)}"
    model = body.get("model", "mock")
    usage = {"input_tokens": 1, "output_tokens": 1}
    if decision:
        name, args = decision
        block = {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}
        stop = "tool_use"
        delta = {"type": "input_json_delta", "partial_json": json.dumps(args)}
        start = {**block, "input": {}}
    else:
        block = {"type": "text", "text": FINAL_TEXT}
        stop = "end_turn"
        delta = {"type": "text_delta", "text": FINAL_TEXT}
        start = {"type": "text", "text": ""}
    message = {"id": mid, "type": "message", "role": "assistant", "model": model,
               "content": [block], "stop_reason": stop, "stop_sequence": None,
               "usage": usage}  # fmt: skip
    events = [
        ("message_start", {"type": "message_start",
                           "message": {**message, "content": [], "stop_reason": None}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": start}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return message, events


def chat(body: dict, decision) -> tuple[dict, list[tuple[str, dict]]]:
    """(JSON response, SSE chunks) for OpenAI Chat Completions."""
    cid = f"chatcmpl-{next(_ids)}"
    base = {"id": cid, "object": "chat.completion.chunk", "created": 0,
            "model": body.get("model", "mock")}  # fmt: skip
    if decision:
        name, args = decision
        call = {"id": f"call_{next(_ids)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}  # fmt: skip
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": FINAL_TEXT}
        delta = {"role": "assistant", "content": FINAL_TEXT}
        finish = "stop"
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    response = {**base, "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage}  # fmt: skip
    chunks = [
        ("", {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
        ("", {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
              "usage": usage}),
    ]  # fmt: skip
    return response, chunks


def responses(body: dict, decision) -> tuple[dict, list[tuple[str, dict]]]:
    """(JSON response, SSE events) for the OpenAI Responses API."""
    rid = f"resp_{next(_ids)}"
    if decision:
        name, args = decision
        item = {"type": "function_call", "id": f"fc_{next(_ids)}",
                "call_id": f"call_{next(_ids)}", "name": name,
                "arguments": json.dumps(args), "status": "completed"}  # fmt: skip
    else:
        item = {
            "type": "message",
            "id": f"msg_{next(_ids)}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": FINAL_TEXT, "annotations": []}],
        }
    usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
             "input_tokens_details": {"cached_tokens": 0},
             "output_tokens_details": {"reasoning_tokens": 0}}  # fmt: skip
    response = {
        "id": rid,
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": body.get("model", "mock"),
        "output": [item],
        "usage": usage,
    }
    events = [
        ("response.created", {"type": "response.created",
                              "response": {**response, "status": "in_progress", "output": []}}),
        ("response.output_item.added", {"type": "response.output_item.added",
                                        "output_index": 0, "item": item}),
    ]  # fmt: skip
    if not decision:
        events.append(
            ("response.output_text.delta", {"type": "response.output_text.delta",
                                            "output_index": 0, "content_index": 0,
                                            "item_id": item["id"], "delta": FINAL_TEXT})
        )  # fmt: skip
    events += [
        ("response.output_item.done", {"type": "response.output_item.done",
                                       "output_index": 0, "item": item}),
        ("response.completed", {"type": "response.completed", "response": response}),
    ]  # fmt: skip
    return response, events


# ---- server ------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    script: Script

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # model lists and health checks
        self._send(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": "mock-model",
                        "object": "model",
                        "type": "model",
                        "display_name": "Mock",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            },
        )

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?", 1)[0]
        self.script.log(path, body)
        if path.endswith("/count_tokens"):
            self._send(200, {"input_tokens": 1})
            return
        if path.endswith("/messages"):
            build, stream_prefix = anthropic, True
        elif path.endswith("/chat/completions"):
            build, stream_prefix = chat, False
        elif path.endswith("/responses"):
            build, stream_prefix = responses, True
        else:
            self._send(404, {"error": {"message": f"mock: no route {path}"}})
            return
        response, events = build(body, self.script.reply(body))
        if not body.get("stream"):
            self._send(200, response)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event, payload in events:
            prefix = f"event: {event}\n" if stream_prefix and event else ""
            self.wfile.write(f"{prefix}data: {json.dumps(payload)}\n\n".encode())
        if build is chat:
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


class MockModel:
    """`with MockModel(script) as url:` runs the server on 127.0.0.1 for the block."""

    def __init__(self, script: Script) -> None:
        handler = type("Handler", (_Handler,), {"script": script})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.url

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
