"""用本机假服务检查真实网关的流式与 JSON 解析（不连外网）。"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from npc.gateway import OpenAICompatGateway, ModelError  # noqa: E402

SEEN = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.headers.get("Authorization"), body))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ["你来", "了。", "\n<<<\n", '{"bond":{}}']:
                self.wfile.write(f"data: {json.dumps({'choices':[{'delta':{'content':piece}}]})}\n\n".encode())
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":7}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
        elif body["model"] == "bad":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"no key"}')
        else:
            out = json.dumps({"choices": [{"message": {"content": '```json\n{"ok": 1}\n```'}}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_stream_and_json(self):
        g = OpenAICompatGateway({"base_url": self.base, "api_key": "k", "model": "m", "reasoning_effort": "low"})
        got = []
        g.on_usage = lambda model, u: got.append((model, u["prompt_tokens"]))
        self.assertEqual("".join(g.stream([{"role": "user", "content": "x"}])), '你来了。\n<<<\n{"bond":{}}')
        auth, body = SEEN[-1]
        self.assertEqual(auth, "Bearer k")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(got, [("m", 100)])
        self.assertIn("max_completion_tokens", body)
        self.assertEqual(g.json([{"role": "user", "content": "x"}]), {"ok": 1})
        self.assertEqual(SEEN[-1][1]["response_format"], {"type": "json_object"})

    def test_error(self):
        g = OpenAICompatGateway({"base_url": self.base, "model": "bad"})
        with self.assertRaises(ModelError):
            g.json([{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
