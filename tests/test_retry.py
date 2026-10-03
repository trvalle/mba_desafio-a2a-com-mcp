"""Regressao: retries nunca reservam com os argumentos crus."""

import importlib.util
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mcp_server", ROOT / "servidor-mcp/server.py")
server = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {"REQUEST_STATE_SECRET": "ab" * 32}):
    spec.loader.exec_module(server)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.reservations = patch.object(server, "RESERVATIONS", [])
        self.reservations.start()
        self.addCleanup(self.reservations.stop)
        self.counter = patch.object(server, "NEXT_RESERVATION", 1)
        self.counter.start()
        self.addCleanup(self.counter.stop)
        self.original = dict(sala="sala-garagem", inicio="2026-11-03T14:00:00-03:00",
                             fim="2026-11-03T15:00:00-03:00", responsavel="Theodoro")
        server._reserve(self.original)
        _, response = self.call(self.original)
        self.state = response["result"]["requestState"]
        self.before = list(server.RESERVATIONS)
        self.counter_before = server.NEXT_RESERVATION
        self.raw = dict(sala="sala-mirante", inicio="2026-11-03T13:00:00-03:00",
                        fim="2026-11-03T14:00:00-03:00", responsavel="Biff")

    def call(self, arguments, **extra):
        params = {"name": "reservar_sala", "arguments": arguments,
                  "_meta": {"io.modelcontextprotocol/protocolVersion": server.PROTOCOLO,
                            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}},
                  **extra}
        return server.handle_rpc({"id": 1, "method": "tools/call", "params": params},
                                 {"Mcp-Protocol-Version": server.PROTOCOLO,
                                  "Mcp-Method": "tools/call", "Mcp-Name": "reservar_sala"})

    def retry(self, action, state=None, room="sala-fusca", arguments=None):
        return self.call(self.raw if arguments is None else arguments,
                         requestState=self.state if state is None else state,
                         inputResponses={server.INPUT_KEY: {"action": action, "content": {"sala": room}}})

    def test_accept_uses_only_sealed_values_and_creates_one_reservation(self):
        for raw in (self.raw, {}, {**self.raw, "sala": "inexistente", "inicio": "invalido"}):
            with self.subTest(arguments=raw):
                server.RESERVATIONS[:] = self.before
                server.NEXT_RESERVATION = self.counter_before
                status, response = self.retry("accept", arguments=raw)
                self.assertEqual(status, 200)
                result = response["result"]["structuredContent"]
                expected = {**self.original, "sala": "sala-fusca"}
                self.assertEqual({k: result[k] for k in expected}, expected)
                self.assertEqual(server.RESERVATIONS, self.before + [{"reserva": result["reserva"], **expected}])
                self.assertEqual(server.NEXT_RESERVATION, self.counter_before + 1)
                self.assertFalse(server._conflicts(self.raw["sala"], self.raw["inicio"], self.raw["fim"]))

    def test_decline_and_cancel_do_not_write(self):
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                status, response = self.retry(action)
                self.assertEqual(status, 200)
                self.assertFalse(response["result"]["structuredContent"]["reservado"])
                self.assertEqual(server.RESERVATIONS, self.before)
                self.assertEqual(server.NEXT_RESERVATION, self.counter_before)

    def test_invalid_continuations_do_not_write(self):
        for extra in ({"requestState": "invalid", "inputResponses": {}},
                      {"inputResponses": {}},
                      {"requestState": self.state, "inputResponses": {}},
                      {"requestState": self.state, "inputResponses": {server.INPUT_KEY: {"action": "accept", "content": {"sala": "sala-aquario"}}}}):
            with self.subTest(extra=extra):
                status, response = self.call(self.raw, **extra)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], -32602)
                self.assertEqual(server.RESERVATIONS, self.before)
                self.assertEqual(server.NEXT_RESERVATION, self.counter_before)

    def test_expired_state_does_not_write(self):
        with patch.object(server.time, "time", return_value=0):
            expired = server.seal_request_state(self.original, ["sala-fusca"])
        status, response = self.retry("accept", state=expired)
        self.assertEqual((status, response["error"]["code"]), (400, -32602))
        self.assertEqual(server.RESERVATIONS, self.before)

    def test_state_signed_with_another_secret_does_not_write(self):
        with patch.object(server, "STATE_SECRET", b"different-secret" * 4):
            invalid = server.seal_request_state(self.original, ["sala-fusca"])
        status, response = self.retry("accept", state=invalid)
        self.assertEqual((status, response["error"]["code"]), (400, -32602))
        self.assertEqual(server.RESERVATIONS, self.before)

    def test_alternative_occupied_after_elicitation_is_rejected(self):
        server._reserve({**self.original, "sala": "sala-fusca"})
        before = list(server.RESERVATIONS)
        _, response = self.retry("accept")
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(server.RESERVATIONS, before)

    def test_repeated_accept_does_not_duplicate_reservation(self):
        self.retry("accept")
        before = list(server.RESERVATIONS)
        _, response = self.retry("accept")
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(server.RESERVATIONS, before)

    def test_simultaneous_reservations_do_not_overlap(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.call(self.raw), range(20)))
        completed = [r for _, r in responses if r["result"].get("structuredContent", {}).get("reservado")]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(server.RESERVATIONS), len(self.before) + 1)

    def test_policy_boundaries_and_adjacent_intervals(self):
        for start, end in (("08:00", "10:00"), ("18:00", "20:00")):
            args = {**self.raw, "inicio": f"2026-11-03T{start}:00-03:00", "fim": f"2026-11-03T{end}:00-03:00"}
            self.assertIsNone(server._validate_interval(args))
        self.assertFalse(server._overlap(self.raw["inicio"], self.raw["fim"], self.original["inicio"], self.original["fim"]))
        for start, end in (("07:59", "09:00"), ("19:00", "20:01"), ("08:00", "10:01"), ("09:00", "09:00")):
            args = {**self.raw, "inicio": f"2026-11-03T{start}:00-03:00", "fim": f"2026-11-03T{end}:00-03:00"}
            self.assertIsNotNone(server._validate_interval(args))

    def test_state_cannot_be_used_on_another_tool(self):
        for name in ("listar_salas", "consultar_disponibilidade"):
            body = {"id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": self.raw, "requestState": self.state,
                "inputResponses": {server.INPUT_KEY: {"action": "accept", "content": {"sala": "sala-fusca"}}},
                "_meta": {"io.modelcontextprotocol/protocolVersion": server.PROTOCOLO,
                          "io.modelcontextprotocol/clientCapabilities": {}}}}
            status, response = server.handle_rpc(body, {"Mcp-Protocol-Version": server.PROTOCOLO,
                "Mcp-Method": "tools/call", "Mcp-Name": name})
            self.assertEqual((status, response["error"]["code"]), (400, -32602))
            self.assertEqual(server.RESERVATIONS, self.before)

    def test_mcp_headers_must_match_body(self):
        body = {"id": 1, "method": "tools/call", "params": {"name": "reservar_sala",
            "arguments": self.raw, "_meta": {"io.modelcontextprotocol/protocolVersion": server.PROTOCOLO,
            "io.modelcontextprotocol/clientCapabilities": {}}}}
        correct = {"Mcp-Protocol-Version": server.PROTOCOLO, "Mcp-Method": "tools/call", "Mcp-Name": "reservar_sala"}
        for key in correct:
            for value in (None, "divergente"):
                headers = dict(correct)
                if value is None:
                    headers.pop(key)
                else:
                    headers[key] = value
                status, response = server.handle_rpc(body, headers)
                self.assertEqual((status, response["error"]["code"]), (400, -32020))
                self.assertEqual(server.RESERVATIONS, self.before)

    def test_invalid_intervals_never_write(self):
        for start, end in (("invalido", self.raw["fim"]),
                           ("2026-11-03T13:00:00", "2026-11-03T14:00:00"),
                           (self.raw["fim"], self.raw["inicio"])):
            _, response = self.call({**self.raw, "inicio": start, "fim": end})
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(server.RESERVATIONS, self.before)


if __name__ == "__main__":
    unittest.main()
