"""Estados adicionais do agente e privacidade do estado MCP."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("agent_server", Path(__file__).resolve().parents[1] / "agente/server.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentTests(unittest.TestCase):
    def setUp(self):
        mocked = patch.object(agent, "TASKS", {})
        mocked.start()
        self.addCleanup(mocked.stop)

    def send(self, text, **extra):
        return agent.handle_rpc({"id": 7, "method": "SendMessage", "params": {
            "message": {"messageId": "msg-test", "role": "ROLE_USER", "parts": [{"text": text}], **extra}}}, None)

    def test_invalid_grammar_fails_without_calling_mcp(self):
        with patch.object(agent.MCP, "call_tool") as call:
            result = self.send("reservar qualquer sala")
            self.assertEqual(result["result"]["task"]["status"]["state"], "TASK_STATE_FAILED")
            call.assert_not_called()

    def test_unknown_task_and_method_return_errors(self):
        for body, code in (({"id": 8, "method": "GetTask", "params": {"id": "missing"}}, -32001),
                           ({"id": 9, "method": "Unknown", "params": {}}, -32601)):
            result = agent.handle_rpc(body, None)
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["id"], body["id"])
        self.assertEqual(self.send("escolha=sala-fusca", taskId="missing")["error"]["code"], -32001)

    def test_public_task_omits_all_private_mcp_fields(self):
        task = agent._new_task({"parts": []}, "private-trace")
        task.update(requestState="private-token", inputKey="private-key", alternatives=["private-room"], arguments={"secret": True})
        public = agent._task_result(task)["result"]["task"]
        self.assertFalse(set(public) & {"requestState", "inputKey", "alternatives", "arguments", "traceparent"})

    def test_mcp_transport_error_ends_task_as_failed(self):
        task = agent._new_task({"parts": []}, None)
        agent._complete_from_mcp(task, {"error": {"code": -32000, "message": "MCP indisponivel"}})
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(task["artifacts"], [])


if __name__ == "__main__":
    unittest.main()
