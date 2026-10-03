"""Bateria reproduzivel: testes locais, validador e reinicio real do MCP."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Servidor encerrou durante inicializacao")
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except urllib.error.HTTPError:
            return  # POST-only endpoint returns an HTTP error to GET.
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Servidor nao iniciou")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=5)


def main():
    protected = [p for folder in ("dados", "validador", "exemplos") for p in (ROOT / folder).rglob("*") if p.is_file()]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    env = {**os.environ, "REQUEST_STATE_SECRET": secrets.token_hex(32), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env, check=True)
    mcp_port, agent_port = free_port(), free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    agent_url = f"http://127.0.0.1:{agent_port}"
    env.update(MCP_PORT=str(mcp_port), AGENT_PORT=str(agent_port), MCP_URL=mcp_url)
    mcp = agent = None
    with tempfile.TemporaryFile() as logs:
        def start(path):
            return subprocess.Popen([sys.executable, str(ROOT / path)], cwd=ROOT, env=env, stdout=logs, stderr=logs)

        def call(name, args, **extra):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": args, **extra, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}}}}
            req = urllib.request.Request(mcp_url, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call", "Mcp-Name": name})
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                assert len(raw) == int(response.headers["Content-Length"])
                return json.loads(raw)

        try:
            mcp = start("servidor-mcp/server.py")
            wait_ready(mcp_url, mcp)
            agent = start("agente/server.py")
            wait_ready(agent_url + "/.well-known/agent-card.json", agent)
            run = subprocess.run([sys.executable, "validador/validar.py", "--agente", agent_url,
                                  "--mcp", mcp_url.removesuffix("/mcp")], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60)
            print(run.stdout, end="")
            if run.returncode:
                raise RuntimeError(run.stderr or "Validador falhou")
            logs.seek(0)
            text = logs.read().decode("utf-8")
            trace = run.stdout.split("trace-id desta execucao: ", 1)[1].splitlines()[0]
            for method in ("tools/list", "resources/read", "tools/call"):
                assert any(f"MCP method={method} " in line and trace in line for line in text.splitlines())
            print("PASS extra traceparent presente no log MCP")
            stop(mcp)
            mcp = start("servidor-mcp/server.py")
            wait_ready(mcp_url, mcp)
            original = dict(sala="sala-garagem", inicio="2026-11-03T14:00:00-03:00",
                            fim="2026-11-03T15:00:00-03:00", responsavel="Theodoro")
            paused = call("reservar_sala", original)["result"]
            stop(mcp)
            mcp = start("servidor-mcp/server.py")
            wait_ready(mcp_url, mcp)
            raw = dict(sala="sala-mirante", inicio="2026-11-03T13:00:00-03:00",
                       fim="2026-11-03T14:00:00-03:00", responsavel="Biff")
            result = call("reservar_sala", raw, requestState=paused["requestState"],
                inputResponses={"__main__:escolha_de_sala": {"action": "accept", "content": {"sala": "sala-fusca"}}})["result"]["structuredContent"]
            assert result["inicio"] == original["inicio"] and result["responsavel"] == "Theodoro"
            assert result["sala"] == "sala-fusca"
            available = call("consultar_disponibilidade", raw)["result"]["structuredContent"]
            assert available["livre"] is True
            print("PASS extra requestState valido apos reinicio e sem reserva adulterada")
            unicode_args = {**raw, "sala": "sala-aquario", "responsavel": "João"}
            assert call("reservar_sala", unicode_args)["result"]["structuredContent"]["responsavel"] == "João"
            print("PASS extra resposta UTF-8 com Content-Length correto")
        finally:
            stop(agent)
            stop(mcp)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    print("PASS extra dados, validador e exemplos preservados")
    print("Bateria concluida sem falhas.")


if __name__ == "__main__":
    main()
