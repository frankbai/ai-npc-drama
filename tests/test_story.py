"""示例剧情《雪痕》：模拟模式下走完六章和三个结局（不调用模型）。

python3 -m unittest discover -s tests
设置 TEST_DATABASE_URL=postgresql://… 会在 PostgreSQL 上再跑一遍。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from npc.runtime import Closed  # noqa: E402


def drain(g):
    return list(g)


class StoryTest(unittest.TestCase):
    def make_host(self, d):
        os.environ.pop("ACCESS_CODE", None)
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("CONTENT_DIR", None)
        from host.server import Host
        return Host(Path(d))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = self.make_host(self.tmp.name)
        self.h.rt.g.delay = 0
        self.d = self.h.d
        self.p = self.h.new_player("叶舟", "归雁客栈", "f", "me_2", "local")
        time.sleep(0.1)

    def tearDown(self):
        time.sleep(0.1)
        self.h.store.close()
        self.tmp.cleanup()

    def last_system(self, purpose=("turn", "scene")):
        r = self.h.store.one(f"SELECT prompt FROM calls WHERE purpose IN ({','.join('?' * len(purpose))}) "
                             "ORDER BY id DESC LIMIT 1", *purpose)
        return json.loads(r["prompt"])[0]["content"]

    def play_cg(self):
        while not self.d.cg_payload(self.p)["done"]:
            cg = self.d.cg_payload(self.p)
            ph = cg["phases"][cg["phase"]]
            if ph["type"] == "script":
                for ln in ph["lines"]:
                    if "slot" in ln:
                        r = self.d.compose_slot(self.p, ln["slot"])
                        self.assertTrue(r["text"])
            else:
                sid, g = self.d.start_scene(self.p)
                evs = drain(g)
                for _ in range(8):
                    if evs[-1].get("leave"):
                        break
                    evs = drain(self.h.rt.say(sid, "option", "嗯"))
                self.assertTrue(evs[-1]["leave"])
            self.d.phase_done(self.p)

    def talk(self, npc="xie_bugui", n=1, text="你好"):
        sid, g = self.d.start_chat(self.p, npc)
        drain(g)
        ev = None
        for _ in range(n):
            ev = drain(self.h.rt.say(sid, "say", text))[-1]
        self.d.close_active(self.p)
        time.sleep(0.05)
        return sid, ev

    def go_to(self, n, before_next=None):
        while True:
            self.play_cg()
            cur = self.d.progress(self.p)["chapter"]
            if cur >= n:
                return
            if before_next:
                before_next(cur)
            self.talk()
            self.d.next_chapter(self.p)

    # ---------------------------------------------------------------- 测试
    def test_who_is_available(self):
        self.play_cg()
        self.assertEqual(self.d.available_npcs(self.p), ["xie_bugui"])
        with self.assertRaises(ValueError):
            self.d.start_chat(self.p, "yan_shouzheng")
        self.go_to(2)
        self.assertEqual(self.d.available_npcs(self.p), ["xie_bugui", "he_rendong"])
        self.go_to(3)
        self.assertEqual(len(self.d.available_npcs(self.p)), 3)

    def test_clues_from_cg_and_outro(self):
        self.go_to(2)
        self.assertTrue(self.d.has_clue(self.p, "c1"))
        self.assertTrue(self.d.has_clue(self.p, "c2"))
        self.go_to(3)
        self.assertTrue(self.d.has_clue(self.p, "c4"))      # 冬至那场戏
        self.assertFalse(self.d.has_clue(self.p, "c5"))
        self.go_to(4)
        self.assertTrue(self.d.has_clue(self.p, "c5"))      # 冬至夜的章末钩子

    def test_ch4_ledger_depends_on_her_trust(self):
        def warm_her(cur):
            if cur == 3:
                self.h.rt.s.x("UPDATE bond SET trust=12 WHERE player=? AND npc='he_rendong'", self.p)
        self.go_to(4, warm_her)
        self.assertTrue(self.d.has_clue(self.p, "c6a"))
        self.assertFalse(self.d.has_clue(self.p, "c6b"))
        self.talk("he_rendong")
        self.assertIn("爹那本旧账，你交给了掌柜", self.last_system())

    def test_ch4_ledger_low_trust(self):
        self.go_to(4)
        self.assertTrue(self.d.has_clue(self.p, "c6b"))
        self.talk("yan_shouzheng")
        self.assertIn("一个姓而已", self.last_system())         # 严守正知道账本在他手里（无辜版说法）

    def test_yan_never_knows_he_is_the_killer_in_chat(self):
        self.go_to(6)
        self.talk("yan_shouzheng")
        sys_ = self.last_system()
        self.assertNotIn("你就是凶手", sys_)
        self.assertIn("你来查看柴房的锁", sys_)                  # 第 6 章：是 NPC 来柴房找玩家
        self.assertNotIn('"inner"', sys_)                      # 心声从不显示，也就不要求模型写

    def test_ch6_errands_only_for_missing_clues(self):
        def visit_temple(cur):
            if cur == 3:
                self.d.do_event(self.p, "inv_temple")
        self.go_to(6, visit_temple)
        items = {e["id"] for g in self.d.available_events(self.p)["groups"] for e in g["items"]}
        self.assertNotIn("inv_errand_room", items)             # 何忍冬还不够信你：不肯替你跑腿
        self.h.store.set_bond_values(self.p, "he_rendong", 12, 30)
        items = {e["id"] for g in self.d.available_events(self.p)["groups"] for e in g["items"]}
        self.assertNotIn("inv_errand_temple", items)           # 已经有这条线索了
        self.assertIn("inv_errand_room", items)
        self.assertNotIn("biz_noodle", items)                  # 第 6 章不能经营

    def test_mask_slips_reach_yan(self):
        self.play_cg()
        self.d.do_event(self.p, "e_brawl", "act")
        self.go_to(3)
        self.assertIn("掌柜的手脚快得不像个开店的", self.d.situation(self.p, "yan_shouzheng"))
        self.assertIn("手脚快得吓人", self.d.situation(self.p, "xie_bugui"))

    def test_budget_twelve_shared(self):
        self.go_to(3)
        for npc in ("xie_bugui", "he_rendong"):
            sid, ev = self.talk(npc, 6)
            self.assertTrue(ev["leave"])
        with self.assertRaises(Closed):
            self.d.start_chat(self.p, "yan_shouzheng")
        self.assertEqual(self.d.budget(self.p)["left"], 0)

    def test_relay_reaches_yan(self):
        self.go_to(3)
        self.talk("he_rendong", 2, "那天晚上我听见后院有动静。")
        time.sleep(0.2)
        heard = self.h.rt.view(self.p, "yan_shouzheng")["heard"]
        self.assertTrue(any(h["source"] == "何忍冬（转述）" for h in heard))

    def finish(self, suspect, clues, bond):
        self.go_to(6)
        self.talk("he_rendong")
        self.d.bond_score = lambda pid: bond        # 情分直接指定，方便测三个结局
        r = self.d.accuse(self.p, suspect, clues)
        cg = self.d.cg_payload(self.p, "ending")
        slots = [ln for ph in cg["phases"] for ln in ph["lines"] if "slot" in ln]
        for ln in slots:
            self.assertTrue(self.d.compose_slot(self.p, ln["slot"], "ending")["text"])
        return r, cg, slots

    def test_ending_a(self):
        r, cg, slots = self.finish("yan_shouzheng", ["c1", "c2", "c4"], 30)
        self.assertEqual(r["score"], 4)
        self.assertEqual(r["ending"], "C")                    # 只有 4 分：不够

    def test_ending_a_with_hard_evidence(self):
        def temple(cur):
            if cur == 3:
                self.d.do_event(self.p, "inv_temple")
        self.go_to(6, temple)
        self.talk("he_rendong")
        self.d.bond_score = lambda pid: 30
        r = self.d.accuse(self.p, "yan_shouzheng", ["c1", "c3", "c2"])
        self.assertEqual((r["ending"], r["score"]), ("A", 5))
        cg = self.d.cg_payload(self.p, "ending")
        slots = [ln["slot"] for ph in cg["phases"] for ln in ph["lines"] if "slot" in ln]
        self.assertEqual(slots, ["end_yan", "end_xie_a"])
        self.d.compose_slot(self.p, "end_yan", "ending")
        self.assertIn("你就是凶手", self.last_system(("line:end_yan",)))   # 揭晓时才给"真相"
        self.assertIn("雪痕是右手刀、山神庙的炭灰和红穗子、封山的时辰对不上", json.dumps(cg, ensure_ascii=False))

    def test_ending_b_low_bond(self):
        def temple(cur):
            if cur == 3:
                self.d.do_event(self.p, "inv_temple")
        self.go_to(6, temple)
        self.talk("he_rendong")
        self.d.bond_score = lambda pid: 3
        r = self.d.accuse(self.p, "yan_shouzheng", ["c1", "c3", "c2"])
        self.assertEqual(r["ending"], "B")

    def test_ending_c_wrong_person(self):
        r, cg, slots = self.finish("lei", ["c1", "c2"], 30)
        self.assertEqual(r["ending"], "C")
        self.assertIn("凶手是雷镖头", json.dumps(cg, ensure_ascii=False))
        self.assertEqual([s["slot"] for s in slots], ["end_yan_c"])
        e = self.d.epilogue(self.p)
        self.assertEqual(e["ending"]["id"], "C")
        self.assertEqual(len(e["people"]), 3)

    def test_real_bond_score(self):
        self.go_to(5)
        self.h.rt.s.x("UPDATE bond SET trust=20, warmth=20 WHERE player=? AND npc='xie_bugui'", self.p)
        self.h.rt.s.log_bond(self.p, "xie_bugui", 4, 20, 20)      # 第 4 章的峰值
        self.h.rt.s.x("UPDATE bond SET trust=0, warmth=5 WHERE player=? AND npc='xie_bugui'", self.p)
        self.talk()
        self.d.next_chapter(self.p)                                 # 第 5 章的下跌不算
        self.assertEqual(self.d.bond_score(self.p), 40)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "设置 TEST_DATABASE_URL 才测试 PostgreSQL")
class PgStoryTest(StoryTest):
    def make_host(self, d):
        from npc.pgwire import Connection
        c = Connection(os.environ["TEST_DATABASE_URL"])
        c.script("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.close()
        os.environ.pop("ACCESS_CODE", None)
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        try:
            from host.server import Host
            return Host(Path(d))
        finally:
            os.environ.pop("DATABASE_URL", None)


if __name__ == "__main__":
    unittest.main()
