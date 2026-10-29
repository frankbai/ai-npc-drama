"""内容包检查：写完剧情先跑一遍。

    python3 tools/check_content.py                 # 检查 content/
    python3 tools/check_content.py path/to/pack    # 检查别的内容包

两类检查：
1. 结构：人物、章节、CG、事件、线索、结局之间的引用对不对；条件写得对不对；指认有没有可能凑够分。
2. 防剧透：按章节、按 NPC 把自由对话时的提示词拼出来（不调用模型），检查 checks.json 里列的字
   有没有提前混进去。例如"凶手在自由对话里永远不知道自己是凶手"。
有问题就列出来，退出码 1；没有问题退出码 0。tests/test_content.py 也会调用这里。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from npc import content as content_mod  # noqa: E402
from npc import prompts  # noqa: E402

COND_KEYS = {"all", "any", "not", "clue", "done", "trust", "warmth", "var", "chapter", "accused_right", "score", "bond"}
PLACEHOLDER = re.compile(r"\{([^{}\s\"]{1,4})\}")


def _json(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _walk_cond(c, where: str, errs: list[str], clue_ids: set, npc_ids: set, var_ids: set, event_ids: set):
    if c is None or c is True or c is False or c == "else":
        return
    if not isinstance(c, dict) or len(c) != 1 or next(iter(c)) not in COND_KEYS:
        errs.append(f"{where}：看不懂的条件 {c}")
        return
    k, v = next(iter(c.items()))
    ops = (">=", ">", "<=", "<", "==", "!=")
    if k in ("trust", "warmth", "var") and isinstance(v, list) and len(v) == 3 and v[1] not in ops:
        errs.append(f"{where}：条件 {c} 的比较符只能是 {' '.join(ops)}")
    if k in ("chapter", "score", "bond") and not (isinstance(v, list) and len(v) == 2 and v[0] in ops):
        errs.append(f"{where}：条件 {c} 要写成 [比较符, 数字]，比较符只能是 {' '.join(ops)}")
    if k in ("all", "any"):
        for x in v:
            _walk_cond(x, where, errs, clue_ids, npc_ids, var_ids, event_ids)
    elif k == "not":
        _walk_cond(v, where, errs, clue_ids, npc_ids, var_ids, event_ids)
    elif k == "clue" and v not in clue_ids:
        errs.append(f"{where}：条件里的线索 {v} 不存在")
    elif k == "done" and v not in event_ids:
        errs.append(f"{where}：条件里的事件 {v} 不存在")
    elif k in ("trust", "warmth") and (len(v) != 3 or v[0] not in npc_ids):
        errs.append(f"{where}：条件 {c} 要写成 [NPC id, 比较符, 数字]")
    elif k == "var" and (len(v) != 3 or v[0] not in var_ids):
        errs.append(f"{where}：条件里的变量 {v} 没在 pack.json 的 vars 里声明")


def validate(root: Path) -> list[str]:
    """结构检查。返回问题列表。"""
    errs: list[str] = []
    try:
        c = content_mod.load(root)
    except Exception as e:  # noqa: BLE001
        return [f"内容包加载失败：{e}"]
    pack = c.pack
    npc_ids = set(c.npcs)
    for k in pack.get("npcs", []):
        if k not in npc_ids:
            errs.append(f"pack.json 的 npcs 里有 {k}，但 npcs/ 下没有这个人")
    words = set()
    for g in (pack.get("player") or {}).get("genders") or []:
        words |= set(g.get("words", {}))
    allowed = {"名", "店名", "证据", "凶手", "n", "name", "place", "cn", "season", "1", "2", "3"} | {w.strip("{}") for w in words}
    var_ids = set((pack.get("vars") or {}).keys())
    clues = {x["id"]: x for x in (_json(root / "clues.json", {}) or {}).get("clues", [])}
    events = {x["id"]: x for x in (_json(root / "events.json", {}) or {}).get("events", [])}
    groups = {g["id"] for g in pack.get("groups", [])} or {"经营", "调查"}
    endings = _json(root / "endings.json", {}) or {}
    suspects = {s["id"] for s in (endings.get("accuse") or {}).get("suspects", [])}
    chapters = {}
    for f in sorted((root / "story").glob("*.json")):
        try:
            ch = _json(f)
            chapters[int(ch["n"])] = ch
        except Exception as e:  # noqa: BLE001
            errs.append(f"{f.name}：读不了（{e}）")
    if not chapters:
        return errs + ["没有章节（story/*.json）"]
    if sorted(chapters) != list(range(1, max(chapters) + 1)):
        errs.append(f"章节编号要从 1 连续编号，现在是 {sorted(chapters)}")
    last = max(chapters)

    def cond(x, where):
        _walk_cond(x, where, errs, set(clues), npc_ids, var_ids, set(events))

    def text_ok(t: str, where: str):
        for m in PLACEHOLDER.findall(t or ""):
            if m not in allowed:
                errs.append(f"{where}：不认识的占位符 {{{m}}}")

    def check_lines(lines, where, n, slots):
        for i, ln in enumerate(lines):
            w = f"{where} 第{i + 1}句"
            who = ln.get("who")
            if who not in npc_ids and who not in ("narr", "head", "me", "memory", "card", "tip", "other"):
                errs.append(f"{w}：who={who} 不是 NPC id，也不是 narr/head/me/memory/card/tip/other")
            if who in npc_ids:
                npc = c.npcs[who]
                if ln.get("expr") and ln["expr"] not in npc.expressions:
                    errs.append(f"{w}：{npc.name} 没有表情 {ln['expr']}")
            if "slot" in ln:
                if who not in npc_ids:
                    errs.append(f"{w}：专属台词必须由 NPC 说")
                if not ln.get("instruction"):
                    errs.append(f"{w}：专属台词缺少 instruction")
                if ln["slot"] in slots:
                    errs.append(f"{w}：专属台词的空位 {ln['slot']} 重名")
                slots.add(ln["slot"])
            elif not ln.get("text") and who != "head":
                errs.append(f"{w}：没有 text")
            cond(ln.get("if"), w)
            text_ok(ln.get("text", ""), w)
            text_ok(ln.get("instruction", ""), w)

    slots: set = set()
    for n, ch in sorted(chapters.items()):
        where = f"第{n}章"
        avail = {k for k, v in c.npcs.items() if v.from_chapter <= n}
        for k, cfg in (ch.get("npcs") or {}).items():
            if k not in npc_ids:
                errs.append(f"{where}：npcs 里的 {k} 不存在")
                continue
            if k not in avail:
                errs.append(f"{where}：{k} 要到第 {c.npcs[k].from_chapter} 章才出场，这里不该有他的配置")
            for nt in cfg.get("notes", []):
                cond(nt.get("if"), f"{where} {k} 的 notes")
            for key in ("stage", "situation", "arrive"):
                text_ok(cfg.get(key, ""), f"{where} {k}.{key}")
        for k in avail:
            if k not in (ch.get("npcs") or {}):
                errs.append(f"{where}：{c.npcs[k].name} 这一章能对话，但没写 stage（这一章你们的关系）")
        for i, ph in enumerate((ch.get("cg") or {}).get("phases", [])):
            w = f"{where} CG 第{i + 1}段"
            if ph.get("type", "script") == "script":
                check_lines(ph.get("lines", []), w, n, slots)
            elif ph.get("type") == "director":
                if ph.get("npc") not in avail:
                    errs.append(f"{w}：导演场景的 npc={ph.get('npc')} 这一章不能出场")
                vs = ph.get("variants") or []
                for v in vs:
                    cond(v.get("if"), w)
                    for g in v.get("gives", []):
                        if g not in clues:
                            errs.append(f"{w}：版本给的线索 {g} 不存在")
                if vs and not any(v.get("else") for v in vs):
                    errs.append(f"{w}：variants 最后最好有一个 else，否则条件都不成立时用的是基础版本")
                if not (ph.get("beats") or all(v.get("beats") for v in vs)):
                    errs.append(f"{w}：导演场景没有 beats（必经事件）")
            else:
                errs.append(f"{w}：type 只能是 script 或 director")
            for g in ph.get("gives", []):
                if g not in clues:
                    errs.append(f"{w}：给的线索 {g} 不存在")
        for g in (ch.get("cg") or {}).get("gives", []) + (ch.get("outro") or {}).get("gives", []):
            if g not in clues:
                errs.append(f"{where}：给的线索 {g} 不存在")
        for i, ph in enumerate((ch.get("outro") or {}).get("phases", [])):
            if ph.get("type", "script") != "script":
                errs.append(f"{where} 章末第{i + 1}段：章末只能是 script")
            check_lines(ph.get("lines", []), f"{where} 章末第{i + 1}段", n, slots)
        for k in list((ch.get("memory") or {})) + list(((ch.get("outro") or {}).get("memory") or {})):
            if k not in npc_ids:
                errs.append(f"{where}：memory 里的 {k} 不存在")
    for e in events.values():
        w = f"事件 {e['id']}"
        if e.get("group") not in groups:
            errs.append(f"{w}：group={e.get('group')} 不在 pack.json 的 groups 里")
        a, b = e.get("chapters", [1, last])
        if not (1 <= a <= b <= last):
            errs.append(f"{w}：chapters={e.get('chapters')} 不在 1..{last} 之内")
        cond(e.get("if"), w)
        for eff in [e] + (e.get("choices") or []):
            for g in eff.get("gives", []):
                if g not in clues:
                    errs.append(f"{w}：给的线索 {g} 不存在")
            for k in (eff.get("knows") or {}):
                if k != "*" and k not in npc_ids:
                    errs.append(f"{w}：knows 里的 {k} 不存在")
            for k in (eff.get("set") or {}):
                if k not in var_ids:
                    errs.append(f"{w}：变量 {k} 没在 pack.json 的 vars 里声明")
            text_ok(eff.get("note", ""), w)
    for cl in clues.values():
        for a in cl.get("against", []):
            if a != "*" and a not in npc_ids and a not in suspects:
                errs.append(f"线索 {cl['id']}：against 里的 {a} 既不是 NPC 也不是嫌疑人")
    for npc in c.npcs.values():
        for r in npc.relays:
            if r.get("to") not in npc_ids:
                errs.append(f"{npc.name}：relays 里的 {r.get('to')} 不存在")
        for vn in npc.var_notes:
            if vn.get("var") not in var_ids:
                errs.append(f"{npc.name}：var_notes 里的变量 {vn.get('var')} 没声明")
        mode = (npc.inner or {}).get("mode", "never")
        if mode not in ("always", "trust", "never"):
            errs.append(f"{npc.name}：inner.mode 只能是 always / trust / never")
    acc = endings.get("accuse")
    if acc:
        if acc.get("culprit") not in suspects:
            errs.append("endings.json：culprit 不在 suspects 里")
        parts = endings.get("parts") or {}
        es = endings.get("endings") or []
        if not es or es[-1].get("if") != "else":
            errs.append("endings.json：最后一个结局的 if 要写 \"else\"，保证总有结局")
        for e in es:
            cond(e.get("if") if e.get("if") != "else" else None, f"结局 {e.get('id')}")
            for i, ph in enumerate(e.get("phases", [])):
                if ph.get("include"):
                    if ph["include"] not in parts:
                        errs.append(f"结局 {e.get('id')}：include 的 {ph['include']} 不在 parts 里")
                else:
                    check_lines(ph.get("lines", []), f"结局 {e.get('id')} 第{i + 1}段", last, slots)
        for name, lines in parts.items():          # 片段可以被好几个结局引用，空位只算一次
            check_lines(lines, f"结局片段 {name}", last, slots)
        best, _ = _best_scores(chapters, events, clues, acc)
        if best < int(acc.get("pass", 0)):
            errs.append(f"指认：能拿到的证据最多只有 {best} 分，凑不够 pass={acc.get('pass')}")
    return errs


def _best_scores(chapters: dict, events: dict, clues: dict, acc: dict) -> tuple[int, int]:
    """指认能拿到的最高分：(整局拿得到的线索, 只算剧情一定会给的线索)。

    一定会给的：CG、章末、没有分版本的导演场景给的线索。分版本的导演场景、事件都要看玩家怎么玩。"""
    reachable, sure = set(), set()
    for ch in chapters.values():
        cg = ch.get("cg") or {}
        fixed = set(cg.get("gives", [])) | set((ch.get("outro") or {}).get("gives", []))
        reachable |= fixed
        sure |= fixed
        for ph in cg.get("phases", []):
            reachable |= set(ph.get("gives", []))
            if not ph.get("variants"):
                sure |= set(ph.get("gives", []))
            for v in ph.get("variants") or []:
                reachable |= set(v.get("gives", []))
    for e in events.values():
        for eff in [e] + (e.get("choices") or []):
            reachable |= set(eff.get("gives", []))

    def best(ids):
        ws = sorted((int(clues[x].get("weight", 0)) for x in ids if x in clues and (
            "*" in clues[x].get("against", []) or acc.get("culprit") in clues[x].get("against", []))), reverse=True)
        return sum(ws[:int(acc.get("pick", 3))])
    return best(reachable), best(sure)


def hints(root: Path) -> list[str]:
    """不算错、但值得看一眼的地方。"""
    out = []
    try:
        endings = _json(root / "endings.json", {}) or {}
        acc = endings.get("accuse")
        if not acc:
            return out
        clues = {x["id"]: x for x in (_json(root / "clues.json", {}) or {}).get("clues", [])}
        events = {x["id"]: x for x in (_json(root / "events.json", {}) or {}).get("events", [])}
        chapters = {i: _json(f) for i, f in enumerate(sorted((root / "story").glob("*.json")))}
        best, sure = _best_scores(chapters, events, clues, acc)
        need = int(acc.get("pass", 0))
        out.append(f"指认：剧情一定会给的线索最多 {sure} 分，加上要自己找的最多 {best} 分，及格线 {need} 分。")
        if sure >= need:
            out.append("指认：不调查也能凑够分。想让调查有意义的话，把 pass 调高或者把关键线索挪进事件里。")
    except Exception:  # noqa: BLE001
        pass
    return out


def _prompt_texts(root: Path) -> dict:
    """按章节、按 NPC 拼出自由对话时会给模型的提示词（不调用模型）。返回 {(who, 章): 文本}。"""
    os.environ.pop("ACCESS_CODE", None)
    os.environ.pop("DATABASE_URL", None)
    from npc.gateway import MockGateway
    from npc.runtime import Runtime
    from npc.store import Store
    from host.story import Director
    c = content_mod.load(root)
    tmp = tempfile.mkdtemp()
    rt = Runtime(c, Store(Path(tmp) / "check.sqlite3"), MockGateway({"mock_delay": 0}))
    d = Director(root, rt)
    out = {}
    genders = [g["id"] for g in (c.pack.get("player") or {}).get("genders") or []] or [""]
    for gender in genders:
        pid = rt.register_player("某人", "某店", gender)
        for n in sorted(d.chapters):
            d.enter_chapter(pid, n)
            p = rt.player(pid)
            out[("player", n, gender)] = prompts.fill(c.persona.render(n), p)
            for npc_id in d.available_npcs(pid, n):
                sid = rt.s.new_session(pid, npc_id, n, "chat", {"situation": d.situation(pid, npc_id)})
                ctx = rt._context(pid, npc_id, rt.s.session(sid))
                ctx["budget"] = ""
                text = prompts.turn_system(ctx)
                # 带条件的补充说明、随变量出现的说明：不管条件成不成立都查（总有玩家会碰到）
                cfg = (d.chapters[n].get("npcs") or {}).get(npc_id) or {}
                text += "\n" + "\n".join(nt.get("text", "") for nt in cfg.get("notes", []))
                text += "\n" + "\n".join(vn.get("text", "") for vn in c.npcs[npc_id].var_notes
                                         if n <= int(vn.get("until", 99)))
                # 导演场景：这一章里这个 NPC 的戏
                for i, ph in enumerate(d.chapters[n]["cg"].get("phases", [])):
                    if ph.get("type") == "director" and ph.get("npc") == npc_id:
                        for v in [ph] + (ph.get("variants") or []):
                            text += "\n" + json.dumps({k: v.get(k) for k in ("scene", "player_state", "beats", "key_lines",
                                                                              "branch", "option_hint")}, ensure_ascii=False)
                out[(npc_id, n, gender)] = text
    time.sleep(0.2)          # 等后台的生活推演写完再关库
    rt.s.close()
    return out


def spoilers(root: Path) -> list[str]:
    rules = (_json(root / "checks.json", {}) or {}).get("rules", [])
    if not rules:
        return []
    texts = _prompt_texts(root)
    errs = []
    for r in rules:
        for (who, n, gender), text in texts.items():
            if r["who"] not in (who, "*") or (r["who"] == "*" and who == "player"):
                continue
            words = r.get("never", []) if "never" in r else (r.get("text", []) if n < int(r.get("before", 0)) else [])
            for w in words:
                if w in text:
                    errs.append(f"剧透：第{n}章 {who} 的提示词里提前出现了「{w}」")
    return sorted(set(errs))


def main(argv=None):
    root = Path((argv or sys.argv[1:] or [str(ROOT / "content")])[0]).resolve()
    errs = validate(root)
    if not errs:
        errs = spoilers(root)
    for e in errs:
        print("✗", e)
    if errs:
        print(f"\n发现 {len(errs)} 个问题。")
        return 1
    for h in hints(root):
        print("·", h)
    print(f"✓ {root} 检查通过（结构 + 防剧透）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
