"""边界情况：连点、刷新、并发、模型乱输出、预算被别的会话用掉……（用 tests/fixtures/pack，模拟模式）。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from host.story import Director  # noqa: E402
from npc import content as content_mod  # noqa: E402
from npc.gateway import ModelError, extract_json  # noqa: E402
from npc.runtime import Closed, Runtime  # noqa: E402
from npc.store import Store  # noqa: E402
from test_runtime import PACK, Scripted, drain  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = content_mod.load(PACK)
        self.g = Scripted()
        self.rt = Runtime(self.c, Store(Path(self.tmp.name) / "t.sqlite3"), self.g)
        self.d = Director(PACK, self.rt)
        self.p = self.d.new_player("阿青", "青店", "m", "me_1")
        time.sleep(0.05)

    def tearDown(self):
        time.sleep(0.1)
        self.rt.s.close()
        self.tmp.cleanup()

    def to_chapter2(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "嗯"))
        self.rt.close_session(sid)
        self.d.next_chapter(self.p)


class FlowTest(Base):
    def test_phase_done_ignores_stale_phase(self):
        out = self.d.phase_done(self.p, phase=3)               # 页面以为在第 4 段：不推进
        self.assertFalse(out["done"])
        self.assertTrue(self.d.phase_done(self.p, phase=0)["done"])
        again = self.d.phase_done(self.p, phase=0)             # 连点第二下：什么也不发生
        self.assertTrue(again["done"])
        self.assertEqual(again["new_clues"], [])

    def test_scene_rounds_are_enforced(self):
        self.to_chapter2()
        sid, g = self.d.start_scene(self.p)                    # 这一幕只有 1 轮
        drain(g)
        ev = drain(self.rt.say(sid, "say", "你好"))[-1]
        self.assertTrue(ev["leave"])
        with self.assertRaises(ValueError):                     # 演完就结束了，不能接着说
            drain(self.rt.say(sid, "say", "再说一句"))

    def test_scene_is_not_restarted_after_reload(self):
        self.to_chapter2()
        sid, g = self.d.start_scene(self.p)
        drain(g)
        with self.assertRaises(ValueError):                     # 已经开场：不会再开一个
            self.d.start_scene(self.p)
        self.assertEqual(self.d.cg_payload(self.p)["scene"], sid)

    def test_failed_scene_start_can_retry(self):
        self.to_chapter2()
        sid, g = self.d.start_scene(self.p)                    # 开了场但一句也没说成（比如模型出错）
        sid2, g2 = self.d.start_scene(self.p)
        drain(g2)
        self.assertNotEqual(sid, sid2)
        self.assertTrue(self.rt.s.session(sid)["ended"])

    def test_budget_rechecked_every_turn(self):
        self.d.phase_done(self.p)
        stale, g = self.d.start_chat(self.p, "alpha")          # 每人 3 轮，全章 4 轮
        drain(g)
        self.rt.s.x("UPDATE sessions SET ended=NULL WHERE id=?", stale)
        # 模拟服务重启后旧会话还开着、全章的 4 轮已经在和别人的会话里用完了
        other = self.rt.s.new_session(self.p, "beta", 1, "chat", {})
        for _ in range(4):
            self.rt.s.add_message(other, "player", "say", "嗯")
        with self.assertRaises(Closed):                         # 旧会话也不能再多说
            drain(self.rt.say(stale, "say", "再说一句"))

    def test_start_chat_closes_every_open_chat(self):
        self.d.phase_done(self.p)
        a, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        b, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        self.assertTrue(self.rt.s.session(a)["ended"])
        self.assertEqual(self.d.active_session(self.p), b)

    def test_concurrent_events_respect_group_limit(self):
        self.d.phase_done(self.p)
        ok, errs = [], []

        def go(eid):
            try:
                ok.append(self.d.do_event(self.p, eid))
            except ValueError as e:
                errs.append(e)
        evs = [e for e in self.d.events.values() if e["group"] == "经营"]
        ts = [threading.Thread(target=go, args=(e["id"],)) for e in evs * 2]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n = self.rt.s.one("SELECT COUNT(*) n FROM done_events WHERE player=? AND grp='经营'", self.p)["n"]
        self.assertEqual(n, 1)                                  # 经营每章 1 件


class MemoryTest(Base):
    def test_reflection_keeps_bond_changes(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "嗯"))
        self.rt.s.set_bond_values(self.p, "alpha", 33, 44)     # 整理记忆期间信任变了
        self.g.reflect_out = {"diary": "聊了几句。", "impression": "人不错。", "facts": ["开店的"]}
        self.rt.close_session(sid)
        b = self.rt.s.bond(self.p, "alpha")
        self.assertEqual((b["trust"], b["warmth"], b["impression"], b["facts"]), (33, 44, "人不错。", ["开店的"]))

    def test_close_while_speaking_waits_for_the_line(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        gen = self.rt.say(sid, "say", "井边有人。")
        next(gen)                                               # 正说到一半
        self.rt.close_session(sid)                              # 这时候结束（比如切了存档）
        self.assertEqual(self.rt.s.session(sid)["reflected"], 0)
        drain(gen)                                              # 这一句说完了才整理
        time.sleep(0.2)
        s = self.rt.s.session(sid)
        self.assertEqual(s["reflected"], 1)
        self.assertIn("井边有人。", [m["text"] for m in self.rt.s.messages(sid)])

    def test_quotes_cannot_span_two_lines(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "他们说我杀"))
        drain(self.rt.say(sid, "say", "了你师父，是在撒谎。"))
        self.g.reflect_out = {"diary": "。", "quotes": [{"topic": "x", "text": "我杀了你师父"}]}
        self.rt.close_session(sid)
        self.assertEqual(self.rt.view(self.p, "alpha")["quotes"], [])

    def test_malformed_reflection_does_not_break(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "嗯"))
        self.g.reflect_out = {"diary": "。", "facts": "没有", "told": "也没有", "quotes": "无", "relay": {"to": "x"},
                              "commitments_new": None, "salience": True}
        self.rt.close_session(sid)
        b = self.rt.s.bond(self.p, "alpha")
        self.assertEqual(b["facts"], [])                        # 字符串不会被拆成一个个字
        self.assertEqual(self.rt.s.session(sid)["reflected"], 1)

    def test_reflection_failure_is_retried_later(self):
        self.d.phase_done(self.p)
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "嗯"))
        orig = self.g.json

        def boom(messages, max_tokens=1500, fast=False):
            if self.g._purpose(messages) == "reflect":
                raise RuntimeError("网络断了")
            return orig(messages, max_tokens, fast)
        self.g.json = boom
        self.rt.close_session(sid)                              # 不抛错
        self.assertEqual(self.rt.s.session(sid)["reflected"], 0)
        self.g.json = orig
        self.rt.reflect_pending(self.p)
        self.assertEqual(self.rt.s.session(sid)["reflected"], 1)

    def test_extract_json_requires_object(self):
        self.assertEqual(extract_json('好的：{"a": 1}'), {"a": 1})
        for bad in ("[1, 2]", '"字符串"', "没有"):
            with self.assertRaises(ModelError):
                extract_json(bad)


class HttpGuardTest(unittest.TestCase):
    """本机模式的防护：只收 JSON、只认 localhost、请求大小。"""

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

    def status(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                r.read()
                return r.status
        except urllib.error.HTTPError as e:
            e.read()
            e.close()
            return e.code

    def test_post_must_be_json(self):
        body = json.dumps({"base_url": "https://evil.example/v1"}).encode()
        self.assertEqual(self.status("/api/config", body, {"Content-Type": "text/plain"}), 415)
        self.assertNotEqual(self.server.HOST.cfg["base_url"], "https://evil.example/v1")

    def test_foreign_host_rejected_in_local_mode(self):
        self.assertEqual(self.status("/api/state", headers={"Host": "evil.example"}), 403)
        self.assertEqual(self.status("/api/state", headers={"Host": "localhost:8800"}), 200)

    def test_bad_content_length(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        c.putrequest("POST", "/api/login")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        r.read()
        c.close()

    def test_unknown_player_id_is_ignored(self):
        # 浏览器里记着别的程序留下的存档编号：不报错，当作没选存档
        with urllib.request.urlopen(self.base + "/api/state?player=p_00000000") as r:
            st = json.loads(r.read())
        self.assertNotIn("chapter", st)
        self.assertEqual(self.status("/api/cg?player=p_00000000"), 403)

    def test_history_hides_inner_voice(self):
        H = self.server.HOST
        pid = H.new_player("某", "某店", "m", "me_1", "local")
        H.d.phase_done(pid)
        H.store.x("UPDATE bond SET trust=0 WHERE player=?", pid)
        H.d.enter_chapter(pid, 2)      # 何忍冬第 2 章出场，信任不够时看不到她的心声
        H.d.phase_done(pid)
        H.store.x("UPDATE story_progress SET cg_done=1 WHERE player=?", pid)
        sid, g = H.d.start_chat(pid, "he_rendong")
        drain(g)
        drain(H.rt.say(sid, "say", "你好"))
        h = H.history(pid, None, "player")
        npc_lines = [m for m in h["messages"] if m["role"] == "npc"]
        self.assertTrue(npc_lines)
        self.assertTrue(all(m["inner"] is None and "meta" not in m for m in npc_lines))


if __name__ == "__main__":
    unittest.main()
