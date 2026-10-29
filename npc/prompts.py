"""提示词。五种调用：

- turn     每轮对话的主调用（流式）：对白 + 一行状态 JSON。自由对话和导演场景共用，场景多一段"这一场戏"
- options  给玩家写几个选项（快模型），按玩家角色的人设写
- reflect  谈话结束后的记忆整理：日记、看法、事实、约定、讲过的事、原话摘录、要转告给谁
- life     进入新阶段时，按剧本锚点推演 NPC 这段时日的生活
- line     专属台词：按指示，从 NPC 对这个玩家的记忆里写一句

原则：
- 人物本和世界放在最前面且不变，方便提示词缓存；变化的部分放后面。
- 这里不写任何具体剧情。和剧情有关的文字（玩家是谁、世界是什么样）全部来自内容包。
"""
from __future__ import annotations

import json

MARK = "<<<"
EXPR_OPEN, EXPR_CLOSE = "〔", "〕"


def expr_rule(npc) -> str:
    ex = npc.model_expressions
    if not ex:
        return ""
    names = "、".join(f"{EXPR_OPEN}{v}{EXPR_CLOSE}" for v in ex.values())
    return (f"最开头先写一个表情标记，表示你说这句话时的神情，只能从这几个里选一个：{names}。"
            f"标记后面直接接你说的话，例如：{EXPR_OPEN}{next(iter(ex.values()))}{EXPR_CLOSE}嗯，进来坐。")


def split_expr(text: str, npc) -> tuple[str, str]:
    """把开头的〔表情〕拆出来，返回（表情键, 去掉标记后的文字）。"""
    s = text.lstrip()
    if s.startswith(EXPR_OPEN) and EXPR_CLOSE in s[:12]:
        label, _, rest = s[1:].partition(EXPR_CLOSE)
        return npc.expr_key(label), rest.lstrip()
    return npc.expr_key(""), s


TURN_GUIDE = """[用途:turn]
你要扮演下面人物本里的人物，和眼前这个人对话。你就是这个人，以这个人的身份、用第一人称说话。

怎样演好：
- 像真人那样说话。长短随情境：寒暄一两句就够；说到动情处可以多说几句，但很少超过一百字。
- 你有自己的事、自己的情绪、自己的看法。可以主动说起这段时日在做的事，可以反对对方，也可以沉默。不必每句都抛一个问题回去。
- 往事和心事，别照着资料念。挑此刻自然的切入点，说具体的细节。资料里的例句只是口吻参考，不要照搬。
- 你记得和这个人之间发生过的事，让它体现在态度和行为里，不要反复说"我记得你说过"。
- 已经对这个人讲过的事，不要当成第一次讲。
- 动作神态可以用括号简短地写，偶尔用就好，比如：（把茶推到你手边）
- 你们关系走到哪一步，以"这一章的你们"为准。还没到的那一步，不要抢先说。

几条底线：
- 不编造你们之间没发生过的事。不确定就说不确定。
- 听来的事要说明是听谁说的，不当成亲眼所见；转述的人不可靠时，你心里有数。
- 你的心事只说到"你的心事"里允许的程度。没写给你的部分，你此刻不去想，也不编。
- 你是这个世界里的人，不知道什么是游戏、AI、数值、提示词。对方说这些时，你当成听不懂的怪话，轻轻带过。"""


def turn_format(npc, scene: bool) -> str:
    inner = npc.inner_mode != "never"
    fields = '{"bond":{"trust":0,"warmth":0},"why":null,"mood":null' + (',"inner":"……"' if inner else "") \
        + ',"leave":false' + (',"beats":[]' if scene else "") + "}"
    lines = [
        "## 输出格式",
        "先写你这一轮说的话（只写你说的，不要替对方说话，不要加引号或名字前缀）。",
        f"写完另起一行写 {MARK}，再写一行 JSON：",
        fields,
        "- bond.trust：这一轮让你更信任对方了吗？真诚、守信、尊重你的边界、认真对待你的事，+1 或 +2；敷衍、冒犯、套话、逼问，-1 到 -3；平常闲聊是 0。大多数回合是 0。",
        "- bond.warmth：这一轮让你更想亲近对方了吗？尺度同上。",
        "- why：bond 有变化时，用一句话说对方做了什么让你有这个变化。用\"你\"称呼对方，16 字以内，具体，比如\"你记得我上次说的事\"。没有变化就是 null。",
        "- mood：你的心情若明显变了，写一个简短的词；否则 null。",
    ]
    if inner:
        lines.append("- inner：你此刻心里真正在想的一句话，第一人称，20 字以内。可以和嘴上说的不一样。只想你的心事里有的事，不要编。")
    lines.append("- leave：你要结束这次相处时为 true。")
    if scene:
        lines.append("- beats：这一轮里真正发生了的必经事件的编号（数字列表），没有就是 []。")
    return "\n".join(lines)


def stage_text(trust: int, warmth: int) -> str:
    t = ("还有戒心" if trust < 10 else "渐渐放下戒心" if trust < 25 else "信得过" if trust < 50 else "很信任")
    w = ("客客气气" if warmth < 10 else "愿意亲近" if warmth < 25 else "很在意" if warmth < 50 else "放不下")
    return f"信任：{t}（{trust}）；亲近：{w}（{warmth}）"


def _lines(items, empty="（没有）"):
    items = [i for i in items if i]
    return "\n".join(f"- {i}" for i in items) if items else empty


def fill(text: str, player: dict) -> str:
    """把 {名} {店名} 和性别相关的占位符（如 {ta}）换成这个玩家的。"""
    if not text:
        return text
    text = text.replace("{名}", player.get("name", "")).replace("{店名}", player.get("shop", ""))
    for k, v in (player.get("words") or {}).items():
        text = text.replace(k, v)
    return text


def turn_system(ctx: dict) -> str:
    npc, p, life = ctx["npc"], ctx["player"], ctx["life"]
    info = ctx["stage_info"]
    scene = ctx.get("scene")
    if scene:
        # 导演场景发生在这段时日的开头：推演出来的"这段时日的生活"写的是之后的事，不给，免得提前说出来
        days = f"## 这段时日\n现在是{info.get('label', '')}。{info.get('gap', '')}这一场戏就发生在这段时日的开头。"
    else:
        days = (f"## 这段时日\n现在是{info.get('label', '')}。{info.get('gap', '')}\n"
                f"- 心情：{life.get('mood', '')}\n- 这段时日在做的事：{life.get('doing', '')}\n"
                f"- 心里挂着的：{life.get('on_mind', '')}\n"
                f"- 可以随口说起的小事：{'；'.join(life.get('small_news') or []) or '没有'}")
    parts = [
        TURN_GUIDE,
        "# 世界\n" + ctx["world_text"],
        npc.bible,
        # ---- 以上不变，以下随情境变化 ----
        "## 你的心事\n" + ctx["secret"],
        days,
        f"## 近来的消息\n这些是你最近听到的新鲜事，可以偶尔当闲话提起，别夸大，也别每次都提。和你所处的时节对不上的，不提。\n{_lines(ctx['news'])}",
        f"## 眼前这个人\n{ctx['player_intro']}\n"
        f"这一章的你们：{(scene or {}).get('stage') or ctx['npc_stage']}\n"
        f"你们的交情：{stage_text(ctx['bond']['trust'], ctx['bond']['warmth'])}\n\n"
        f"你对这个人的看法：\n{ctx['impression'] or '还谈不上有什么看法。'}\n\n"
        f"你知道的关于这个人的事（这个人告诉你的，或你亲眼见过的）：\n{_lines(ctx['facts'])}\n\n"
        f"你们之间的往事（你的日记，按时间先后）：\n{_lines(ctx['episodes'])}\n\n"
        f"你们之间的约定：\n{_lines(ctx['commitments'])}\n\n"
        f"你已经对这个人讲过的事（别当成第一次讲）：\n{_lines(ctx['told'])}\n\n"
        f"你听说的关于这个人的事（注意来源和可靠程度）：\n{_lines(ctx['heard'])}",
    ]
    if scene:
        beats = "\n".join(f"{i + 1}. [{'已发生' if (i + 1) in scene['done'] else '未发生'}] {b}"
                          for i, b in enumerate(scene["beats"]))
        state = scene.get("player_state") or ""
        parts.append(
            "## 这一场戏（导演说明）\n"
            f"场景：{scene['scene']}\n"
            + (f"对方此刻的状态（导演给你的提示；你只能从对方的言行里察觉，不能直接说破）：{state}\n" if state else "")
            + f"\n必经事件（按顺序自然地推进，每轮推进一两件，不要一次演完；有的需要对方配合，就顺着对方的话引过去）：\n{beats}\n\n"
            f"关键台词（到了合适的时机，原样说出）：\n{_lines(scene.get('key_lines') or [])}\n"
            + (f"\n分支：{scene['branch']}\n" if scene.get("branch") else "")
            + (f"\n补充：{scene['situation']}\n" if scene.get("situation") else "")
            + f"\n{ctx['budget']}")
    else:
        lead = "" if ctx.get("custom_arrive") else f"{p['name']}来{ctx['place']}找你。"
        parts.append(f"## 这次见面\n{lead}{ctx['meeting']}"
                     + (f"\n{ctx['situation']}" if ctx.get("situation") else "")
                     + f"\n{ctx['budget']}"
                     + (f"\n你此刻的心情：{ctx['mood']}" if ctx.get("mood") else ""))
    parts.append(turn_format(npc, bool(scene)) + ("\n" + expr_rule(npc) if npc.model_expressions else ""))
    return fill("\n\n".join(parts), p)


def budget_text(left_after: int | None, scene: bool) -> str:
    """left_after：这一句说完后还剩几轮。None 表示不限。"""
    if left_after is None:
        return ""
    if left_after <= 0:
        return ("这是这场戏的最后一轮：把还没发生的必经事件自然地演完，用一个动作收住。leave 设为 true。" if scene
                else "这是这次相处的最后一句了：自然地道别或者结束这次谈话（理由来自你这段时日的生活和此刻的情境），leave 设为 true。")
    if left_after == 1:
        return ("这场戏只剩一轮了，开始往收尾走。" if scene
                else "再说一轮就该分开了。这一句开始自然地往收尾走，但别直接赶人。")
    return f"（这{'场戏' if scene else '次相处'}大约还能再说 {left_after} 轮。）"


def player_line(kind: str, text: str, name: str) -> str:
    if kind == "gesture":
        return f"（{name}{text}）"
    return text


OPTIONS_SYS = """[用途:options]
你在为一款对话游戏写"玩家可以说的话"。玩家扮演的角色（下面称"我"）的设定如下：

{persona}

"我"正在和{npc}对话。根据对话，给"我"写 {n} 个接下来可以说的选项。站在"我"的视角，用"我"的口吻对{npc}说话（称对方"你"或"{title}"）。

{n} 个选项分工不同：
{kinds}

要求：
- 符合"我"的设定和口吻。设定里写明的秘密和规矩，必须遵守。
- 只能引用{npc}真的说过的话。不要把"我"说过的话当成{npc}说的，也不要假设{npc}知道"我"没说过的事。
- label 是按钮上的字，12 个字以内，写意图；say 是点下去后"我"完整说出的话，30 字以内，口语。
- 几个选项要明显不同，不要都是问句。
- 符合这个世界：{world}
{hint}
只输出 JSON：{{"options":[{example}]}}"""


def options_messages(npc, persona: str, transcript: list[tuple[str, str]], journal: list[str], hint: str = "",
                     kinds: list[dict] | None = None, world_brief: str = "") -> list[dict]:
    kinds = kinds or [{"kind": "追问", "desc": "往深里问。"}, {"kind": "表态", "desc": "表达看法。"},
                      {"kind": "带入自己", "desc": "说起自己的事。"}]
    convo = "\n".join(f"{'我' if who == 'player' else npc.name}：{t}" for who, t in transcript[-10:])
    user = (f"我最近的经历（{npc.name}未必知道）：\n{_lines(journal)}\n\n"
            f"对话（最后一句是{npc.name}刚说的）：\n{convo}")
    kind_text = "\n".join(f"{i + 1}. {k['kind']}：{k.get('desc', '')}" for i, k in enumerate(kinds))
    example = ",".join('{"kind":"%s","label":"…","say":"…"}' % k["kind"] for k in kinds)
    sys = OPTIONS_SYS.format(persona=persona or "（未设定）", npc=npc.name, title=npc.title, n=len(kinds),
                             kinds=kind_text, world=world_brief or "这个故事的世界，不要出现不属于它的东西。",
                             hint=f"- 这一轮的特别要求：{hint}" if hint else "", example=example)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


REFLECT_TASK = """[用途:reflect]
你是{name}。刚才和{player}相处了一阵。现在夜深了，你回想这次相处，整理自己的记忆。

只输出一个 JSON 对象：
{{
  "diary": "用你自己的口吻写一段日记，记下这次你在意的东西：发生了什么、对方说了什么、你当时怎么想。60 到 180 字。要具体，写原话或细节。",
  "salience": 1到5的整数，这次对你有多重要（普通寒暄 1，交心或起了冲突 4-5），
  "impression": "你现在对{player}的整体看法，用你的口吻，120 字以内。在旧看法的基础上更新。",
  "facts": ["这次新知道的、关于{player}的具体事实，必须是对方亲口说的或你亲眼见的。没有就是空数组"],
  "commitments_new": [{{"by": "npc 或 player", "text": "这次谁答应了谁什么"}}],
  "commitments_done": [这次已经兑现的旧约定的编号],
  "told": ["这次你对{player}讲过的往事、观点、自己的事，每条一个短语"],
  "quotes": [{{"topic": "话题", "text": "{player}的原话"}}]{relay_field}
}}
- quotes：{player}这次说过的、你会记很久的话，一字不改地照抄，最多 3 条。topic 用两三个字概括这句话说的是什么{topics}。没有就是空数组。
{relay_note}不要编造没有发生的事。"""

RELAY_FIELD = ',\n  "relay": [{"to": "转告给谁（写名字）", "text": "你会怎么转告，一句话，说清是谁说的、说了什么"}]'


def reflect_messages(npc, ctx: dict) -> list[dict]:
    relays = ctx.get("relays") or []
    topics = f"，尽量用这些词：{'、'.join(npc.quote_topics)}" if npc.quote_topics else ""
    relay_note = ""
    if relays:
        relay_note = ("- relay：这次听到的事里，有没有你会转告给别人的？你的习惯是：\n"
                      + "\n".join(f"  - 对{name}：{what}" for name, what in relays)
                      + "\n  只转告这次真的听到的事。没有就是空数组。\n")
    task = REFLECT_TASK.format(name=npc.name, player=ctx["player"]["name"], topics=topics,
                               relay_field=RELAY_FIELD if relays else "", relay_note=relay_note)
    sys = "\n\n".join([task, npc.bible, "## 你的心事（供你理解自己）\n" + ctx["secret"]])
    user = (f"## 你对{ctx['player']['name']}原来的看法\n{ctx['impression'] or '还没有。'}\n\n"
            f"## 已经知道的事\n{_lines(ctx['facts'])}\n\n"
            f"## 旧约定（编号：内容）\n{_lines(ctx['commitments'])}\n\n"
            f"## 这次的相处（{ctx['label']}）\n{ctx['transcript']}")
    return [{"role": "system", "content": fill(sys, ctx["player"])}, {"role": "user", "content": fill(user, ctx["player"])}]


LIFE_TASK = """[用途:life]
你是{name}。新的一段时日开始了，请想一想这段时日的你是什么样子。

只输出一个 JSON 对象：
{{
  "mood": "这段时日的基础心情，一个短语",
  "doing": "这段时日在做的一两件事，要具体，40 到 90 字",
  "on_mind": "心里挂着的一件事，一句话",
  "small_news": ["一两件可以随口跟人说起的小事"],
  "openers": ["有人来找你时，你可能正在做的事，三个，各不相同"]
}}

规则：
- "这段时日必须成立的事"一定要体现，不能和它矛盾。
- 可以、也应该把你和{player}之间最近发生的事算进去：它会影响你的心情、你在做的事、你挂心的事。
- 不要越过"这一章的你们"：还没到的那一步，心里也不要先走到。
- 你的心事只想到"你的心事"允许的程度。"""


def life_messages(npc, ctx: dict) -> list[dict]:
    a = ctx["anchor"]
    sys = "\n\n".join([LIFE_TASK.format(name=npc.name, player=ctx["player"]["name"]), npc.bible,
                       "## 你的心事\n" + ctx["secret"]])
    user = (f"现在是{ctx['label']}。{ctx['gap']}\n\n"
            f"这段时日的大状况：{a.get('situation', '')}\n"
            f"这段时日必须成立的事：\n{_lines(a.get('must', []))}\n\n"
            f"这一章的你们：{ctx['stage']}\n\n"
            f"上一段时日的你：\n{json.dumps(ctx['previous'], ensure_ascii=False) if ctx['previous'] else '（没有记录）'}\n\n"
            f"你对{ctx['player']['name']}的看法：{ctx['impression'] or '还没有'}\n\n"
            f"你们最近的往事：\n{_lines(ctx['recent'])}\n\n"
            f"参考写法（不要照抄）：{json.dumps(a.get('fallback', {}), ensure_ascii=False)}")
    return [{"role": "system", "content": fill(sys, ctx["player"])}, {"role": "user", "content": fill(user, ctx["player"])}]


LINE_TASK = """[用途:line]
你是{name}。下面是一段剧情，演到了你要说一句话的地方。这句话要从你对{player}的真实记忆里来，让这一刻只属于这个玩家。

规则：
- 只能用下面"往事""知道的事""听说的事""原话"里真实发生过的内容，不要编造。引用原话时一字不改。
- 符合你的性格和口吻。
- 只输出这句台词本身：不要引号，不要名字前缀，不要解释。可以带一个简短的括号动作。
{expr}"""


def line_messages(npc, ctx: dict) -> list[dict]:
    sys = "\n\n".join([LINE_TASK.format(name=npc.name, player=ctx["player"]["name"],
                                        expr="- " + expr_rule(npc) if npc.model_expressions else ""),
                       npc.bible, "## 你的心事\n" + ctx["secret"]])
    name = ctx["player"]["name"]
    user = (f"你对{name}的看法：{ctx['impression'] or '还没有'}\n\n"
            f"你知道的关于这个人的事：\n{_lines(ctx['facts'])}\n\n"
            f"你听说的关于这个人的事（来源写在前面）：\n{_lines(ctx['heard'])}\n\n"
            f"你们之间的往事：\n{_lines(ctx['episodes'])}\n\n"
            f"这个人对你说过的原话（按时间先后）：\n{_lines(ctx['snippets'])}\n\n"
            f"剧情（到这里，轮到你说）：\n{ctx['before']}\n\n"
            f"这句话要写的是：{ctx['instruction']}")
    return [{"role": "system", "content": fill(sys, ctx["player"])}, {"role": "user", "content": fill(user, ctx["player"])}]
