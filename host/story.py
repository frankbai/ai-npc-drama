"""剧情导演：宿主一侧的"游戏逻辑"。

它读内容包里宿主自己的那部分（story/、events.json、clues.json、endings.json、pack.json），负责：
- 章节进度、CG 播放进度（开场 CG、章末钩子、结局）
- 每章开始时，按剧本锚点让运行时推演每个 NPC 的生活
- 自由对话的预算（每章共 N 轮、每人最多 M 轮）、谁这一章能聊、这次见面的补充说明
- 玩家在界面上触发的事件（经营、调查、有选择的事件、消息），翻译成运行时的事件
- 线索簿、剧情变量、条件判断、导演卡的版本
- 最后一章的指认和结局

运行时（npc/）不知道章节、CG、事件、线索是什么，它只收：阶段、事件、记忆、导演场景、专属台词指示。
换一个游戏时，这一层就是你自己的游戏服务器。
"""
from __future__ import annotations

import json
import operator
import threading
import time
import traceback
from pathlib import Path

from npc import prompts
from npc.runtime import Closed, Runtime

CN_NUM = "一二三四五六七八九十"
OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}
TABLES = (
    "CREATE TABLE IF NOT EXISTS story_progress (player TEXT PRIMARY KEY, chapter INTEGER, phase INTEGER, "
    "cg_done INTEGER, scene TEXT, choices TEXT DEFAULT '{}')",
    "CREATE TABLE IF NOT EXISTS story_vars (player TEXT, k TEXT, v INTEGER, PRIMARY KEY (player, k))",
    "CREATE TABLE IF NOT EXISTS story_clues (player TEXT, clue TEXT, chapter INTEGER, source TEXT, "
    "PRIMARY KEY (player, clue))",
    "CREATE TABLE IF NOT EXISTS done_events (player TEXT, chapter INTEGER, event TEXT, grp TEXT, choice TEXT, "
    "PRIMARY KEY (player, chapter, event))",
    "CREATE TABLE IF NOT EXISTS player_meta (player TEXT PRIMARY KEY, avatar TEXT)",
)


def _json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class Director:
    def __init__(self, root: Path, rt: Runtime):
        self.root = Path(root)
        self.rt = rt
        self.s = rt.s
        self.c = rt.c
        self.pack = self.c.pack
        self.ui = self.pack.get("ui") or {}
        self.chapters = {}
        for f in sorted((self.root / "story").glob("*.json")):
            ch = _json(f)
            self.chapters[int(ch["n"])] = ch
        if not self.chapters:
            raise ValueError("内容目录里没有章节（content/story/*.json）")
        self.last_chapter = max(self.chapters)
        ev = _json(self.root / "events.json", {"events": []})
        self.events = {e["id"]: e for e in ev.get("events", [])}
        cl = _json(self.root / "clues.json", {"clues": []})
        self.clues = {c["id"]: c for c in cl.get("clues", [])}
        self.weight_names = cl.get("weights") or {"2": "确凿", "1": "中", "0": "低"}
        en = _json(self.root / "endings.json", {}) or {}
        self.accuse_cfg = en.get("accuse")
        self.bond_cfg = en.get("bond") or {}
        parts = en.get("parts") or {}
        self.endings = []
        for e in en.get("endings", []):
            phases = []
            for ph in e.get("phases", []):
                if ph.get("include"):
                    phases.append({"type": "script", "lines": list(parts[ph["include"]])})
                else:
                    phases.append(ph)
            self.endings.append({**e, "phases": phases})
        self.groups = self.pack.get("groups") or [{"id": "经营", "limit": 2}, {"id": "调查", "limit": 1}]
        b = self.pack.get("budget") or {}
        self.per_chapter = int(b.get("per_chapter", 12))
        self.per_npc = int(b.get("per_npc", 6))
        for sql in TABLES:
            self.s.x(sql)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def lock(self, pid: str) -> threading.RLock:
        """每个玩家一把锁：同一个玩家的推进章节、做事件、指认、开始谈话这些操作排队做，避免连点两下做了两遍。"""
        with self._locks_guard:
            return self._locks.setdefault(pid, threading.RLock())

    # ================================================================ 工具
    def fill(self, text: str, pid: str, extra: dict | None = None) -> str:
        p = self.rt.player(pid)
        text = prompts.fill(text or "", p)
        for k, v in (extra or {}).items():
            text = text.replace(k, v)
        return text

    def chapter_label(self, n: int) -> str:
        ch = self.chapters[n]
        cn = CN_NUM[n - 1] if 1 <= n <= 10 else str(n)
        return (self.pack.get("chapter_label") or "第{cn}章 · {season}").replace("{cn}", cn).replace(
            "{n}", str(n)).replace("{season}", ch.get("season", ""))

    def progress(self, pid: str) -> dict:
        r = self.s.one("SELECT * FROM story_progress WHERE player=?", pid)
        if not r:
            self.s.x("INSERT INTO story_progress(player,chapter,phase,cg_done,scene,choices) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT DO NOTHING", pid, 1, 0, 0, None, "{}")
            r = self.s.one("SELECT * FROM story_progress WHERE player=?", pid)
        r["choices"] = json.loads(r.get("choices") or "{}")
        return r

    def _set_choice(self, pid: str, key: str, value):
        self._set_choices(pid, {key: value})

    def _set_choices(self, pid: str, values: dict):
        with self.lock(pid):
            ch = self.progress(pid)["choices"]
            ch.update(values)
            self.s.x("UPDATE story_progress SET choices=? WHERE player=?", json.dumps(ch, ensure_ascii=False), pid)

    def available_npcs(self, pid: str, n: int | None = None) -> list[str]:
        n = n or self.progress(pid)["chapter"]
        return [k for k, npc in self.c.npcs.items() if npc.from_chapter <= n]

    def npc_cfg(self, n: int, npc_id: str) -> dict:
        return (self.chapters[n].get("npcs") or {}).get(npc_id) or {}

    # ================================================================ 变量、线索、条件
    def get_var(self, pid: str, k: str) -> int:
        r = self.s.one("SELECT v FROM story_vars WHERE player=? AND k=?", pid, k)
        if r:
            return int(r["v"])
        return int(((self.pack.get("vars") or {}).get(k) or {}).get("start", 0))

    def add_var(self, pid: str, k: str, d: int):
        v = self.get_var(pid, k) + int(d)
        self.s.x("INSERT INTO story_vars VALUES(?,?,?) ON CONFLICT(player, k) DO UPDATE SET v=excluded.v", pid, k, v)

    def has_clue(self, pid: str, cid: str) -> bool:
        return bool(self.s.one("SELECT 1 FROM story_clues WHERE player=? AND clue=?", pid, cid))

    def give(self, pid: str, ids, source: str = "") -> list[dict]:
        n = self.progress(pid)["chapter"]
        new = []
        for cid in ids or []:
            if cid in self.clues and not self.has_clue(pid, cid):
                self.s.x("INSERT INTO story_clues VALUES(?,?,?,?) ON CONFLICT DO NOTHING", pid, cid, n, source)
                new.append(self.clue_view(cid))
        return new

    def clue_view(self, cid: str) -> dict:
        c = self.clues[cid]
        w = int(c.get("weight", 0))
        return {"id": cid, "title": c["title"], "text": c.get("text", ""), "note": c.get("note", ""),
                "weight": w, "weight_name": self.weight_names.get(str(w), str(w))}

    def owned_clues(self, pid: str) -> list[dict]:
        rows = self.s.q("SELECT clue, chapter FROM story_clues WHERE player=? ORDER BY chapter, clue", pid)
        return [{**self.clue_view(r["clue"]), "chapter": r["chapter"]} for r in rows if r["clue"] in self.clues]

    def cond(self, pid: str, c, ctx: dict | None = None) -> bool:
        """条件。见 docs/内容包写作指南.md 的"条件"一节。"""
        if c is None or c is True or c == "else":
            return True
        if c is False:
            return False
        ctx = ctx or {}
        if "all" in c:
            return all(self.cond(pid, x, ctx) for x in c["all"])
        if "any" in c:
            return any(self.cond(pid, x, ctx) for x in c["any"])
        if "not" in c:
            return not self.cond(pid, c["not"], ctx)
        if "clue" in c:
            return self.has_clue(pid, c["clue"])
        if "done" in c:
            return bool(self.s.one("SELECT 1 FROM done_events WHERE player=? AND event=?", pid, c["done"]))
        for k in ("trust", "warmth"):
            if k in c:
                npc, op, v = c[k]
                return OPS[op](self.rt.bond_summary(pid, npc)[k], v)
        if "var" in c:
            name, op, v = c["var"]
            return OPS[op](self.get_var(pid, name), v)
        if "chapter" in c:
            op, v = c["chapter"]
            return OPS[op](self.progress(pid)["chapter"], v)
        if "accused_right" in c:
            return bool(ctx.get("accused_right")) == bool(c["accused_right"])
        for k in ("score", "bond"):
            if k in c:
                op, v = c[k]
                return OPS[op](ctx.get(k, 0), v)
        raise ValueError(f"看不懂的条件：{c}")

    # ================================================================ 玩家与章节
    def new_player(self, name: str, shop: str, gender: str = "", avatar: str = "") -> str:
        genders = [g["id"] for g in (self.pack.get("player") or {}).get("genders") or []]
        gender = gender if gender in genders else (genders[0] if genders else "")
        avatars = [a["id"] for a in (self.pack.get("player") or {}).get("avatars") or []]
        avatar = avatar if avatar in avatars else (avatars[0] if avatars else "")
        pid = self.rt.register_player(name, shop, gender)
        self.s.x("INSERT INTO player_meta VALUES(?,?) ON CONFLICT DO NOTHING", pid, avatar)
        self.enter_chapter(pid, 1)
        return pid

    def avatar_of(self, pid: str) -> str:
        r = self.s.one("SELECT avatar FROM player_meta WHERE player=?", pid)
        return r["avatar"] if r else ""

    def enter_chapter(self, pid: str, n: int):
        ch = self.chapters[n]
        npc_stage = {k: self.fill(v.get("stage", ""), pid) for k, v in (ch.get("npcs") or {}).items()}
        self.rt.set_stage(pid, n, {"label": self.chapter_label(n), "gap": ch.get("gap", ""), "npc_stage": npc_stage})
        self.progress(pid)
        self.s.x("UPDATE story_progress SET chapter=?, phase=0, cg_done=0, scene=NULL WHERE player=?", n, pid)
        # 导演卡的版本在进入这一章时就定下来（比如按此刻的信任），之后回看也不变
        for i, ph in enumerate(ch.get("cg", {}).get("phases", [])):
            if ph.get("variants"):
                self._set_choice(pid, f"variant:{n}:{i}", self._pick_variant(pid, ph["variants"]))
        # 后台推演每个 NPC 这段时日的生活；同时前台播放 CG，玩家不用等
        for npc_id in self.available_npcs(pid, n):
            anchor = self.npc_cfg(n, npc_id).get("anchor")
            if anchor:
                threading.Thread(target=self._plan_life, args=(pid, npc_id, anchor), daemon=True).start()

    def _plan_life(self, pid, npc_id, anchor):
        try:
            self.rt.plan_life(pid, npc_id, anchor)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _pick_variant(self, pid: str, variants: list[dict]) -> int | None:
        for i, v in enumerate(variants):
            if v.get("else") or self.cond(pid, v.get("if")):
                return i
        return None

    def _phase(self, pid: str, n: int, i: int) -> dict:
        ph = self.chapters[n]["cg"]["phases"][i]
        if not ph.get("variants"):
            return ph
        idx = self.progress(pid)["choices"].get(f"variant:{n}:{i}")
        merged = {k: v for k, v in ph.items() if k != "variants"}
        if idx is not None and idx < len(ph["variants"]):
            merged.update({k: v for k, v in ph["variants"][idx].items() if k not in ("if", "else")})
        return merged

    def _phases(self, pid: str, part: str) -> tuple[list[dict], dict]:
        pr = self.progress(pid)
        n = pr["chapter"]
        ch = self.chapters[n]
        if part == "outro":
            return list((ch.get("outro") or {}).get("phases") or []), {}
        if part == "ending":
            eid = pr["choices"].get("ending")
            e = next((x for x in self.endings if x["id"] == eid), None)
            if not e:
                return [], {}
            acc = pr["choices"].get("accuse") or {}
            names = {s["id"]: s["name"] for s in (self.accuse_cfg or {}).get("suspects", [])}
            extra = {"{证据}": "、".join(self.clues[c]["title"] for c in acc.get("clues", []) if c in self.clues),
                     "{凶手}": names.get(acc.get("suspect"), acc.get("suspect", ""))}
            return e["phases"], extra
        return [self._phase(pid, n, i) for i in range(len(ch["cg"]["phases"]))], {}

    # ================================================================ CG
    def _line(self, pid: str, ln: dict, extra: dict, p: dict) -> dict:
        who = ln["who"]
        item = {"who": who}
        if who in self.c.npcs:
            item["name"] = self.c.npcs[who].name
        elif who == "me":
            item["name"] = p["name"]
        elif who in ("other", "card"):
            item["name"] = ln.get("name", "")
        if ln.get("image"):
            item["image"] = ln["image"]
        if "slot" in ln:
            item["slot"] = ln["slot"]
            cached = self.s.one("SELECT text FROM lines WHERE player=? AND slot=?", pid, ln["slot"])
            if cached:
                expr, text = self.rt.split_line(who, cached["text"])
                item["expr"], item["text"] = ln.get("expr") or expr, text
            else:
                item["text"] = None
        else:
            item["text"] = self.fill(ln.get("text", ""), pid, extra)
            if who in self.c.npcs:
                item["expr"] = ln.get("expr") or self.c.npcs[who].expr_key("")
        return item

    def cg_payload(self, pid: str, part: str = "cg") -> dict:
        """当前章节的 CG（或章末钩子 outro、结局 ending），以及播放到了哪一段。台词里的占位符已替换。"""
        p = self.rt.player(pid)
        pr = self.progress(pid)
        n = pr["chapter"]
        ch = self.chapters[n]
        raw, extra = self._phases(pid, part)
        ctx = self._ending_ctx(pid, part)
        phases = []
        for ph in raw:
            if ph.get("type", "script") == "script":
                lines = [self._line(pid, ln, extra, p) for ln in ph.get("lines", []) if self.cond(pid, ln.get("if"), ctx)]
                phases.append({"type": "script", "image": ph.get("image"), "lines": lines})
            else:
                npc = self.c.npcs[ph["npc"]]
                phases.append({"type": "director", "npc": npc.id, "npc_name": npc.name, "image": ph.get("image"),
                               "rounds": ph.get("rounds", 4)})
        if part == "ending":
            e = next((x for x in self.endings if x["id"] == pr["choices"].get("ending")), {})
            title, image = e.get("title", ""), None
        else:
            title, image = ch["cg"].get("title", ch.get("title", "")), ch["cg"].get("image")
        return {"part": part, "chapter": n, "label": self.chapter_label(n), "title": title, "image": image,
                "phase": pr["phase"] if part == "cg" else 0, "done": bool(pr["cg_done"]) if part == "cg" else False,
                "scene": pr["scene"] if part == "cg" else None, "phases": phases,
                "me_name": p["name"], "me_avatar": self.avatar_of(pid)}

    def _ending_ctx(self, pid: str, part: str) -> dict:
        """结局里的台词可以按指认结果写条件（accused_right / score / bond）。"""
        return (self.progress(pid)["choices"].get("accuse") or {}) if part == "ending" else {}

    def compose_slot(self, pid: str, slot: str, part: str = "cg") -> dict:
        p = self.rt.player(pid)
        raw, extra = self._phases(pid, part)
        ctx = self._ending_ctx(pid, part)
        before = []
        for ph in raw:
            if ph.get("type", "script") != "script":
                continue
            for ln in ph.get("lines", []):
                if not self.cond(pid, ln.get("if"), ctx):
                    continue
                if ln.get("slot") == slot:
                    npc_id = ln["who"]
                    raw_text = self.rt.compose_line(pid, npc_id, slot, self.fill(ln["instruction"], pid, extra),
                                                    "\n".join(before[-16:]), ln.get("topics"), bool(ln.get("reveal")))
                    expr, text = self.rt.split_line(npc_id, raw_text)
                    return {"text": text, "expr": ln.get("expr") or expr}
                who = ln["who"]
                label = (self.c.npcs[who].name + "：" if who in self.c.npcs else f"{p['name']}：" if who == "me"
                         else f"{ln.get('name', '')}：" if who in ("other", "card") and ln.get("name") else
                         "（回忆）" if who == "memory" else "")
                if ln.get("text"):
                    before.append(label + self.fill(ln["text"], pid, extra))
        raise ValueError("这里没有这个空位")

    def start_scene(self, pid: str):
        with self.lock(pid):
            return self._start_scene(pid)

    def _start_scene(self, pid: str):
        pr = self.progress(pid)
        n = pr["chapter"]
        phases = self.chapters[n]["cg"]["phases"]
        if pr["cg_done"] or pr["phase"] >= len(phases) or phases[pr["phase"]].get("type") != "director":
            raise ValueError("现在没有可以开始的场景")
        old = self.s.session(pr["scene"]) if pr["scene"] else None
        if old and not old["ended"]:
            if self.s.messages(old["id"]):
                raise ValueError("这一幕已经开始了，刷新页面就能接着演")
            self.rt.close_session(old["id"])     # 上次没能开场（比如模型出错），作废重来
        ph = self._phase(pid, n, pr["phase"])
        keys = ("scene", "player_state", "beats", "key_lines", "rounds", "branch", "option_hint", "situation", "stage")
        spec = {}
        for k in keys:
            v = ph.get(k)
            if v is None:
                continue
            spec[k] = self.fill(v, pid) if isinstance(v, str) else [self.fill(x, pid) for x in v] if isinstance(v, list) else v
        sid, gen = self.rt.open_scene(pid, ph["npc"], spec)
        self.s.x("UPDATE story_progress SET scene=? WHERE player=?", sid, pid)
        return sid, gen

    def phase_done(self, pid: str, phase: int | None = None) -> dict:
        """当前这一段 CG 播完了。phase：页面以为自己在第几段；和服务器对不上（比如连点了两下）就不推进。"""
        with self.lock(pid):
            return self._phase_done(pid, phase)

    def _phase_done(self, pid: str, phase: int | None) -> dict:
        pr = self.progress(pid)
        n = pr["chapter"]
        ch = self.chapters[n]
        phases = ch["cg"]["phases"]
        if pr["cg_done"] or (phase is not None and int(phase) != pr["phase"]):
            out = self.cg_payload(pid)
            out["new_clues"] = []
            return out
        if pr["scene"]:
            threading.Thread(target=self.rt.close_session, args=(pr["scene"],), daemon=True).start()
        ph = self._phase(pid, n, pr["phase"])
        new = self.give(pid, ph.get("gives"), f"cg:{n}")
        nxt = pr["phase"] + 1
        if nxt >= len(phases):
            new += self.finish_cg(pid, ch)
        else:
            self.s.x("UPDATE story_progress SET phase=?, scene=NULL WHERE player=?", nxt, pid)
        out = self.cg_payload(pid)
        out["new_clues"] = new
        return out

    def _apply_memory(self, pid: str, block: dict):
        for npc_id, m in (block.get("memory") or {}).items():
            items = [{"text": m}] if isinstance(m, str) else m
            for it in items:
                if self.cond(pid, it.get("if")):
                    self.rt.remember(pid, npc_id, it["text"], salience=5)
        if block.get("player_note"):
            self.rt.ingest({"kind": "note", "player": pid, "text": self.fill(block["player_note"], pid), "tag": "剧情"})

    def finish_cg(self, pid: str, ch: dict) -> list[dict]:
        self.s.x("UPDATE story_progress SET cg_done=1, scene=NULL WHERE player=?", pid)
        self._apply_memory(pid, ch)
        return self.give(pid, ch["cg"].get("gives"), f"cg:{ch['n']}")

    def next_chapter(self, pid: str) -> int:
        with self.lock(pid):
            return self._next_chapter(pid)

    def _next_chapter(self, pid: str) -> int:
        pr = self.progress(pid)
        n = pr["chapter"]
        ch = self.chapters[n]
        if not pr["cg_done"]:
            raise ValueError("先看完这一章的剧情")
        if n >= self.last_chapter or ch.get("final"):
            raise ValueError("这是最后一章了")
        if ch.get("require_talk", True) and self.rt.rounds_used(pid) < 1:
            raise ValueError(self.ui.get("need_talk", "这段时日还没找任何人说过话。"))
        self.close_active(pid)
        outro = ch.get("outro") or {}
        self._apply_memory(pid, outro)
        self.give(pid, outro.get("gives"), f"outro:{n}")
        self.enter_chapter(pid, n + 1)
        return n + 1

    def active_session(self, pid: str) -> str | None:
        """玩家正在进行的自由对话（存在库里，服务重启后也找得回来）。"""
        r = self.s.one("SELECT id FROM sessions WHERE player=? AND kind='chat' AND ended IS NULL "
                       "ORDER BY started DESC LIMIT 1", pid)
        return r["id"] if r else None

    def close_active(self, pid: str, sid: str | None = None):
        """结束自由对话：给了 sid 就只结束这一次，否则结束这个玩家所有没结束的。记忆在后台整理。"""
        if sid:
            ids = [sid]
        else:
            ids = [r["id"] for r in self.s.q(
                "SELECT id FROM sessions WHERE player=? AND kind='chat' AND ended IS NULL", pid)]
        for x in ids:
            self.s.x("UPDATE sessions SET ended=? WHERE id=? AND ended IS NULL", time.time(), x)
            threading.Thread(target=self.rt.close_session, args=(x,), daemon=True).start()

    # ================================================================ 自由对话
    def budget(self, pid: str) -> dict:
        used = self.rt.rounds_used(pid)
        left = max(0, self.per_chapter - used)
        by = {}
        for npc_id in self.available_npcs(pid):
            u = self.rt.rounds_used(pid, npc_id)
            by[npc_id] = {"used": u, "left": max(0, min(self.per_npc - u, left))}
        return {"per_chapter": self.per_chapter, "per_npc": self.per_npc, "used": used, "left": left, "by_npc": by}

    def situation(self, pid: str, npc_id: str) -> str:
        n = self.progress(pid)["chapter"]
        cfg = self.npc_cfg(n, npc_id)
        parts = [cfg.get("situation", "")]
        parts += [nt["text"] for nt in cfg.get("notes", []) if self.cond(pid, nt.get("if"))]
        for vn in self.c.npcs[npc_id].var_notes:
            if n <= int(vn.get("until", 99)) and self.get_var(pid, vn["var"]) >= int(vn.get("min", 1)):
                parts.append(vn["text"])
        return self.fill("\n".join(p for p in parts if p), pid)

    def start_chat(self, pid: str, npc_id: str):
        with self.lock(pid):
            return self._start_chat(pid, npc_id)

    def _start_chat(self, pid: str, npc_id: str):
        pr = self.progress(pid)
        if not pr["cg_done"]:
            raise ValueError("先看完这一章的剧情")
        if npc_id not in self.available_npcs(pid):
            raise ValueError("这一章还见不到这个人")
        if pr["choices"].get("ending"):
            raise ValueError("故事已经结束了")
        u = self.rt.rounds_used(pid, npc_id)
        total = self.rt.rounds_used(pid)
        npc = self.c.npcs[npc_id]
        if u >= self.per_npc:
            raise Closed(npc.closed_line)
        if total >= self.per_chapter:
            raise Closed(self.ui.get("lamp_out", "这段时日就到这里了。"))
        cfg = self.npc_cfg(pr["chapter"], npc_id)
        self.close_active(pid)
        return self.rt.open_session(pid, npc_id, self.per_npc, self.situation(pid, npc_id),
                                    cfg.get("arrive"), cfg.get("place"), total_limit=self.per_chapter,
                                    closed_text=self.ui.get("lamp_out", "这段时日就到这里了。"))

    # ================================================================ 事件
    def _limits(self, n: int) -> dict:
        lim = {g["id"]: g.get("limit") for g in self.groups}
        lim.update(self.chapters[n].get("limits") or {})
        return lim

    def _event_open(self, pid: str, e: dict, n: int) -> bool:
        a, b = e.get("chapters", [1, self.last_chapter])
        return a <= n <= b and self.cond(pid, e.get("if"))

    def available_events(self, pid: str) -> dict:
        pr = self.progress(pid)
        n = pr["chapter"]
        this = {r["event"]: r for r in self.s.q("SELECT * FROM done_events WHERE player=? AND chapter=?", pid, n)}
        ever = {r["event"] for r in self.s.q("SELECT event FROM done_events WHERE player=?", pid)}
        limits = self._limits(n)
        out = []
        for g in self.groups:
            gid = g["id"]
            items = []
            for e in self.events.values():
                if e["group"] != gid or not self._event_open(pid, e, n):
                    continue
                if e.get("once") and e["id"] in ever and e["id"] not in this:
                    continue                   # 整局只做一次、而且在之前的章节做过了：不再出现
                done = e["id"] in this
                item = {"id": e["id"], "label": self.fill(e["label"], pid), "done": done}
                if e.get("choices"):
                    item["intro"] = self.fill(e.get("intro", ""), pid)
                    item["choices"] = [{"id": c["id"], "label": c["label"]} for c in e["choices"]]
                    if done:
                        item["chosen"] = this[e["id"]]["choice"]
                items.append(item)
            lim = limits.get(gid)
            used = sum(1 for r in this.values() if r["grp"] == gid)
            left = None if lim is None else max(0, lim - used)
            if lim == 0 and not items:
                continue
            out.append({"id": gid, "hint": (g.get("hint") or "").replace("{n}", str(left if left is not None else "")),
                        "left": left, "items": items})
        return {"groups": out}

    def do_event(self, pid: str, event_id: str, choice: str | None = None) -> dict:
        with self.lock(pid):
            return self._do_event(pid, event_id, choice)

    def _do_event(self, pid: str, event_id: str, choice: str | None = None) -> dict:
        pr = self.progress(pid)
        n = pr["chapter"]
        if not pr["cg_done"]:
            raise ValueError("先看完这一章的剧情")
        if pr["choices"].get("ending"):
            raise ValueError("故事已经结束了")
        e = self.events.get(event_id)
        if not e:
            raise ValueError("没有这件事")
        if not self._event_open(pid, e, n):
            raise ValueError("这件事眼下不会发生")
        if self.s.one("SELECT 1 FROM done_events WHERE player=? AND chapter=? AND event=?", pid, n, event_id) or (
                e.get("once") and self.s.one("SELECT 1 FROM done_events WHERE player=? AND event=?", pid, event_id)):
            raise ValueError("这件事已经做过了")
        grp = e["group"]
        lim = self._limits(n).get(grp)
        if lim is not None:
            used = self.s.one("SELECT COUNT(*) n FROM done_events WHERE player=? AND chapter=? AND grp=?", pid, n, grp)["n"]
            if used >= lim:
                raise ValueError(f"这段时日的{grp}已经做够 {lim} 件了")
        eff = e
        if e.get("choices"):
            eff = next((c for c in e["choices"] if c["id"] == choice), None)
            if not eff:
                raise ValueError("请选一个做法")
        if e.get("special") == "rumors":
            note = self.rumors(pid)
            public = note
        else:
            note = self.fill(eff.get("note", ""), pid)
            public = self.fill(eff.get("public") or e.get("public") or "", pid)
        self.s.x("INSERT INTO done_events VALUES(?,?,?,?,?)", pid, n, event_id, grp, choice)
        if note:
            self.rt.ingest({"kind": "note", "player": pid, "text": note, "tag": grp})
        for key, k in (eff.get("knows") or {}).items():
            targets = self.available_npcs(pid) if key == "*" else [key] if key in self.available_npcs(pid) else []
            how = k.get("how", "none")
            if how not in ("seen", "heard"):
                continue
            for t in targets:
                self.rt.ingest({"kind": "heard", "player": pid, "npc": t, "text": self.fill(k.get("text", ""), pid),
                                "source": "你亲眼看见" if how == "seen" else k.get("source", "有人"),
                                "certainty": "高" if how == "seen" else k.get("certainty", "中")})
        if public:
            self.rt.ingest({"kind": "news", "player": pid, "text": public})
        for var, d in (eff.get("set") or {}).items():
            self.add_var(pid, var, d)
        new = self.give(pid, eff.get("gives"), f"event:{event_id}")
        return {"note": note, "new_clues": new}

    def rumors(self, pid: str) -> str:
        """江湖传言：用其他真实玩家的店名（全服共享的一个例子），不够就用内容包里的凑数名单。"""
        cfg = self.pack.get("rumors") or {}
        rows = self.s.q("SELECT shop FROM players WHERE id != ? ORDER BY created DESC LIMIT 3", pid)
        shops = [r["shop"] for r in rows if r["shop"]]
        for f in cfg.get("fillers", []):
            if len(shops) >= 3:
                break
            if f not in shops:
                shops.append(f)
        text = cfg.get("template", "")
        for i, s in enumerate(shops[:3]):
            text = text.replace("{%d}" % (i + 1), s)
        return text

    # ================================================================ 指认与结局
    def can_accuse(self, pid: str) -> bool:
        pr = self.progress(pid)
        return bool(self.accuse_cfg and pr["chapter"] == int(self.accuse_cfg.get("chapter", self.last_chapter))
                    and pr["cg_done"] and not pr["choices"].get("ending"))

    def bond_score(self, pid: str) -> int:
        cfg = self.bond_cfg
        if not cfg.get("npc"):
            return 0
        measure = cfg.get("measure") or ["trust"]
        hist = self.rt.bond_history(pid, cfg["npc"])
        if not hist:
            return 0

        def peak(r):
            return r["max_sum"] if set(measure) == {"trust", "warmth"} else sum(r[f"max_{m}"] for m in measure)
        until = int(cfg.get("peak_until", self.last_chapter))
        best = max((peak(r) for r in hist if r["stage"] <= until), default=0)
        n = self.progress(pid)["chapter"]
        last = next((r for r in hist if r["stage"] == n), None)
        delta = sum(last[f"last_{m}"] - last[f"start_{m}"] for m in measure) if last and n > until else 0
        return best + delta

    def accuse(self, pid: str, suspect: str, clue_ids: list[str]) -> dict:
        with self.lock(pid):
            return self._accuse(pid, suspect, clue_ids)

    def _accuse(self, pid: str, suspect: str, clue_ids: list[str]) -> dict:
        cfg = self.accuse_cfg
        if not self.can_accuse(pid):
            raise ValueError("现在还不能指认")
        names = {s["id"]: s["name"] for s in cfg.get("suspects", [])}
        if suspect not in names:
            raise ValueError("没有这个人")
        ids = []
        for c in clue_ids or []:
            if c not in ids:
                ids.append(c)
        if not ids or len(ids) > int(cfg.get("pick", 3)):
            raise ValueError(f"请选 1 到 {cfg.get('pick', 3)} 条证据")
        for c in ids:
            if not self.has_clue(pid, c):
                raise ValueError("你手里没有这条证据")
        score = sum(int(self.clues[c].get("weight", 0)) for c in ids
                    if "*" in self.clues[c].get("against", []) or suspect in self.clues[c].get("against", []))
        right = suspect == cfg.get("culprit")
        ctx = {"accused_right": right, "score": score, "bond": self.bond_score(pid)}
        ending = next((e for e in self.endings if e.get("if") == "else" or self.cond(pid, e.get("if"), ctx)), None)
        if not ending:
            raise ValueError("内容包里没有匹配的结局")
        self.close_active(pid)
        self._set_choices(pid, {"accuse": {"suspect": suspect, "clues": ids, **ctx}, "ending": ending["id"]})
        return {"ending": ending["id"], "title": ending.get("title", ""), **ctx}

    def epilogue(self, pid: str) -> dict:
        pr = self.progress(pid)
        e = next((x for x in self.endings if x["id"] == pr["choices"].get("ending")), None)
        people = []
        for npc_id in self.available_npcs(pid):
            b = self.rt.view(pid, npc_id)
            people.append({"id": npc_id, "name": self.c.npcs[npc_id].name, "impression": b["impression"],
                           "trust": b["bond"]["trust"], "warmth": b["bond"]["warmth"], "meetings": b["bond"]["meetings"],
                           "quotes": [q["text"] for q in b["quotes"][:2]]})     # 他记住的你的原话（最近的两句）
        bn = self.c.npcs.get(self.bond_cfg.get("npc", ""))
        label = (self.bond_cfg.get("label") or (f"和{bn.name}的情分" if bn else "")) if bn else ""
        return {"ending": {"id": e["id"], "title": e.get("title", "")} if e else None,
                "accuse": pr["choices"].get("accuse"), "people": people, "the_end": self.ui.get("the_end", ""),
                "bond_label": label}

    # ================================================================ 给界面的状态
    def people(self, pid: str) -> list[dict]:
        n = self.progress(pid)["chapter"]
        bud = self.budget(pid)
        out = []
        for npc_id in self.available_npcs(pid):
            npc = self.c.npcs[npc_id]
            cfg = self.npc_cfg(n, npc_id)
            visible, label = self.rt.inner_visibility(pid, npc_id)
            out.append({"id": npc_id, "name": npc.name, "title": npc.title, "card": npc.card,
                        "place": cfg.get("place") or npc.place,
                        "place_desc": cfg.get("place_desc") or ("" if cfg.get("place") else npc.place_desc),
                        "expr": self.rt.last_expr(pid, npc_id), "bond": self.rt.bond_summary(pid, npc_id),
                        "rounds": bud["by_npc"].get(npc_id), "inner_visible": visible, "inner_label": label})
        return out

    def state(self, pid: str) -> dict:
        pr = self.progress(pid)
        n = pr["chapter"]
        ch = self.chapters[n]
        bud = self.budget(pid)
        ended = bool(pr["choices"].get("ending"))
        return {
            "chapter": n, "label": self.chapter_label(n), "chapter_title": ch.get("title", ""),
            "cg_done": bool(pr["cg_done"]), "budget": bud, "people": self.people(pid),
            "has_outro": bool((ch.get("outro") or {}).get("phases")),
            "can_advance": bool(pr["cg_done"]) and n < self.last_chapter and not ch.get("final")
            and (not ch.get("require_talk", True) or bud["used"] >= 1),
            "can_accuse": self.can_accuse(pid), "ended": ended,
            "ending": pr["choices"].get("ending"),
            "events": self.available_events(pid),
            "journal": self.s.q("SELECT stage, text, kind FROM journal WHERE player=? ORDER BY id DESC", pid),
            "clues": self.owned_clues(pid),
            "me_avatar": self.avatar_of(pid),
            "active_session": self.active_session(pid),
        }

    def static_info(self) -> dict:
        """不随玩家变化的信息：标题、文案、人物、建档选项、指认配置。"""
        npcs = [{"id": k, "name": v.name, "title": v.title, "expressions": v.expressions, "inner_mode": v.inner_mode,
                 "from_chapter": v.from_chapter, "card": v.card} for k, v in self.c.npcs.items()]
        acc = None
        if self.accuse_cfg:
            acc = {k: self.accuse_cfg.get(k) for k in ("title", "prompt", "suspects", "pick")}
        player = self.pack.get("player") or {}
        return {"title": self.pack.get("title", ""), "subtitle": self.pack.get("subtitle", ""),
                "seal": self.pack.get("seal", ""), "intro": self.pack.get("intro", ""),
                "login_hint": self.pack.get("login_hint", ""), "ui": self.ui, "npcs": npcs,
                "player": {k: player.get(k) for k in ("name", "shop", "genders", "avatars")},
                "gestures": self.pack.get("gestures") or [], "accuse": acc, "last_chapter": self.last_chapter}
