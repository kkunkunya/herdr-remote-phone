#!/usr/bin/env python3
import ast
import json
import tempfile
import unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parents[1] / "relay/herdr_relay.py"


def load_functions(*names):
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    wanted = set(names)
    body = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    namespace = {"json": json, "os": __import__("os"), "re": __import__("re")}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(RELAY), "exec"), namespace)
    return [namespace[name] for name in names]


class InboxStateTests(unittest.TestCase):
    def test_load_ignores_malformed_items(self):
        load_inbox, = load_functions("_load_inbox")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            path.write_text('[null,{"id":"bad"},{"id":"in7","prompt":"ok"}]')
            load_inbox.__globals__["INBOX_PATH"] = str(path)
            self.assertEqual(load_inbox(), [{"id": "in7", "prompt": "ok"}])

    def test_save_is_atomic_and_round_trips(self):
        save_inbox, = load_functions("_save_inbox")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            save_inbox.__globals__["INBOX_PATH"] = str(path)
            save_inbox.__globals__["log"] = type("Log", (), {"exception": lambda *_: None})()
            expected = [{"id": "in1", "resolved": False}]
            save_inbox(expected)
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
