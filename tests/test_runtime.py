"""运行时与剧情导演的通用能力（用 tests/fixtures/pack 这个小内容包，和《雪痕》无关）。

python3 -m unittest discover -s tests
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

from host.story import Director  # noqa: E402
from npc import content as content_mod  # noqa: E402
from npc import prompts  # noqa: E402
from npc.gateway import MockGateway  # noqa: E402
from npc.runtime import Closed, Runtime  # noqa: E402
from npc.store import Store  # noqa: E402

PACK = ROOT / "tests" / "fixtures" / "pack"


class Scripted(MockGateway):
    """模拟网关：可以指定记忆整理的输出，并记下每次调用的提示词。"""

    def __init__(self):
        super().__init__({"mock_delay": 0})
        self.reflect_out = None
        self.bond = None
        self.calls = []

    def stream(self, messages, max_tokens=1200, fast=False):
        self.calls.append(messages)
        if self.bond is not None and self._purpose(messages) == "turn":
            state = {"bond": self.bond, "why": "测试", "inner": "心里话", "leave": False}
            yield "〔平静〕好。\n<<<\n" + json.dumps(state, ensure_ascii=False)
            return
        yield from super().stream(messages, max_tokens, fast)

    def json(self, messages, max_tokens=1500, fast=False):
        self.calls.append(messages)
        if self._purpose(messages) == "reflect" and self.reflect_out is not None:
            return self.reflect_out
        return super().json(messages, max_tokens, fast)


def drain(g):
    return list(g)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = content_mod.load(PACK)
        self.g = Scripted()
        self.rt = Runtime(self.c, Store(Path(self.tmp.name) / "t.sqlite3"), self.g)
        self.d = Director(PACK, self.rt)
        self.p = self.d.new_player("阿青", "青店", "m", "me_1")
        time.sleep(0.05)

    def tearDown(self):
        time.sleep(0.05)                 # 等后台的生活推演写完
        self.rt.s.close()
        self.tmp.cleanup()

    def system_of(self, purpose):
        for m in reversed(self.g.calls):
            if f"[用途:{purpose}]" in m[0]["content"]:
                return m[0]["content"]
        return ""

    def talk(self, npc="alpha"):
        """说一轮话再告辞（进入下一章要求这一章至少和人说过一轮）。"""
        sid, g = self.d.start_chat(self.p, npc)
        drain(g)
        drain(self.rt.say(sid, "say", "嗯"))
        self.rt.close_session(sid)

    def play_cg(self):
        while not self.d.cg_payload(self.p)["done"]:
            cg = self.d.cg_payload(self.p)
            ph = cg["phases"][cg["phase"]]
            if ph["type"] == "director":
                sid, g = self.d.start_scene(self.p)
                drain(g)
            self.d.phase_done(self.p)

    # ---------------------------------------------------------------- 玩家与提示词
    def test_gender_placeholders_and_intro(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        sys_ = self.system_of("turn")
        self.assertIn("阿青，「青店」的主人。他是个好人。", sys_)
        self.assertIn("初识。你叫他朋友。", sys_)
        self.assertNotIn("{ta}", sys_)

    def test_bond_start_and_log(self):
        b = self.rt.bond_summary(self.p, "alpha")
        self.assertEqual((b["trust"], b["warmth"]), (1, 2))
        self.play_cg()
        self.g.bond = {"trust": 2, "warmth": 1}
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "你好"))
        log = self.rt.bond_history(self.p, "alpha")[0]
        self.assertEqual((log["start_trust"], log["last_trust"]), (1, 5))     # 开场一句 + 一轮，各 +2
        self.assertEqual(log["max_sum"], 5 + 4)

    def test_persona_layers_in_options(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        self.assertNotIn("玉佩在井里", self.system_of("options"))
        drain(self.rt.say(sid, "say", "嗯"))
        self.rt.close_session(sid)
        self.d.next_chapter(self.p)
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        self.assertIn("玉佩在井里", self.system_of("options"))

    def test_inner_voice_modes(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        ev = drain(g)[-1]
        self.assertTrue(ev["inner"])                            # always
        self.assertIn('"inner"', self.system_of("turn"))
        drain(self.rt.say(sid, "say", "嗯"))
        self.rt.close_session(sid)
        self.d.next_chapter(self.p)
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "beta")
        ev = drain(g)[-1]
        self.assertIsNone(ev["inner"])                          # trust 门槛没到
        self.assertEqual(ev["inner_label"], "看不透她")
        self.rt.s.x("UPDATE bond SET trust=9 WHERE player=? AND npc='beta'", self.p)
        ev = drain(self.rt.say(sid, "say", "说说看"))[-1]
        self.assertTrue(ev["inner"])
        self.assertNotIn("〔冷笑〕", self.system_of("turn"))   # 只给写死台词用的表情，不让模型选

    def test_quotes_must_be_verbatim(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "那口井我小时候掉进去过。"))
        self.g.reflect_out = {"diary": "聊了井。", "quotes": [
            {"topic": "井", "text": "那口井我小时候掉进去过"},       # 真说过（标点不同也算）
            {"topic": "井", "text": "我把玉佩扔进了井里"}]}           # 没说过：不收
        self.rt.close_session(sid)
        q = self.rt.view(self.p, "alpha")["quotes"]
        self.assertEqual([x["text"] for x in q], ["那口井我小时候掉进去过"])

    def test_relay_between_npcs(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "我昨晚去了井边。"))
        self.g.reflect_out = {"diary": "他说去了井边。", "relay": [
            {"to": "贝塔", "text": "阿青说他昨晚去了井边。"}, {"to": "不存在的人", "text": "乱传"}]}
        self.rt.close_session(sid)
        self.assertIn("对贝塔：什么都告诉贝塔。", self.system_of("reflect"))
        heard = self.rt.view(self.p, "beta")["heard"]
        self.assertEqual(len(heard), 1)
        self.assertEqual((heard[0]["source"], heard[0]["certainty"]), ("阿尔（转述）", "低"))
        self.assertEqual(self.rt.view(self.p, "alpha")["relayed"][0]["npc"], "beta")

    def test_truth_only_when_revealed(self):
        self.play_cg()
        self.talk()
        self.d.next_chapter(self.p)
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "beta")
        drain(g)
        self.assertNotIn("你就是凶手", self.system_of("turn"))
        self.rt.compose_line(self.p, "beta", "x1", "说一句", "", reveal=False)
        self.assertNotIn("你就是凶手", self.system_of("line"))
        self.rt.compose_line(self.p, "beta", "x2", "说一句", "", reveal=True)
        self.assertIn("你就是凶手", self.system_of("line"))

    def test_compose_line_picks_topic_quotes(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")
        drain(g)
        drain(self.rt.say(sid, "say", "井水很凉。"))
        for i in range(2):
            drain(self.rt.say(sid, "say", f"今天天气不错{i}。"))
        self.rt.close_session(sid)
        self.rt.compose_line(self.p, "alpha", "x", "说一句", "", topics=["井"])
        user = [m for m in self.g.calls if "[用途:line]" in m[0]["content"]][-1][-1]["content"]
        self.assertIn("阿青说：井水很凉。", user)

    # ---------------------------------------------------------------- 导演
    def test_shared_budget(self):
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "alpha")       # 第 1 章只有阿尔：每人最多 3 轮
        drain(g)
        for _ in range(3):
            ev = drain(self.rt.say(sid, "say", "嗯"))[-1]
        self.assertTrue(ev["leave"])
        with self.assertRaises(Closed):
            self.d.start_chat(self.p, "alpha")
        with self.assertRaises(ValueError):
            self.d.start_chat(self.p, "beta")              # 第 2 章才出场
        self.d.next_chapter(self.p)
        self.play_cg()
        sid, g = self.d.start_chat(self.p, "beta")
        drain(g)
        for _ in range(3):
            drain(self.rt.say(sid, "say", "嗯"))
        self.rt.close_session(sid)
        sid, g = self.d.start_chat(self.p, "alpha")       # 全章共 4 轮，只剩 1 轮
        drain(g)
        ev = drain(self.rt.say(sid, "say", "嗯"))[-1]
        self.assertEqual(ev["rounds"]["limit"], 1)
        self.assertTrue(ev["leave"])
        with self.assertRaises(Closed):
            self.d.start_chat(self.p, "alpha")

    def test_variant_by_trust_and_gives(self):
        self.play_cg()
        self.assertTrue(self.d.has_clue(self.p, "k1"))       # CG 给的
        self.talk()
        self.rt.s.x("UPDATE bond SET trust=60 WHERE player=? AND npc='alpha'", self.p)
        self.d.next_chapter(self.p)
        self.assertTrue(self.d.has_clue(self.p, "k2"))       # 章末钩子给的
        sid, g = self.d.start_scene(self.p)
        drain(g)
        self.assertIn("高信任版本", self.system_of("turn"))
        self.d.phase_done(self.p)
        self.assertTrue(self.d.has_clue(self.p, "k3"))
        self.assertFalse(self.d.has_clue(self.p, "k4"))
        # 版本给的线索作为条件，影响"这次见面"的补充说明
        self.assertIn("你知道玉佩的事。", self.d.situation(self.p, "beta"))

    def test_choice_event_vars_and_knows(self):
        self.play_cg()
        with self.assertRaises(ValueError):
            self.d.do_event(self.p, "pick")                   # 要选做法
        r = self.d.do_event(self.p, "pick", "act")
        self.assertEqual(r["new_clues"][0]["id"], "k5")
        self.assertEqual(self.d.get_var(self.p, "暴露"), 1)
        self.assertIn("你听说阿青身手不凡。", self.d.situation(self.p, "alpha"))
        with self.assertRaises(ValueError):
            self.d.do_event(self.p, "pick", "no")            # once
        self.d.do_event(self.p, "biz")
        with self.assertRaises(ValueError):
            self.d.do_event(self.p, "biz2")                   # 经营每章 1 件
        self.assertEqual(self.rt.view(self.p, "alpha")["heard"][0]["text"], "阿青赚了钱。")

    def test_rumors_use_other_players(self):
        self.d.new_player("阿红", "红店", "f", "me_1")
        self.play_cg()
        r = self.d.do_event(self.p, "news")
        self.assertIn("「红店」", r["note"])
        self.assertIn("「甲店」", r["note"])
        self.assertIn("红店", self.rt.view(self.p, "alpha")["news"][0])

    def test_accuse_and_endings(self):
        self.play_cg()
        self.talk()
        with self.assertRaises(ValueError):
            self.d.accuse(self.p, "beta", ["k1"])            # 还没到最后一章
        self.d.next_chapter(self.p)
        self.play_cg()
        with self.assertRaises(ValueError):
            self.d.accuse(self.p, "beta", ["k3"])            # 手里没有这条
        with self.assertRaises(ValueError):
            self.d.next_chapter(self.p)                       # 最后一章不能再往下
        r = self.d.accuse(self.p, "beta", ["k1", "k2", "k4"])   # 2 + 1 + 1 = 4
        self.assertEqual((r["score"], r["accused_right"]), (4, True))
        self.assertEqual(r["ending"], "ok")                   # 情分不够
        cg = self.d.cg_payload(self.p, "ending")
        self.assertEqual(cg["phases"][0]["lines"][0]["text"], "你摆出了：一、二、四，指认了贝塔。")
        with self.assertRaises(ValueError):
            self.d.accuse(self.p, "beta", ["k1"])            # 只能指认一次

    def test_wrong_accusation_and_reveal_slot(self):
        self.play_cg()
        self.talk()
        self.d.next_chapter(self.p)
        self.play_cg()
        r = self.d.accuse(self.p, "alpha", ["k1", "k2"])       # k2 只能用来指认贝塔
        self.assertEqual(r["ending"], "bad")
        out = self.d.compose_slot(self.p, "s_beta", "ending")
        self.assertEqual(out["expr"], "cold")
        self.assertIn("你就是凶手", self.system_of("line"))

    def test_bond_score_peak_plus_final_delta(self):
        self.play_cg()
        self.rt.s.x("UPDATE bond SET trust=20, warmth=10 WHERE player=? AND npc='alpha'", self.p)
        self.rt.s.log_bond(self.p, "alpha", 1, 20, 10)       # 第 1 章最高 30
        self.rt.s.x("UPDATE bond SET trust=2, warmth=0 WHERE player=? AND npc='alpha'", self.p)
        self.talk()
        self.d.next_chapter(self.p)                           # 第 2 章起点 = 当前值
        self.rt.s.x("UPDATE bond SET trust=5 WHERE player=? AND npc='alpha'", self.p)
        b = self.rt.bond_summary(self.p, "alpha")
        self.rt.s.log_bond(self.p, "alpha", 2, b["trust"], b["warmth"])
        start = self.rt.bond_history(self.p, "alpha")[1]
        self.assertEqual(self.d.bond_score(self.p), 30 + (5 - start["start_trust"]) + (b["warmth"] - start["start_warmth"]))


if __name__ == "__main__":
    unittest.main()
