"""内容包加载。运行时不认识任何具体游戏：世界、人物、心事、玩家人设全部来自内容目录。

content/
  pack.json               总配置：标题、玩家建档、可对话的人、预算、文案……（运行时只读其中几项，其余给宿主）
  world.md                世界常识（每次都给所有 NPC）
  player.md               玩家角色的人设，按章节分层（只给"回复选项"生成器，NPC 看不到）
  npcs/<id>/npc.json      名字、地点、表情、心声、初始交情、传话关系……
  npcs/<id>/bible.md      人物本（每次都给模型）
  npcs/<id>/secret.md     分层的心事：## 影子 / ## 第N章起 / ## 真相（只在显式"揭晓"时给）
宿主自己的数据（story/、events.json、clues.json、endings.json）由宿主读取，运行时不读。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_STAGE = re.compile(r"^第\s*(\d+)\s*(?:章起|章|层)")


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return re.sub(r"\n{3,}", "\n\n", _COMMENT.sub("", path.read_text(encoding="utf-8"))).strip()


def _json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


@dataclass
class Layered:
    """分层的文字：影子一直给；第 N 章起的段落到第 N 章才给；真相只在 reveal=True 时给。"""
    shadow: str = ""
    stages: dict[int, str] = field(default_factory=dict)
    truth: str = ""

    @classmethod
    def parse(cls, text: str) -> "Layered":
        s = cls()
        for block in re.split(r"(?m)^## ", text)[1:]:
            title, _, body = block.partition("\n")
            title, body = title.strip(), body.strip()
            if title == "影子":
                s.shadow = body
            elif title == "真相":
                s.truth = body
            elif m := _STAGE.match(title):
                s.stages[int(m.group(1))] = body
        return s

    def render(self, stage: int, reveal: bool = False) -> str:
        """只拼出当前阶段已经解锁的部分。没给的，模型根本不知道。"""
        parts = [self.shadow]
        for n in sorted(self.stages):
            if n <= stage:
                parts.append(f"### 第{n}章以来\n{self.stages[n]}")
        if reveal and self.truth:
            parts.append(f"### 真相\n{self.truth}")
        return "\n\n".join(p for p in parts if p)


Secret = Layered  # 旧名字，保持兼容


@dataclass
class NPC:
    id: str
    name: str
    title: str
    place: str
    place_desc: str
    closed_line: str
    bible: str
    secret: Layered
    expressions: dict[str, str] = field(default_factory=dict)   # 键 -> 中文名，例如 smile -> 微笑
    hidden_expressions: list[str] = field(default_factory=list)  # 只在写死的台词里用，不让模型选
    inner: dict = field(default_factory=lambda: {"mode": "never"})  # 心声：always / trust(min) / never
    bond_start: dict = field(default_factory=dict)              # 初始交情 {"trust": 0, "warmth": 0}
    quote_topics: list[str] = field(default_factory=list)       # 整理原话时建议用的话题
    relays: list[dict] = field(default_factory=list)            # 会把听到的事转告给谁
    option_kinds: list[dict] | None = None                      # 回复选项的分工（不填用 pack 的默认）
    from_chapter: int = 1
    card: str = ""
    var_notes: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def model_expressions(self) -> dict[str, str]:
        return {k: v for k, v in self.expressions.items() if k not in self.hidden_expressions}

    def expr_key(self, label: str) -> str:
        """把模型写的中文表情名换成键；认不出就用第一个（通常是"平静"）。"""
        label = (label or "").strip()
        for k, v in self.expressions.items():
            if label in (k, v):
                return k
        return next(iter(self.expressions), "")

    @property
    def inner_mode(self) -> str:
        return (self.inner or {}).get("mode", "never")


@dataclass
class Content:
    world_text: str
    persona: Layered          # 玩家角色的人设（分层），可含 {名} {店名} {ta}
    npcs: dict[str, NPC]
    pack: dict = field(default_factory=dict)

    @property
    def player_intro(self) -> str:
        return (self.pack.get("player") or {}).get("intro") or "{名}。"

    def gender_words(self, gender: str) -> dict[str, str]:
        for g in (self.pack.get("player") or {}).get("genders") or []:
            if g.get("id") == gender:
                return dict(g.get("words") or {})
        gs = (self.pack.get("player") or {}).get("genders") or []
        return dict(gs[0].get("words") or {}) if gs else {"{ta}": "ta"}

    @property
    def option_kinds(self) -> list[dict]:
        return ((self.pack.get("options") or {}).get("kinds")
                or [{"kind": "追问", "desc": "接住对方刚说的某个具体的东西，往深里问。"},
                    {"kind": "表态", "desc": "对对方刚说的话表达明确的看法。"},
                    {"kind": "带入自己", "desc": "说起“我”自己的事。"}])

    @property
    def world_brief(self) -> str:
        return (self.pack.get("options") or {}).get("world_brief", "")


def load(root: str | Path) -> Content:
    root = Path(root)
    npcs: dict[str, NPC] = {}
    for d in sorted((root / "npcs").iterdir()):
        if not (d / "npc.json").exists():
            continue
        cfg = _json(d / "npc.json", {})
        npcs[cfg["id"]] = NPC(
            id=cfg["id"], name=cfg["name"], title=cfg.get("title", cfg["name"]),
            place=cfg.get("place", ""), place_desc=cfg.get("place_desc", ""),
            closed_line=cfg.get("closed_line", "今天就到这里吧。"),
            bible=_read(d / "bible.md"), secret=Layered.parse(_read(d / "secret.md")),
            expressions=cfg.get("expressions", {}), hidden_expressions=cfg.get("hidden_expressions", []),
            inner=cfg.get("inner") or {"mode": "never"}, bond_start=cfg.get("bond_start") or {},
            quote_topics=cfg.get("quote_topics") or [], relays=cfg.get("relays") or [],
            option_kinds=cfg.get("option_kinds"), from_chapter=int(cfg.get("from_chapter", 1)),
            card=cfg.get("card", ""), var_notes=cfg.get("var_notes") or [], raw=cfg)
    if not npcs:
        raise ValueError("内容目录里没有任何 NPC")
    pack = _json(root / "pack.json", {}) or {}
    world = _read(root / "world.md") or _read(root / "world" / "world.md")
    persona = Layered.parse(_read(root / "player.md"))
    if not (persona.shadow or persona.stages):          # 没有分层标题：整份都是"影子"
        persona = Layered(shadow=_read(root / "player.md"))
    if pack.get("npcs"):                                 # 按 pack 里的顺序排
        order = {k: i for i, k in enumerate(pack["npcs"])}
        npcs = dict(sorted(npcs.items(), key=lambda kv: order.get(kv[0], 99)))
    return Content(world_text=world, persona=persona, npcs=npcs, pack=pack)
