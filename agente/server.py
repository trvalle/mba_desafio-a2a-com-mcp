#!/usr/bin/env python3
"""Agente A2A que hospeda um cliente MCP HTTP da Central de Salas."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
PORT = int(os.environ.get("AGENT_PORT", "7300"))
PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def _text(parts: list[dict[str, Any]]) -> str:
    return " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))


def _parse_reservation(value: str) -> dict[str, str] | None:
    pattern = re.fullmatch(r"reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(\S+)", value.strip())
    if not pattern:
        return None
    return {"sala": pattern.group(1), "inicio": pattern.group(2), "fim": pattern.group(3), "responsavel": pattern.group(4)}


def _parse_choice(value: str) -> str | None:
    pattern = re.fullmatch(r"escolha=(\S+)", value.strip())
    return pattern.group(1) if pattern else None


def _rpc_headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOLO, "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    return headers


class MCPClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self._discovery_lock = threading.Lock()
        self._discovered = False
        self.tools: dict[str, dict[str, Any]] = {}
        self.policy_version = ""

    def _call(self, method: str, params: dict[str, Any], name: str | None, traceparent: str | None) -> dict[str, Any]:
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        request = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": method, "params": {**params, "_meta": meta}}
        req = urllib.request.Request(self.url, data=json.dumps(request).encode("utf-8"), headers=_rpc_headers(method, name), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {"error": {"code": error.code, "message": "MCP HTTP error"}}
        except (urllib.error.URLError, TimeoutError) as error:
            return {"error": {"code": -32000, "message": f"MCP indisponivel: {error}"}}

    def ensure_discovered(self, traceparent: str | None) -> None:
        if self._discovered:
            return
        with self._discovery_lock:
            if self._discovered:
                return
            listed = self._call("tools/list", {}, None, traceparent)
            result = listed.get("result") or {}
            self.tools = {item["name"]: item for item in result.get("tools", []) if isinstance(item, dict) and item.get("name")}
            policy = self._call("resources/read", {"uri": "politica://uso"}, "politica://uso", traceparent)
            contents = ((policy.get("result") or {}).get("contents") or [{}])
            text = contents[0].get("text", "") if contents else ""
            first_line = text.splitlines()[0] if text.splitlines() else ""
            self.policy_version = first_line.split(":", 1)[1].strip() if ":" in first_line else ""
            self._discovered = True

    def call_tool(self, name: str, arguments: dict[str, Any], traceparent: str | None, retry: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if retry:
            params.update(retry)
        return self._call("tools/call", params, name, traceparent)


MCP = MCPClient(MCP_URL)
TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.RLock()


def _message(message_id: str, role: str, text: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"messageId": message_id, "role": role, "parts": [{"text": text}]}
    if task:
        result.update({"taskId": task["id"], "contextId": task["contextId"]})
    return result


def _task_result(task: dict[str, Any]) -> dict[str, Any]:
    # O requestState nunca e copiado para esta estrutura serializavel.
    public = {key: value for key, value in task.items() if key not in {"requestState", "inputKey", "alternatives", "arguments", "traceparent"}}
    return {"jsonrpc": "2.0", "id": task.get("rpcId"), "result": {"task": public}}


def _set_status(task: dict[str, Any], state: str, text: str) -> None:
    message = _message(_new_id("msg"), "ROLE_AGENT", text, task)
    task["status"] = {"state": state, "message": message}
    task["history"].append(message)


def _failure_text(result: dict[str, Any]) -> str:
    value = result.get("result") or {}
    return " ".join(part.get("text", "") for part in value.get("content", []) if isinstance(part, dict)) or (result.get("error") or {}).get("message", "Falha no servidor MCP")


def _complete_from_mcp(task: dict[str, Any], response: dict[str, Any]) -> None:
    result = response.get("result") or {}
    if response.get("error") or result.get("isError"):
        _set_status(task, "TASK_STATE_FAILED", _failure_text(response))
        return
    if result.get("resultType") == "input_required":
        requests = result.get("inputRequests") or {}
        key = next(iter(requests), None)
        request = requests.get(key, {}) if key else {}
        schema = ((request.get("params") or {}).get("requestedSchema") or {})
        field = (schema.get("properties") or {}).get("sala") or {}
        alternatives = list(field.get("enum") or [])
        task["requestState"] = result.get("requestState")
        task["inputKey"] = key
        task["alternatives"] = alternatives
        _set_status(task, "TASK_STATE_INPUT_REQUIRED", f"alternativas: {', '.join(alternatives)}")
        return
    data = result.get("structuredContent") or {}
    if not data.get("reservado"):
        _set_status(task, "TASK_STATE_CANCELED", "Reserva recusada.")
        return
    artifact_data = {key: data.get(key) for key in ("reserva", "sala", "inicio", "fim", "responsavel", "politica")}
    task["artifacts"] = [{"artifactId": _new_id("art"), "name": "reserva", "parts": [{"text": json.dumps(artifact_data, ensure_ascii=False)}]}]
    _set_status(task, "TASK_STATE_COMPLETED", f"Reserva {data.get('reserva')} confirmada na {data.get('sala')}.")


def _new_task(message: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    task = {
        "id": _new_id("task"),
        "contextId": _new_id("ctx"),
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "history": [message],
        "artifacts": [],
        "arguments": None,
        "requestState": None,
        "inputKey": None,
        "alternatives": [],
        "traceparent": traceparent,
    }
    task["status"] = {"state": "TASK_STATE_WORKING"}
    return task


def _process_new(task: dict[str, Any], text: str) -> None:
    arguments = _parse_reservation(text)
    if not arguments:
        _set_status(task, "TASK_STATE_FAILED", "Formato invalido")
        return
    task["arguments"] = arguments
    MCP.ensure_discovered(task.get("traceparent"))
    _complete_from_mcp(task, MCP.call_tool("reservar_sala", arguments, task.get("traceparent")))


def _process_continuation(task: dict[str, Any], text: str) -> None:
    choice = _parse_choice(text)
    if not choice:
        _set_status(task, "TASK_STATE_INPUT_REQUIRED", f"alternativas: {', '.join(task.get('alternatives', []))}")
        return
    if choice == "recusar":
        retry = {"inputResponses": {task["inputKey"]: {"action": "decline"}}, "requestState": task["requestState"]}
        _complete_from_mcp(task, MCP.call_tool("reservar_sala", task["arguments"], task.get("traceparent"), retry))
        return
    if choice not in task.get("alternatives", []):
        _set_status(task, "TASK_STATE_INPUT_REQUIRED", f"alternativas: {', '.join(task.get('alternatives', []))}")
        return
    retry = {"inputResponses": {task["inputKey"]: {"action": "accept", "content": {"sala": choice}}}, "requestState": task["requestState"]}
    _complete_from_mcp(task, MCP.call_tool("reservar_sala", task["arguments"], task.get("traceparent"), retry))


def _handle_send(params: dict[str, Any], traceparent: str | None, rpc_id: Any) -> dict[str, Any]:
    message = params.get("message") or {}
    parts = message.get("parts") or []
    text = _text(parts)
    task_id = message.get("taskId") or params.get("taskId")
    with TASKS_LOCK:
        if task_id:
            task = TASKS.get(task_id)
            if not task:
                return {"error": {"code": -32001, "message": "Task nao encontrada"}}
            if task["status"].get("state") in TERMINAL:
                return {"error": {"code": -32002, "message": "Task terminal nao pode receber mensagens"}}
            task["rpcId"] = rpc_id
            task["history"].append(message)
            _process_continuation(task, text)
            return _task_result(task)
        task = _new_task(message, traceparent)
        task["rpcId"] = rpc_id
        TASKS[task["id"]] = task
        _process_new(task, text)
        return _task_result(task)


def handle_rpc(body: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    method = body.get("method")
    rpc_id = body.get("id")
    params = body.get("params") or {}
    if method == "SendMessage":
        return {"jsonrpc": "2.0", "id": rpc_id, **_handle_send(params, traceparent, rpc_id)}
    if method == "GetTask":
        task = TASKS.get(params.get("id"))
        if not task:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "Task nao encontrada"}}
        task["rpcId"] = rpc_id
        return _task_result(task)
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not found"}}


CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": "http://127.0.0.1:7300/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "reservar-sala", "name": "Reservar sala", "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.", "tags": ["salas", "agenda"], "inputModes": ["text/plain"], "outputModes": ["text/plain"], "examples": ["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"]}],
}


class Handler(BaseHTTPRequestHandler):
    server_version = "central-de-salas-agent/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/.well-known/agent-card.json":
            self.send_error(404)
            return
        self._write(200, CARD)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/a2a":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            response = handle_rpc(body, self.headers.get("traceparent"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        self._write(200, response)

    def _write(self, status: int, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    server = ThreadingHTTPServer((os.environ.get("AGENT_HOST", "127.0.0.1"), PORT), Handler)
    print(f"agente A2A em http://127.0.0.1:{PORT}/a2a; MCP={MCP_URL}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
