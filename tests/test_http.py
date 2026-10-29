"""HTTP 接口：本机起一个服务，按页面的调用顺序走一遍（模拟模式，不调用模型）。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("ACCESS_CODE", None)
        os.environ.pop("DATABASE_URL", None)
        from host import server
        cls.tmp = tempfile.TemporaryDirectory()
        server.HOST = server.Host(Path(cls.tmp.name))
        server.HOST.rt.g.delay = 0
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        time.sleep(0.1)
        cls.server.HOST.store.close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read().decode()
                if r.headers.get("Content-Type", "").startswith("text/event-stream"):
                    return [json.loads(x[5:]) for x in raw.split("\n\n") if x.startswith("data:")]
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    def test_flow(self):
        st = self.get("/api/state")
        self.assertEqual(st["story"]["title"], "雪痕")
        self.assertEqual([n["id"] for n in st["story"]["npcs"]], ["xie_bugui", "he_rendong", "yan_shouzheng"])
        pid = self.post("/api/players", {"name": "叶舟", "shop": "归雁客栈", "gender": "m", "avatar": "me_3"})["id"]
        st = self.get(f"/api/state?player={pid}")
        self.assertEqual((st["chapter"], st["cg_done"], st["budget"]["left"]), (1, False, 12))
        r = self.post("/api/session/start", {"player": pid, "npc": "xie_bugui"})
        self.assertEqual(r["status"], 400)                              # 先看 CG
        cg = self.get(f"/api/cg?player={pid}")
        self.assertEqual(cg["title"], "他是来杀你的")
        self.assertTrue(any(ln["who"] == "memory" for ln in cg["phases"][0]["lines"]))
        done = self.post("/api/cg/phase_done", {"player": pid})
        self.assertTrue(done["done"])
        evs = self.post("/api/session/start", {"player": pid, "npc": "xie_bugui"})
        self.assertEqual(evs[0]["t"], "session")
        self.assertEqual(evs[-1]["t"], "done")
        sid = evs[0]["session"]
        evs = self.post("/api/turn", {"session": sid, "kind": "option", "text": "天字房还空着。"})
        self.assertEqual(evs[-1]["t"], "done")
        self.assertIn("inner", evs[-1])
        self.assertEqual(self.get(f"/api/history?player={pid}")["npc"], "xie_bugui")
        self.post("/api/session/end", {"player": pid})
        r = self.post("/api/event", {"player": pid, "event": "e_brawl"})
        self.assertEqual(r["status"], 400)                              # 要选做法
        r = self.post("/api/event", {"player": pid, "event": "e_brawl", "choice": "act"})
        self.assertIn("左手一抄", r["note"])
        out = self.get(f"/api/cg?player={pid}&part=outro")
        self.assertTrue(out["phases"])
        self.assertEqual(self.post("/api/chapter/next", {"player": pid})["chapter"], 2)
        mind = self.get(f"/api/mind?player={pid}&npc=xie_bugui")
        self.assertIn("quotes", mind)
        r = self.post("/api/accuse", {"player": pid, "suspect": "yan_shouzheng", "clues": ["c1"]})
        self.assertEqual(r["status"], 400)                              # 还没到最后一章
        self.assertEqual(self.get("/healthz"), {"ok": True})

    def test_static(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"app.js", r.read())
        with urllib.request.urlopen(self.base + "/cg/README.txt") as r:   # 美术素材从内容包里找
            self.assertIn("cg01", r.read().decode())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../npc/runtime.py")


if __name__ == "__main__":
    unittest.main()
