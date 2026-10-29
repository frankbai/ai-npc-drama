"""NPC 运行时：宿主唯一需要调用的东西。它不认识具体的游戏和剧情。

宿主可以做的事：
  register_player(name, shop, gender)            新玩家
  set_stage(player, n, info)                     玩家进入第 n 个阶段（Demo 里是章节），info 是给 NPC 看的阶段说明
  plan_life(player, npc, anchor)                 按剧本锚点推演 NPC 这一阶段的生活（失败就用锚点里的兜底）
  ingest(event)                                  世界上发生了什么：note / heard / news
  remember(player, npc, text)                    把一段"发生过的事"直接写进 NPC 的记忆（比如写死的 CG）
  open_session(player, npc, round_limit, ...)    自由对话；超过回合预算时抛 Closed，宿主可显示固定的话，不调用模型
  open_scene(player, npc, spec)                  导演场景：场景 + 必经事件 + 关键台词 + 轮数
  say(session, kind, text) / close_session(...)  说话、结束（结束后整理记忆：日记、原话、要转告的事……）
  compose_line(player, npc, slot, instruction, before, topics, reveal)
                                                 专属台词：按指示从记忆里写一句，结果缓存
  view(player, npc) / bond_history(player, npc)  心智面板、交情曲线
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Iterator

from . import prompts
from .content import Content
from .gateway import ModelError, extract_json
from .store import Store

MAX_EPISODES_IN_PROMPT = 16
MAX_TOLD_IN_PROMPT = 40
MAX_FACTS = 40
MAX_HEARD_IN_PROMPT = 12
DEFAULT_GESTURES = ["沉默了一会儿", "笑了笑", "看了看四周"]
_PUNCT = re.compile(r"[\s，。！？、；：“”‘’「」『』（）()《》〈〉…—\-,.!?;:'\"~～·]")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "")


class Closed(Exception):
    """回合预算已经用完。"""


def _lst(out: dict, key: str) -> list:
    """模型输出里应该是列表的字段：不是列表就当没有（防止把一个字符串拆成一个个字）。"""
    v = out.get(key)
    return v if isinstance(v, list) else []


class Runtime:
    def __init__(self, content: Content, store: Store, gateway):
        self.c = content
        self.s = store
        self.g = gateway
        self._locks: dict[str, threading.Lock] = {}   # 每个玩家一把锁：同一玩家的记忆整理、生活推演排队，不同玩家互不等待
        self._locks_guard = threading.Lock()
        self._session_mood: dict[str, str] = {}
        self._busy: set[str] = set()                  # 正在生成回复的会话
        self._reflect_after: set[str] = set()         # 生成途中被结束的会话：说完这一句再整理记忆
        self._busy_lock = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------ 玩家与阶段
    def register_player(self, name: str, shop: str, gender: str = "") -> str:
        pid = self.s.add_player(name, shop, gender)
        for npc_id in self.c.npcs:
            b = self._bond(pid, npc_id)
            self.s.log_bond(pid, npc_id, 1, b["trust"], b["warmth"])
        return pid

    def player(self, pid: str) -> dict | None:
        """玩家资料，附带性别对应的占位符（{ta} 等），提示词里用。"""
        p = self.s.player(pid)
        if p:
            p["words"] = self.c.gender_words(p.get("gender") or "")
        return p

    def stage(self, player: str) -> int:
        p = self.s.player(player)
        return (p and p["stage"]) or 1

    def set_stage(self, player: str, stage: int, info: dict):
        """info：label（如"第三章 · 冬至"）、gap（距上一阶段过了多久）、
        npc_stage（{npc_id: 这一阶段你们的关系}，也可以只给一个通用的 stage）。"""
        self.reflect_pending(player)
        self.s.set_stage(player, stage, info)
        for npc_id in self.c.npcs:                    # 记下每个 NPC 这一阶段交情的起点
            b = self._bond(player, npc_id)
            self.s.log_bond(player, npc_id, stage, b["trust"], b["warmth"])

    def _bond(self, player: str, npc_id: str) -> dict:
        npc = self.c.npcs.get(npc_id)
        return self.s.bond(player, npc_id, npc.bond_start if npc else None)

    # ------------------------------------------------------------------ 世界与记忆
    def ingest(self, ev: dict):
        """kind:
        note   只有玩家自己知道的经历（写进手记，用于选项）{player, text, tag?}
        heard  NPC 得知了关于玩家的事 {player, npc, text, source, certainty}
        news   消息 {text, player?}；player 为空表示全服都能听到
        """
        k = ev["kind"]
        if k == "note":
            self.s.x("INSERT INTO journal(player,stage,text,kind) VALUES(?,?,?,?)",
                     ev["player"], self.stage(ev["player"]), ev["text"], ev.get("tag", ""))
        elif k == "heard":
            self._check_npc(ev["npc"])
            self.s.x("INSERT INTO heard(player,npc,stage,text,source,certainty) VALUES(?,?,?,?,?,?)",
                     ev["player"], ev["npc"], self.stage(ev["player"]), ev["text"],
                     ev.get("source", ""), ev.get("certainty", "中"))
        elif k == "news":
            stage = self.stage(ev["player"]) if ev.get("player") else 0
            self.s.x("INSERT INTO news(player,stage,text,created) VALUES(?,?,?,?)",
                     ev.get("player"), stage, ev["text"], time.time())
        else:
            raise ValueError(f"未知事件类型：{k}")

    def remember(self, player: str, npc: str, text: str, salience: int = 5):
        p = self.player(player)
        self.s.x("INSERT INTO episodes(player,npc,stage,text,salience) VALUES(?,?,?,?,?)",
                 player, npc, self.stage(player), prompts.fill(text, p), salience)

    # ------------------------------------------------------------------ 生活推演
    def life(self, player: str, npc: str) -> dict:
        r = self.s.one("SELECT state FROM life WHERE player=? AND npc=? AND stage<=? ORDER BY stage DESC LIMIT 1",
                       player, npc, self.stage(player))
        return json.loads(r["state"]) if r else {}

    def plan_life(self, player: str, npc_id: str, anchor: dict):
        """按锚点推演这一阶段的生活。已经有了就不重复。失败时用 anchor['fallback']。"""
        stage = self.stage(player)
        with self._lock(f"life:{player}:{npc_id}"):     # 同一个人同一阶段只推演一次
            if self.s.one("SELECT 1 FROM life WHERE player=? AND npc=? AND stage=?", player, npc_id, stage):
                return
            self._plan_life(player, npc_id, anchor, stage)

    def _plan_life(self, player: str, npc_id: str, anchor: dict, stage: int):
        npc = self.c.npcs[npc_id]
        p = self.player(player)
        b = self._bond(player, npc_id)
        prev = self.s.one("SELECT state FROM life WHERE player=? AND npc=? AND stage<? ORDER BY stage DESC LIMIT 1",
                          player, npc_id, stage)
        recent = [r["text"] for r in self.s.q(
            "SELECT text FROM episodes WHERE player=? AND npc=? ORDER BY id DESC LIMIT 4", player, npc_id)][::-1]
        info = p["stage_info"]
        ctx = {"player": p, "label": info.get("label", ""), "gap": info.get("gap", ""),
               "stage": self._npc_stage(info, npc_id), "anchor": anchor,
               "previous": json.loads(prev["state"]) if prev else None,
               "impression": b["impression"], "recent": recent, "secret": npc.secret.render(stage)}
        msgs = prompts.life_messages(npc, ctx)
        t0 = time.time()
        try:
            state = self.g.json(msgs, max_tokens=1200)
            if not isinstance(state.get("doing"), str) or not state["doing"].strip():
                raise ModelError("推演结果缺少内容")
            self.s.log_call(None, f"life:{npc_id}", msgs, json.dumps(state, ensure_ascii=False), _ms(t0))
        except Exception as e:  # noqa: BLE001  任何失败都用剧本锚点兜底
            self.s.log_call(None, f"life:{npc_id}", msgs, f"ERROR {e}", _ms(t0))
            state = dict(anchor.get("fallback") or {})
            state["_fallback"] = True
        self.s.x("INSERT INTO life(player,npc,stage,state) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                 player, npc_id, stage, json.dumps(state, ensure_ascii=False))

    @staticmethod
    def _npc_stage(info: dict, npc_id: str) -> str:
        return (info.get("npc_stage") or {}).get(npc_id) or info.get("stage", "")

    # ------------------------------------------------------------------ 会话
    def rounds_used(self, player: str, npc: str | None = None) -> int:
        """这一阶段自由对话已经说了几轮。npc 为空时，算所有 NPC 的总数。"""
        sql = """SELECT COUNT(*) n FROM messages m JOIN sessions s ON m.session = s.id
                 WHERE s.player=? AND s.stage=? AND s.kind='chat' AND m.role='player' AND m.kind != 'arrive'"""
        args = [player, self.stage(player)]
        if npc:
            sql += " AND s.npc=?"
            args.append(npc)
        r = self.s.one(sql, *args)
        return r["n"] if r else 0

    def open_session(self, player: str, npc: str, round_limit: int | None = None, situation: str = "",
                     arrive: str | None = None, place: str | None = None, total_limit: int | None = None,
                     closed_text: str = "") -> tuple[str, Iterator[dict]]:
        """自由对话。
        round_limit：这一阶段和这个 NPC 最多几轮（玩家一句 + NPC 一句算一轮）。
        total_limit：这一阶段和所有 NPC 加起来最多几轮（几个 NPC 共用的预算）。每一轮都会重新检查。
        closed_text：总预算用完时显示的话（不调用模型）。
        situation：这次见面的补充说明（宿主根据剧情、变量写的），会放进提示词。
        arrive / place：覆盖默认的"某某来到某地"（比如玩家被关着，是 NPC 来找玩家）。"""
        self._check_npc(npc)
        p = self.player(player)
        if not p:
            raise ValueError("没有这个玩家")
        self._check_budget(player, npc, round_limit, total_limit, closed_text)
        self.reflect_pending(player)
        place = place or self.c.npcs[npc].place
        spec = {"round_limit": round_limit, "total_limit": total_limit, "closed_text": closed_text,
                "situation": situation, "place": place, "custom_arrive": bool(arrive)}
        sid = self.s.new_session(player, npc, self.stage(player), "chat", spec)
        text = prompts.fill(arrive, p) if arrive else f"（{p['name']}来到{place}。请你先开口。）"
        return sid, self._turn(sid, "arrive", text)

    def _check_budget(self, player, npc, round_limit, total_limit, closed_text):
        """再说一句会不会超预算。超了抛 Closed（不调用模型）。"""
        if round_limit is not None and self.rounds_used(player, npc) >= round_limit:
            raise Closed(self.c.npcs[npc].closed_line)
        if total_limit is not None and self.rounds_used(player) >= total_limit:
            raise Closed(closed_text or self.c.npcs[npc].closed_line)

    def _left_after(self, player, npc, spec, this_round: int) -> tuple[int | None, int | None]:
        """这一句说完后还剩几轮（取和这个人的、和所有人的两者中较少的），以及和这个人已经说了几轮。"""
        npc_used = self.rounds_used(player, npc) + this_round
        lefts = []
        if spec.get("round_limit") is not None:
            lefts.append(int(spec["round_limit"]) - npc_used)
        if spec.get("total_limit") is not None:
            lefts.append(int(spec["total_limit"]) - (self.rounds_used(player) + this_round))
        return (min(lefts) if lefts else None), npc_used

    def open_scene(self, player: str, npc: str, spec: dict) -> tuple[str, Iterator[dict]]:
        """导演场景。spec：scene, player_state, beats[], key_lines[], rounds, branch?, option_hint?, situation?"""
        self._check_npc(npc)
        self.reflect_pending(player)
        spec = {**spec, "done": [], "rounds": int(spec.get("rounds", 4))}
        sid = self.s.new_session(player, npc, self.stage(player), "scene", spec)
        return sid, self._turn(sid, "arrive", "（场景开始。请你先开口，或先做一个动作。）")

    def say(self, sid: str, kind: str, text: str) -> Iterator[dict]:
        """kind: say（自由输入）/ option（点选项）/ gesture（非语言动作）"""
        if kind not in ("say", "option", "gesture"):
            raise ValueError("kind 只能是 say / option / gesture")
        text = (text or "").strip()[:300]
        if not text:
            raise ValueError("说点什么吧")
        return self._turn(sid, kind, text)

    def close_session(self, sid: str):
        """结束一次相处，然后整理记忆。如果对方正说到一半，等这一句说完再整理，免得漏掉这一句。"""
        sess = self.s.session(sid)
        if not sess:
            return
        if not sess["ended"]:
            self.s.x("UPDATE sessions SET ended=? WHERE id=? AND ended IS NULL", time.time(), sid)
        with self._busy_lock:
            if sid in self._busy:
                self._reflect_after.add(sid)
                return
        self._reflect(sid)

    def _reflect_quietly(self, sid: str):
        try:
            self._reflect(sid)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    def _turn(self, sid: str, kind: str, text: str) -> Iterator[dict]:
        sess = self.s.session(sid)
        if not sess:
            raise ValueError("会话不存在")
        if sess["ended"]:
            raise ValueError("这次相处已经结束了")
        with self._busy_lock:
            if sid in self._busy:
                raise ValueError("对方还在说上一句")
            self._busy.add(sid)
        try:
            yield from self._turn_inner(sess, kind, text)
        finally:
            with self._busy_lock:
                self._busy.discard(sid)
                later = sid in self._reflect_after
                self._reflect_after.discard(sid)
            if later:
                threading.Thread(target=self._reflect_quietly, args=(sid,), daemon=True).start()

    def _turn_inner(self, sess, kind, text) -> Iterator[dict]:
        sid, player, npc_id = sess["id"], sess["player"], sess["npc"]
        npc = self.c.npcs[npc_id]
        p = self.player(player)
        history = self.s.messages(sid)
        is_scene = sess["kind"] == "scene"
        spec = sess["spec"]

        # ---- 回合预算：这一句说完后还剩几轮。每一句都重新检查（预算可能被别的会话用掉了） ----
        played = sum(1 for m in history if m["role"] == "player" and m["kind"] != "arrive")
        this_round = 0 if kind == "arrive" else 1
        if is_scene:
            used = played + this_round
            if kind != "arrive" and used > spec["rounds"]:
                raise Closed("这一幕已经演完了。")
            left_after = spec["rounds"] - used
        else:
            if kind != "arrive":
                self._check_budget(player, npc_id, spec.get("round_limit"), spec.get("total_limit"),
                                   spec.get("closed_text", ""))
            left_after, used = self._left_after(player, npc_id, spec, this_round)
        limit = (used + left_after) if left_after is not None else None
        budget = prompts.budget_text(left_after, is_scene)

        ctx = self._context(player, npc_id, sess)
        ctx["budget"] = budget
        if is_scene:
            ctx["scene"] = spec
        msgs = [{"role": "system", "content": prompts.turn_system(ctx)}]
        for m in history:
            role = "assistant" if m["role"] == "npc" else "user"
            msgs.append({"role": role, "content": m["text"] if role == "assistant"
                         else prompts.player_line(m["kind"], m["text"], p["name"])})
        msgs.append({"role": "user", "content": prompts.player_line(kind, text, p["name"])})

        # ---- 流式生成：开头的〔表情〕先拆出来；<<< 之前是对白，之后是状态 ----
        t0 = time.time()
        buf, shown = "", 0
        expr, body_at = None, 0          # body_at：去掉表情标记后，对白从第几个字开始
        try:
            for piece in self.g.stream(msgs, max_tokens=1500):
                buf += piece
                lb = buf.lstrip()
                if expr is None:
                    if not lb:
                        continue
                    if lb.startswith(prompts.EXPR_OPEN):
                        close = lb.find(prompts.EXPR_CLOSE)
                        if close < 0 and len(lb) < 12:
                            continue      # 标记还没收完整，再等一下
                        if close >= 0:
                            expr = npc.expr_key(lb[1:close])
                            body_at = close + 1
                        else:
                            expr = npc.expr_key("")
                    else:
                        expr = npc.expr_key("")
                    yield {"t": "expr", "expr": expr}
                body = lb[body_at:].lstrip() if body_at else lb
                cut = body.find(prompts.MARK)
                visible = body[:cut].rstrip() if cut >= 0 else body[:max(0, len(body) - len(prompts.MARK))]
                if len(visible) > shown:
                    yield {"t": "delta", "text": visible[shown:]}
                    shown = len(visible)
        except ModelError as e:
            self.s.log_call(sid, "scene" if is_scene else "turn", msgs, f"ERROR {e}\n{buf}", _ms(t0))
            yield {"t": "error", "message": str(e)}
            return
        self.s.log_call(sid, "scene" if is_scene else "turn", msgs, buf, _ms(t0))

        expr2, body = prompts.split_expr(buf, npc)
        expr = expr or expr2
        cut = body.find(prompts.MARK)
        reply = (body[:cut] if cut >= 0 else body).rstrip()
        if len(reply) > shown:
            yield {"t": "delta", "text": reply[shown:]}
        state = {}
        if cut >= 0:
            try:
                state = extract_json(body[cut + len(prompts.MARK):])
            except ModelError:
                state = {}
        if not reply:
            yield {"t": "error", "message": "模型没有给出对白"}
            return

        # ---- 提交 ----
        applied = self._apply_state(player, npc_id, sid, state)
        inner = str(state.get("inner") or "").strip()[:40] if npc.inner_mode != "never" else ""
        if is_scene:
            beats = state.get("beats") if isinstance(state.get("beats"), list) else []
            new = [b for b in beats if isinstance(b, int) and not isinstance(b, bool) and 1 <= b <= len(spec["beats"])]
            spec["done"] = sorted(set(spec["done"]) | set(new))
        finished = left_after is not None and left_after <= 0
        leave = state.get("leave") is True or finished
        spec["last_options"] = []
        with self.s.tx():
            if kind != "arrive" or not history:
                self.s.add_message(sid, "player", kind, text)
            self.s.add_message(sid, "npc", "say", reply, {"state": state, "applied": applied, "expr": expr,
                                                          "inner": inner})
            self.s.x("UPDATE sessions SET spec=? WHERE id=?", json.dumps(spec, ensure_ascii=False), sid)
            if leave:   # 说完了：这次相处到此为止，之后不再接受新的话
                self.s.x("UPDATE sessions SET ended=? WHERE id=? AND ended IS NULL", time.time(), sid)
        visible, label = self.inner_visibility(player, npc_id)

        options = [] if leave else self._options(player, npc, sid, spec.get("option_hint", "") if is_scene else "")
        if options:   # 记下来：玩家刷新页面后还能看到这一轮的选项
            spec["last_options"] = options
            self.s.x("UPDATE sessions SET spec=? WHERE id=?", json.dumps(spec, ensure_ascii=False), sid)
        gestures = [] if leave else list(self.c.pack.get("gestures") or DEFAULT_GESTURES)
        yield {"t": "done", "reply": reply, "expr": expr, "options": options, "gestures": gestures, "leave": leave,
               "applied": applied, "bond": self.bond_summary(player, npc_id),
               "inner": inner if (visible and inner) else None, "inner_label": None if visible else label,
               "rounds": {"used": used if limit is not None else None, "limit": limit},
               "scene": {"done": spec["done"], "total": len(spec["beats"])} if is_scene else None}

    def inner_visibility(self, player: str, npc_id: str) -> tuple[bool, str | None]:
        """心声给不给玩家看：always 一直给；trust 信任到门槛才给；never 从不给。返回（给不给, 不给时显示的字）。"""
        npc = self.c.npcs[npc_id]
        cfg = npc.inner or {}
        mode = cfg.get("mode", "never")
        if mode == "always":
            return True, None
        if mode == "trust" and self._bond(player, npc_id)["trust"] >= int(cfg.get("min", 10)):
            return True, None
        return False, cfg.get("label") or "看不透"

    def _apply_state(self, player, npc_id, sid, state: dict) -> dict:
        b = self._bond(player, npc_id)
        delta = state.get("bond") if isinstance(state.get("bond"), dict) else {}
        applied = {}
        for k in ("trust", "warmth"):
            try:
                d = max(-3, min(2, int(delta.get(k, 0) or 0)))
            except (TypeError, ValueError):
                d = 0
            if d:
                b[k] = max(0, min(100, b[k] + d))
                applied[k] = d
        if applied and isinstance(state.get("why"), str) and state["why"].strip():
            applied["why"] = state["why"].strip()[:24]
        if isinstance(state.get("mood"), str) and state["mood"].strip():
            self._session_mood[sid] = state["mood"][:20]
        if applied:
            self.s.set_bond_values(player, npc_id, b["trust"], b["warmth"])
            self.s.log_bond(player, npc_id, self.stage(player), b["trust"], b["warmth"])
        return applied

    def bond_summary(self, player: str, npc_id: str) -> dict:
        b = self._bond(player, npc_id)
        return {"trust": b["trust"], "warmth": b["warmth"], "stage": prompts.stage_text(b["trust"], b["warmth"])}

    def bond_history(self, player: str, npc_id: str) -> list[dict]:
        """每个阶段交情的起点、最高点、终点。宿主可以用来判断"某个时刻之前的情分"。"""
        return self.s.q("SELECT * FROM bond_log WHERE player=? AND npc=? ORDER BY stage", player, npc_id)

    def _options(self, player, npc, sid, hint: str = "") -> list[dict]:
        p = self.player(player)
        stage = self.stage(player)
        transcript = [("player" if m["role"] == "player" else "npc",
                       prompts.player_line(m["kind"], m["text"], "我") if m["role"] == "player" else m["text"])
                      for m in self.s.messages(sid) if m["kind"] != "arrive"]
        journal = [r["text"] for r in self.s.q(
            "SELECT text FROM journal WHERE player=? AND stage>=? ORDER BY id DESC LIMIT 5", player, stage - 1)]
        kinds = npc.option_kinds or self.c.option_kinds
        msgs = prompts.options_messages(npc, prompts.fill(self.c.persona.render(stage), p), transcript, journal,
                                        prompts.fill(hint, p), kinds, self.c.world_brief)
        t0 = time.time()
        try:
            out = self.g.json(msgs, max_tokens=800, fast=True)
            self.s.log_call(sid, "options", msgs, json.dumps(out, ensure_ascii=False), _ms(t0))
        except Exception as e:  # noqa: BLE001  选项失败不影响这一轮（对白已经存下了）
            self.s.log_call(sid, "options", msgs, f"ERROR {e}", _ms(t0))
            return []
        opts = []
        for o in _lst(out, "options")[:len(kinds)]:
            if isinstance(o, dict) and o.get("say"):
                opts.append({"kind": str(o.get("kind", ""))[:6], "label": str(o.get("label") or o["say"])[:16],
                             "say": str(o["say"])[:60]})
        return opts

    # ------------------------------------------------------------------ 上下文
    def _episodes(self, player, npc_id) -> list[str]:
        eps = self.s.q("SELECT * FROM episodes WHERE player=? AND npc=? ORDER BY id", player, npc_id)
        if len(eps) > MAX_EPISODES_IN_PROMPT:
            recent = eps[-8:]
            older = sorted(eps[:-8], key=lambda e: -e["salience"])[:MAX_EPISODES_IN_PROMPT - 8]
            eps = sorted(older + recent, key=lambda e: e["id"])
        return [f"第{e['stage']}章：{e['text']}" for e in eps]

    def _heard(self, player, npc_id, limit=MAX_HEARD_IN_PROMPT) -> list[str]:
        return [f"第{r['stage']}章，{r['source'] or '有人'}说（可靠程度：{r['certainty']}）：{r['text']}"
                for r in self.s.q("SELECT * FROM heard WHERE player=? AND npc=? ORDER BY id DESC LIMIT ?",
                                  player, npc_id, limit)][::-1]

    def _context(self, player: str, npc_id: str, sess: dict | None) -> dict:
        npc = self.c.npcs[npc_id]
        p = self.player(player)
        b = self._bond(player, npc_id)
        stage = p["stage"]
        sid = sess["id"] if sess else ""
        spec = (sess or {}).get("spec") or {}
        commitments = [f"[{c['id']}] {'你答应对方' if c['who'] == 'npc' else '对方答应你'}：{c['text']}"
                       for c in self.s.q("SELECT * FROM commitments WHERE player=? AND npc=? AND status='open'",
                                         player, npc_id)]
        told = [r["item"] for r in self.s.q(
            "SELECT item FROM told WHERE player=? AND npc=? ORDER BY id DESC LIMIT ?", player, npc_id,
            MAX_TOLD_IN_PROMPT)][::-1]
        news = [r["text"] for r in self.s.q(
            "SELECT text FROM news WHERE (player=? AND stage>=?) OR player IS NULL ORDER BY id DESC LIMIT 6",
            player, stage - 1)][::-1]
        same_stage = self.s.one("SELECT 1 FROM sessions WHERE player=? AND npc=? AND stage=? AND id!=? AND kind='chat'",
                                player, npc_id, stage, sid)
        meeting = "这段时日里你们已经见过，这是又来了。" if same_stage else "这是这段时日里你们第一次见面。"
        return {
            "npc": npc, "player": p, "stage_info": p["stage_info"], "world_text": self.c.world_text,
            "player_intro": prompts.fill(self.c.player_intro, p),
            "npc_stage": self._npc_stage(p["stage_info"], npc_id),
            "secret": npc.secret.render(stage), "life": self.life(player, npc_id), "news": news,
            "bond": b, "impression": b["impression"], "facts": b["facts"], "episodes": self._episodes(player, npc_id),
            "commitments": commitments, "told": told, "heard": self._heard(player, npc_id), "meeting": meeting,
            "mood": self._session_mood.get(sid), "place": spec.get("place") or npc.place,
            "situation": spec.get("situation", ""), "custom_arrive": spec.get("custom_arrive", False),
        }

    # ------------------------------------------------------------------ 专属台词
    def compose_line(self, player: str, npc_id: str, slot: str, instruction: str, before: str,
                     topics: list[str] | None = None, reveal: bool = False) -> str:
        """按指示，从 NPC 对这个玩家的记忆里写一句台词。同一个玩家同一个空位只生成一次。
        topics：优先找这些话题的原话（比如"一刀雪"）。reveal：把心事里的"真相"一段也给模型（结局揭晓用）。"""
        r = self.s.one("SELECT text FROM lines WHERE player=? AND slot=?", player, slot)
        if r:
            return r["text"]
        npc = self.c.npcs[npc_id]
        p = self.player(player)
        b = self._bond(player, npc_id)
        said = []
        for s in self.s.q("SELECT id FROM sessions WHERE player=? AND npc=? ORDER BY started", player, npc_id):
            said += [m["text"] for m in self.s.messages(s["id"]) if m["role"] == "player" and m["kind"] in ("say", "option")]
        picked = []
        if topics:
            for q in self.s.q("SELECT topic, text FROM quotes WHERE player=? AND npc=? ORDER BY id", player, npc_id):
                if any(t in q["topic"] or q["topic"] in t or t in q["text"] for t in topics):
                    picked.append(q["text"])
            picked += [t for t in said if any(k in t for k in topics)]
        picked += said[-8:]
        seen, snippets = set(), []
        for t in picked:
            if t not in seen:
                seen.add(t)
                snippets.append(f"{p['name']}说：{t}")
        ctx = {"player": p, "impression": b["impression"], "facts": b["facts"], "episodes": self._episodes(player, npc_id),
               "heard": self._heard(player, npc_id, 20), "snippets": snippets[-16:], "before": before,
               "instruction": instruction, "secret": npc.secret.render(p["stage"], reveal=reveal)}
        msgs = prompts.line_messages(npc, ctx)
        t0 = time.time()
        text = ""
        try:
            text = "".join(self.g.stream(msgs, max_tokens=600)).strip()
            if prompts.MARK in text:
                text = text.split(prompts.MARK)[0].strip()
            ex, body = prompts.split_expr(text, npc)
            body = body.strip().strip("“”\"")
            text = f"{prompts.EXPR_OPEN}{npc.expressions.get(ex, '')}{prompts.EXPR_CLOSE}{body}" if body and npc.expressions else body
            self.s.log_call(None, f"line:{slot}", msgs, text, _ms(t0))
        except Exception as e:  # noqa: BLE001
            self.s.log_call(None, f"line:{slot}", msgs, f"ERROR {e}", _ms(t0))
        if not text:
            return ""  # 失败不缓存，宿主可以跳过这一句
        self.s.x("INSERT INTO lines(player,slot,text) VALUES(?,?,?) ON CONFLICT DO NOTHING", player, slot, text[:240])
        r = self.s.one("SELECT text FROM lines WHERE player=? AND slot=?", player, slot)   # 同时生成了两次：以先存下的为准
        return r["text"] if r else text[:240]

    # ------------------------------------------------------------------ 记忆整理
    def reflect_pending(self, player: str | None = None):
        """把还没整理的谈话整理掉（正在说话的会话除外）。失败的留到下次。"""
        sql = "SELECT id FROM sessions WHERE reflected=0" + (" AND player=?" if player else "")
        for r in self.s.q(sql, *([player] if player else [])):
            with self._busy_lock:
                if r["id"] in self._busy:
                    continue
            self._reflect_quietly(r["id"])

    def _active_relays(self, npc, stage: int) -> list[dict]:
        return [r for r in npc.relays if r.get("to") in self.c.npcs and stage >= int(r.get("from_stage", 1))]

    def _reflect(self, sid: str):
        sess = self.s.session(sid)
        if not sess:
            return
        with self._lock(f"reflect:{sess['player']}"):   # 同一个玩家的整理排队；不同玩家并行
            sess = self.s.session(sid)
            if not sess or sess["reflected"]:
                return
            all_msgs = self.s.messages(sid)
            player, npc_id = sess["player"], sess["npc"]
            if not any(m["role"] == "player" and m["kind"] != "arrive" for m in all_msgs):
                self.s.x("UPDATE sessions SET reflected=1 WHERE id=?", sid)
                return
            npc = self.c.npcs[npc_id]
            p = self.player(player)
            b = self._bond(player, npc_id)
            lines = []
            if sess["kind"] == "scene":
                lines.append(f"（这是一场戏：{sess['spec'].get('scene', '')}）")
            for m in all_msgs:
                if m["role"] == "npc":
                    lines.append(f"你：{m['text']}")
                elif m["kind"] != "arrive":
                    lines.append(f"{p['name']}：{prompts.player_line(m['kind'], m['text'], p['name'])}")
            open_c = self.s.q("SELECT * FROM commitments WHERE player=? AND npc=? AND status='open'", player, npc_id)
            relays = self._active_relays(npc, sess["stage"])
            ctx = {"player": p, "label": p["stage_info"].get("label", ""), "impression": b["impression"],
                   "facts": b["facts"], "commitments": [f"{c['id']}：{c['text']}" for c in open_c],
                   "secret": npc.secret.render(sess["stage"]), "transcript": "\n".join(lines),
                   "relays": [(self.c.npcs[r["to"]].name, prompts.fill(r.get("what", ""), p)) for r in relays]}
            pm = prompts.reflect_messages(npc, ctx)
            t0 = time.time()
            try:
                out = self.g.json(pm, max_tokens=1800)
                self.s.log_call(sid, "reflect", pm, json.dumps(out, ensure_ascii=False), _ms(t0))
            except Exception as e:  # noqa: BLE001
                self.s.log_call(sid, "reflect", pm, f"ERROR {e}", _ms(t0))
                return  # 留到下次再整理
            # 玩家说过的每一句（分开存，原话只能出自其中某一句，不能跨句拼出来）
            said = [_norm(m["text"]) for m in all_msgs if m["role"] == "player" and m["kind"] in ("say", "option")]
            self._apply_reflection(sess, out, {c["id"] for c in open_c}, said, relays)

    def _apply_reflection(self, sess, out: dict, open_ids: set[int], said: list[str], relays: list[dict]):
        player, npc_id, stage = sess["player"], sess["npc"], sess["stage"]
        npc = self.c.npcs[npc_id]
        with self.s.tx():
            b = self._bond(player, npc_id)      # 在事务里重新读：信任、亲近以库里的为准，这里只改看法和事实
            if isinstance(out.get("diary"), str) and out["diary"].strip():
                sal = out.get("salience", 2)
                sal = sal if isinstance(sal, int) and not isinstance(sal, bool) else 2
                self.s.x("INSERT INTO episodes(player,npc,stage,text,salience) VALUES(?,?,?,?,?)",
                         player, npc_id, stage, out["diary"][:600], max(1, min(5, sal)))
            if isinstance(out.get("impression"), str) and out["impression"].strip():
                b["impression"] = out["impression"][:400]
            for f in _lst(out, "facts"):
                if isinstance(f, str) and f and f not in b["facts"]:
                    b["facts"].append(f[:120])
            b["facts"] = b["facts"][-MAX_FACTS:]
            for c in _lst(out, "commitments_new"):
                if isinstance(c, dict) and c.get("text"):
                    self.s.x("INSERT INTO commitments(player,npc,stage,who,text) VALUES(?,?,?,?,?)",
                             player, npc_id, stage, "npc" if c.get("by") == "npc" else "player", str(c["text"])[:200])
            for cid in _lst(out, "commitments_done"):
                if isinstance(cid, int) and cid in open_ids:
                    self.s.x("UPDATE commitments SET status='done', done_stage=? WHERE id=?", stage, cid)
            for t in _lst(out, "told"):
                if isinstance(t, str) and t:
                    self.s.x("INSERT INTO told(player,npc,stage,item) VALUES(?,?,?,?)", player, npc_id, stage, t[:80])
            # 原话：只收真的出现在这次对话里的句子，防止模型"记错"
            for q in _lst(out, "quotes")[:3]:
                if isinstance(q, dict) and isinstance(q.get("text"), str):
                    text = q["text"].strip().strip("“”\"")
                    n = _norm(text)
                    if len(n) >= 4 and any(n in u for u in said):
                        self.s.x("INSERT INTO quotes(player,npc,stage,topic,text) VALUES(?,?,?,?,?)",
                                 player, npc_id, stage, str(q.get("topic") or "")[:12], text[:120])
            # 传话：把这次听到的事转告给别的 NPC，写进对方的"听说"
            for item in _lst(out, "relay"):
                if not (isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()):
                    continue
                to = str(item.get("to") or "")
                for r in relays:
                    target = self.c.npcs[r["to"]]
                    if to in (target.id, target.name, target.title) or target.name in to:
                        self.s.x("INSERT INTO heard(player,npc,stage,text,source,certainty) VALUES(?,?,?,?,?,?)",
                                 player, target.id, stage, item["text"].strip()[:200], f"{npc.name}（转述）",
                                 r.get("certainty", "中"))
                        break
            self.s.set_bond_memory(player, npc_id, b["impression"], b["facts"], b["meetings"] + 1)
            self.s.x("UPDATE sessions SET reflected=1 WHERE id=?", sess["id"])

    # ------------------------------------------------------------------ 查看
    def view(self, player: str, npc_id: str) -> dict:
        npc = self.c.npcs[npc_id]
        b = self._bond(player, npc_id)
        stage = self.stage(player)
        last = self.s.one("""SELECT m.meta FROM messages m JOIN sessions s ON m.session = s.id
                             WHERE s.player=? AND s.npc=? AND m.role='npc' ORDER BY m.id DESC LIMIT 1""", player, npc_id)
        return {
            "npc": {"id": npc.id, "name": npc.name, "inner_mode": npc.inner_mode},
            "stage": stage, "life": self.life(player, npc_id),
            "bond": {"trust": b["trust"], "warmth": b["warmth"], "meetings": b["meetings"],
                     "stage": prompts.stage_text(b["trust"], b["warmth"])},
            "bond_log": self.bond_history(player, npc_id),
            "secret_stage": stage,
            "last_inner": json.loads(last["meta"]).get("inner") if last else "",
            "impression": b["impression"], "facts": b["facts"],
            "episodes": self.s.q("SELECT stage,text,salience FROM episodes WHERE player=? AND npc=? ORDER BY id DESC",
                                 player, npc_id),
            "commitments": self.s.q("SELECT id,stage,who,text,status FROM commitments WHERE player=? AND npc=? ORDER BY id DESC",
                                    player, npc_id),
            "told": [r["item"] for r in self.s.q("SELECT item FROM told WHERE player=? AND npc=? ORDER BY id DESC",
                                                 player, npc_id)],
            "quotes": self.s.q("SELECT stage,topic,text FROM quotes WHERE player=? AND npc=? ORDER BY id DESC", player, npc_id),
            "heard": self.s.q("SELECT stage,source,certainty,text FROM heard WHERE player=? AND npc=? ORDER BY id DESC",
                              player, npc_id),
            "relayed": self.s.q("SELECT npc,stage,text FROM heard WHERE player=? AND source=? ORDER BY id DESC",
                                player, f"{npc.name}（转述）"),
            "news": [r["text"] for r in self.s.q(
                "SELECT text FROM news WHERE player=? OR player IS NULL ORDER BY id DESC LIMIT 8", player)],
            "lines": self.s.q("SELECT slot,text FROM lines WHERE player=?", player),
        }

    def last_expr(self, player: str, npc_id: str) -> str:
        """NPC 最近一次说话时的表情（用来显示人物卡片上的头像）。"""
        r = self.s.one("""SELECT m.meta FROM messages m JOIN sessions s ON m.session = s.id
                          WHERE s.player=? AND s.npc=? AND m.role='npc' ORDER BY m.id DESC LIMIT 1""", player, npc_id)
        npc = self.c.npcs[npc_id]
        return (json.loads(r["meta"]).get("expr") if r else None) or npc.expr_key("")

    def split_line(self, npc_id: str, text: str) -> tuple[str, str]:
        return prompts.split_expr(text or "", self.c.npcs[npc_id])

    def last_prompt(self, sid: str) -> dict | None:
        r = self.s.one("SELECT purpose,prompt,output,ms FROM calls WHERE session=? AND purpose IN ('turn','scene') "
                       "ORDER BY id DESC LIMIT 1", sid)
        if r:
            r["prompt"] = json.loads(r["prompt"])
        return r

    def _check_npc(self, npc_id):
        if npc_id not in self.c.npcs:
            raise ValueError(f"没有这个 NPC：{npc_id}")


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
