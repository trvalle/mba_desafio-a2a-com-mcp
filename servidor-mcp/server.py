#!/usr/bin/env python3
# AI_SERVER_VALIDATED_MBA_A2A_MCP_20260929: preserved implementation, syntax-checked before promotion.
"""Servidor MCP Streamable HTTP da Central de Salas.

O endpoint e intencionalmente pequeno: o desafio avalia o wire contract MCP,
entao cada chamada HTTP e uma unidade independente e nao existe sessao de
protocolo escondida entre requests.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dados"
PORT = int(os.environ.get("MCP_PORT", "7301"))
PROTOCOLO = "2026-07-28"
SERVER_INFO = {"name": "central-de-salas", "version": "1.0.0"}
STATE_TTL_SECONDS = 15 * 60
INPUT_KEY = "__main__:escolha_de_sala"
REQUIRED_CAPABILITY = {"elicitation": {"form": {}}}
RESERVATIONS_LOCK = threading.Lock()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


ROOMS = _read_json(DATA / "salas.json")
POLICY_TEXT = (DATA / "politica-de-uso.md").read_text(encoding="utf-8")
POLICY_VERSION = POLICY_TEXT.splitlines()[0].split(":", 1)[1].strip()
RESERVATIONS: list[dict[str, Any]] = _read_json(DATA / "reservas.json")
NEXT_RESERVATION = 1 + max(
    (int(str(item.get("id", "res-0000")).split("-")[-1]) for item in RESERVATIONS),
    default=0,
)


def _secret() -> bytes:
    raw = os.environ.get("REQUEST_STATE_SECRET", "")
    try:
        value = bytes.fromhex(raw)
    except ValueError as exc:
        raise SystemExit("REQUEST_STATE_SECRET deve ser hexadecimal") from exc
    if len(value) < 32:
        raise SystemExit("REQUEST_STATE_SECRET deve conter pelo menos 32 bytes")
    return value


STATE_SECRET = _secret()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def seal_request_state(arguments: dict[str, Any], alternatives: list[str]) -> str:
    payload = {
        "v": 1,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
        "tool": "reservar_sala",
        "inputKey": INPUT_KEY,
        "arguments": arguments,
        "alternatives": alternatives,
    }
    body = _b64(_json(payload).encode("utf-8"))
    signature = _b64(hmac.new(STATE_SECRET, body.encode("ascii"), hashlib.sha256).digest())
    return f"v1.{body}.{signature}"


def open_request_state(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        raise ValueError("requestState ausente")
    try:
        version, body, signature = token.split(".", 2)
        if version != "v1" or not body or not signature:
            raise ValueError("requestState invalido")
        expected = hmac.new(STATE_SECRET, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            raise ValueError("requestState invalido")
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise ValueError("requestState invalido") from exc
    if payload.get("v") != 1 or payload.get("tool") != "reservar_sala":
        raise ValueError("requestState invalido")
    if not isinstance(payload.get("exp"), int) or payload["exp"] < int(time.time()):
        raise ValueError("requestState expirado")
    if payload.get("inputKey") != INPUT_KEY:
        raise ValueError("requestState invalido")
    if not isinstance(payload.get("arguments"), dict) or not isinstance(payload.get("alternatives"), list):
        raise ValueError("requestState invalido")
    return payload


def _room(room_id: str) -> dict[str, Any] | None:
    return next((room for room in ROOMS if room["id"] == room_id), None)


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone ausente")
    return parsed


def _validate_interval(arguments: dict[str, Any]) -> str | None:
    try:
        start = _parse_time(str(arguments["inicio"]))
        end = _parse_time(str(arguments["fim"]))
    except (KeyError, TypeError, ValueError):
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if end <= start:
        return "Intervalo invalido: fim deve ser posterior a inicio"
    local_start = start.astimezone(dt.timezone(dt.timedelta(hours=-3)))
    local_end = end.astimezone(dt.timezone(dt.timedelta(hours=-3)))
    if local_start.date() != local_end.date() or local_start.time() < dt.time(8) or local_end.time() > dt.time(20):
        return "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    if end - start > dt.timedelta(hours=2):
        return "Duracao acima do limite: a politica permite no maximo 2 horas"
    return None


def _overlap(left_start: str, left_end: str, right_start: str, right_end: str) -> bool:
    a, b = _parse_time(left_start), _parse_time(left_end)
    c, d = _parse_time(right_start), _parse_time(right_end)
    return a < d and c < b


def _conflicts(room_id: str, start: str, end: str) -> list[dict[str, Any]]:
    return [
        item for item in RESERVATIONS
        if item.get("sala") == room_id and _overlap(item["inicio"], item["fim"], start, end)
    ]


def _alternatives(room: dict[str, Any], start: str, end: str) -> list[str]:
    candidates = []
    for candidate in ROOMS:
        if candidate["capacidade"] >= room["capacidade"] and not _conflicts(candidate["id"], start, end):
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item["capacidade"], item["id"]))
    return [item["id"] for item in candidates[:3]]


def _next_id() -> str:
    global NEXT_RESERVATION
    with RESERVATIONS_LOCK:
        reservation_id = f"res-{NEXT_RESERVATION:04d}"
        NEXT_RESERVATION += 1
        return reservation_id


def _reserve(arguments: dict[str, Any]) -> dict[str, Any]:
    result = {
        "reserva": _next_id(),
        "reservado": True,
        "sala": arguments["sala"],
        "inicio": arguments["inicio"],
        "fim": arguments["fim"],
        "responsavel": arguments["responsavel"],
        "politica": POLICY_VERSION,
        "motivo": None,
    }
    with RESERVATIONS_LOCK:
        RESERVATIONS.append({k: result[k] for k in ("reserva", "sala", "inicio", "fim", "responsavel")})
    return result


def _complete(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": value,
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "resultType": "complete",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "listar_salas",
            "description": "Lista todas as salas com capacidade e recursos.",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {"salas": {"type": "array"}}, "required": ["salas"]},
        },
        {
            "name": "consultar_disponibilidade",
            "description": "Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
            "inputSchema": {
                "type": "object",
                "properties": {"sala": {"type": "string"}, "inicio": {"type": "string"}, "fim": {"type": "string"}},
                "required": ["sala", "inicio", "fim"],
            },
            "outputSchema": {"type": "object", "properties": {"sala": {"type": "string"}, "livre": {"type": "boolean"}, "conflitos": {"type": "array"}}, "required": ["sala", "livre", "conflitos"]},
        },
        {
            "name": "reservar_sala",
            "description": "Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
            "inputSchema": {
                "type": "object",
                "properties": {"sala": {"type": "string"}, "inicio": {"type": "string"}, "fim": {"type": "string"}, "responsavel": {"type": "string"}},
                "required": ["sala", "inicio", "fim", "responsavel"],
            },
            "outputSchema": {"type": "object", "properties": {"reserva": {}, "reservado": {"type": "boolean"}, "sala": {}, "inicio": {}, "fim": {}, "responsavel": {}, "politica": {}, "motivo": {}}},
        },
    ]


def _rpc_error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        value["data"] = data
    return value


def handle_rpc(body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Invalid Request")}

    meta = params.get("_meta")
    if not isinstance(meta, dict) or "io.modelcontextprotocol/protocolVersion" not in meta or "io.modelcontextprotocol/clientCapabilities" not in meta:
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Missing required MCP metadata")}
    if meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOLO:
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Unsupported MCP protocol version")}

    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    header_protocol = normalized_headers.get("mcp-protocol-version")
    header_method = normalized_headers.get("mcp-method")
    if header_protocol != PROTOCOLO or header_method != method:
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32020, "MCP header does not match request body")}
    if method == "tools/call" and normalized_headers.get("mcp-name") != params.get("name"):
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32020, "Mcp-Name does not match tool name")}
    if method == "resources/read" and normalized_headers.get("mcp-name") != params.get("uri"):
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32020, "Mcp-Name does not match resource URI")}

    if method in {"initialize", "ping"}:
        result = {"protocolVersion": PROTOCOLO, "capabilities": {"tools": {}, "resources": {}, "elicitation": {"form": {}}}, "serverInfo": SERVER_INFO}
    elif method == "tools/list":
        result = {"cacheScope": "private", "resultType": "complete", "tools": _tools(), "ttlMs": 0, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
    elif method == "resources/list":
        result = {"cacheScope": "private", "resultType": "complete", "resources": [{"uri": "politica://uso", "name": "politica://uso", "mimeType": "text/markdown"}], "ttlMs": 0, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
    elif method == "resources/read":
        if params.get("uri") != "politica://uso":
            return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Unknown resource")}
        result = {"cacheScope": "private", "contents": [{"uri": "politica://uso", "mimeType": "text/markdown", "text": POLICY_TEXT}], "resultType": "complete", "ttlMs": 0, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in {"listar_salas", "consultar_disponibilidade", "reservar_sala"}:
            return 200, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Unknown tool")}
        if not isinstance(arguments, dict):
            return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Invalid arguments")}
        if name == "listar_salas":
            result = _complete({"salas": ROOMS})
        else:
            required = ["sala", "inicio", "fim"]
            if name == "reservar_sala":
                required.append("responsavel")
            if any(key not in arguments for key in required):
                return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32602, "Missing required argument")}
            room = _room(str(arguments["sala"]))
            if room is None:
                result = _tool_error(f"Sala inexistente: {arguments['sala']}")
            else:
                interval_error = _validate_interval(arguments)
                if interval_error:
                    result = _tool_error(interval_error)
                else:
                    conflicts = _conflicts(room["id"], arguments["inicio"], arguments["fim"])
                    if name == "consultar_disponibilidade":
                        result = _complete({"sala": room["id"], "livre": not conflicts, "conflitos": conflicts})
                    elif not conflicts:
                        result = _complete(_reserve(arguments))
                    else:
                        alternatives = _alternatives(room, arguments["inicio"], arguments["fim"])
                        if not alternatives:
                            result = _tool_error("Sem alternativas disponiveis no intervalo")
                        elif not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict) or meta["io.modelcontextprotocol/clientCapabilities"].get("elicitation", {}).get("form") is None:
                            result = {"__error__": _rpc_error(-32021, "Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'", {"requiredCapabilities": REQUIRED_CAPABILITY})}
                        else:
                            state = seal_request_state({key: arguments[key] for key in required}, alternatives)
                            result = {"inputRequests": {INPUT_KEY: {"method": "elicitation/create", "params": {"message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", "mode": "form", "requestedSchema": {"type": "object", "properties": {"sala": {"type": "string", "title": "Sala", "description": "Sala alternativa escolhida", "enum": alternatives}}, "required": ["sala"]}}}}, "requestState": state, "resultType": "input_required", "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
                    if name == "reservar_sala" and "inputResponses" in params:
                        try:
                            sealed = open_request_state(params.get("requestState"))
                            if params.get("name") != sealed["tool"] or not isinstance(params.get("inputResponses"), dict) or INPUT_KEY not in params["inputResponses"]:
                                raise ValueError("requestState invalido")
                            response = params["inputResponses"][INPUT_KEY]
                            action = response.get("action") if isinstance(response, dict) else None
                            if action in {"decline", "cancel"}:
                                result = _complete({"reserva": None, "reservado": False, "sala": None, "inicio": None, "fim": None, "responsavel": None, "politica": None, "motivo": "recusado" if action == "decline" else "cancelado"})
                            elif action == "accept" and isinstance(response.get("content"), dict) and response["content"].get("sala") in sealed["alternatives"]:
                                original = dict(sealed["arguments"])
                                original["sala"] = response["content"]["sala"]
                                result = _complete(_reserve(original))
                            else:
                                raise ValueError("inputResponse invalida")
                        except ValueError as exc:
                            result = {"__error__": _rpc_error(-32602, str(exc))}
    else:
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": _rpc_error(-32601, "Method not found")}

    if isinstance(result, dict) and "__error__" in result:
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": result["__error__"]}
    return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "central-de-salas/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            traceparent = ((body.get("params") or {}).get("_meta") or {}).get("traceparent", "-")
            method = body.get("method", "-")
            request_id = body.get("id", "-")
            print(f"MCP method={method} id={request_id} traceparent={traceparent}", file=sys.stderr, flush=True)
            status, response = handle_rpc(body, {key: value for key, value in self.headers.items()})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            status, response = 400, {"jsonrpc": "2.0", "id": None, "error": _rpc_error(-32700, "Parse error")}
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(json.dumps(response).encode("utf-8"))))
        self.end_headers()
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


def main() -> None:
    server = ThreadingHTTPServer((os.environ.get("MCP_HOST", "127.0.0.1"), PORT), Handler)
    print(f"servidor MCP em http://127.0.0.1:{PORT}/mcp", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

