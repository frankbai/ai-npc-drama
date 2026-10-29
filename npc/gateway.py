"""模型网关。运行时只依赖两个能力：流式文本、JSON 对象输出。

不使用任何厂商独有的功能，所以换成别家的模型（DeepSeek、Qwen、GLM、本地部署的模型等任何
Chat Completions 兼容服务）只需要改配置。
"""
from __future__ import annotations

import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from typing import Iterator


class ModelError(Exception):
    pass


def raise_http(e):
    detail = e.read().decode("utf-8", "replace")[:500]
    raise ModelError(f"模型服务返回 {e.code}：{detail}") from None


def extract_json(text: str) -> dict:
    """从模型输出里取出一个 JSON 对象。取不出来、或者取出来的不是对象，都抛 ModelError。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for cand in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        if not cand:
            continue
        try:
            out = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(out, dict):
            return out
    raise ModelError(f"模型没有返回合法的 JSON 对象：{text[:200]}")


# 读响应时可能遇到的网络错误：一律转成 ModelError，调用方只需要处理这一种
_NET_ERRORS = (OSError, TimeoutError, http.client.HTTPException, ValueError)


class OpenAICompatGateway:
    """Chat Completions 兼容接口。"""

    def __init__(self, cfg: dict):
        self.base = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
        self.key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self.model = cfg.get("model", "gpt-6.1-sol")
        self.fast_model = cfg.get("fast_model") or self.model
        self.timeout = float(cfg.get("timeout", 60))
        self.reasoning_effort = cfg.get("reasoning_effort")  # 例如 "low"；不填则不发送
        self.temperature = cfg.get("temperature")              # 不填则不发送
        self.max_tokens_param = cfg.get("max_tokens_param", "max_completion_tokens")
        self.json_mode = cfg.get("json_mode", True)
        self.stream_usage = cfg.get("stream_usage", True)  # 流式时请求返回用量；不支持的服务可关掉
        self.on_usage = None  # 宿主可以挂一个回调：on_usage(model, usage_dict)

    def _report(self, model: str, usage):
        if self.on_usage and isinstance(usage, dict):
            try:
                self.on_usage(model, usage)
            except Exception:  # noqa: BLE001  统计失败不影响对话
                pass

    @property
    def url(self) -> str:
        return self.base if self.base.endswith("/chat/completions") else self.base + "/chat/completions"

    def _body(self, messages, model, max_tokens, stream, json_mode=False) -> dict:
        body = {"model": model, "messages": messages, "stream": stream,
                self.max_tokens_param: max_tokens}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if json_mode and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _open(self, body: dict):
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
        for attempt in (1, 2):  # 中转服务偶尔 502：在还没收到任何内容之前，自动重试一次
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if attempt == 1 and e.code in (500, 502, 503, 504):
                    time.sleep(1.0)
                    continue
                raise_http(e)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 1 and not isinstance(e, TimeoutError):
                    time.sleep(1.0)
                    continue
                raise ModelError(f"连不上模型服务：{e}") from None

    def stream(self, messages: list[dict], max_tokens: int = 1200, fast: bool = False) -> Iterator[str]:
        body = self._body(messages, self.fast_model if fast else self.model, max_tokens, True)
        if self.stream_usage:
            body["stream_options"] = {"include_usage": True}
        resp = self._open(body)
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("usage"):
                    self._report(body["model"], chunk["usage"])
                for ch in chunk.get("choices") or []:
                    piece = ((ch or {}).get("delta") or {}).get("content") if isinstance(ch, dict) else None
                    if isinstance(piece, str) and piece:
                        yield piece
        except _NET_ERRORS as e:
            raise ModelError(f"读取模型输出时断开了：{e}") from None
        finally:
            resp.close()

    def json(self, messages: list[dict], max_tokens: int = 1500, fast: bool = False) -> dict:
        body = self._body(messages, self.fast_model if fast else self.model, max_tokens, False, True)
        resp = self._open(body)
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        except _NET_ERRORS as e:
            raise ModelError(f"读取模型输出时出错：{e}") from None
        finally:
            resp.close()
        if not isinstance(data, dict):
            raise ModelError(f"模型返回格式异常：{str(data)[:200]}")
        self._report(body["model"], data.get("usage"))
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ModelError(f"模型返回格式异常：{str(data)[:200]}") from None
        return extract_json(text)


class MockGateway:
    """离线模拟：不调用任何外部服务，用来检查流程是否通畅。对白是占位的，不代表真实效果。"""

    def __init__(self, cfg: dict | None = None):
        self.delay = float((cfg or {}).get("mock_delay", 0.02))

    @staticmethod
    def _purpose(messages) -> str:
        m = re.search(r"\[用途:(\w+)\]", messages[0]["content"])
        return m.group(1) if m else "turn"

    @staticmethod
    def _name(messages) -> str:
        m = re.search(r"^# (\S+)\s+## 你是谁", messages[0]["content"], re.M)
        return m.group(1) if m else "对方"

    @staticmethod
    def _exprs(sys_: str) -> list[str]:
        m = re.search(r"只能从这几个里选一个：(.+?)。", sys_)
        return re.findall(r"〔(.+?)〕", m.group(1)) if m else []

    def stream(self, messages, max_tokens=1200, fast=False) -> Iterator[str]:
        rnd = random.Random(len(messages) * 7919 + len(messages[-1]["content"]))
        name = self._name(messages)
        sys_ = messages[0]["content"]
        last = messages[-1]["content"]
        exprs = self._exprs(sys_)
        tag = f"〔{rnd.choice(exprs)}〕" if exprs else ""
        if self._purpose(messages) == "line":
            text = tag + "（模拟的专属台词）你那天说的话，我一直记着。"
        else:
            lines = [
                "嗯，这个我得想想……你怎么看？",
                "说起来，这阵子我手头正有件事拿不定主意。",
                "你这么一说，倒让我想起点别的。",
                "（看了你一眼）先坐吧。",
            ]
            reply = f"（{name}抬起头）你来了。" if ("来到" in last or "场景开始" in last or "门外" in last) else rnd.choice(lines)
            state = {"bond": {"trust": 2 if ("谢" in last or "帮" in last) else rnd.choice([0, 0, 1]),
                              "warmth": rnd.choice([0, 1])},
                     "why": "（模拟）你认真听我说话", "mood": None, "leave": "告辞" in last}
            if '"inner"' in sys_:
                state["inner"] = "（模拟心声）这人倒有意思。"
            if "## 这一场戏" in sys_:
                m = re.search(r"(\d+)\. \[未发生\]", sys_)
                state["beats"] = [int(m.group(1))] if m else []
            text = tag + f"{reply}\n<<<\n" + json.dumps(state, ensure_ascii=False)
        for i in range(0, len(text), 4):
            if self.delay:
                time.sleep(self.delay)
            yield text[i:i + 4]

    def json(self, messages, max_tokens=1500, fast=False) -> dict:
        if self.delay:
            time.sleep(self.delay * 5)
        p = self._purpose(messages)
        sys_ = messages[0]["content"]
        if p == "options":
            kinds = re.findall(r'\{"kind":"([^"]+)","label"', sys_) or ["追问", "表态", "带入自己"]
            samples = [("为什么这么想？", "你为什么会这么想？"), ("我倒不这么觉得", "我倒不这么觉得。"),
                       ("说说店里的事", "店里这几日倒是出了件事。"), ("换个话头", "不说这个了。")]
            return {"options": [{"kind": k, "label": samples[i % 4][0], "say": samples[i % 4][1]}
                                for i, k in enumerate(kinds)]}
        if p == "reflect":
            user = messages[-1]["content"]
            said = [ln.split("：", 1)[1] for ln in user.split("## 这次的相处", 1)[-1].splitlines()
                    if "：" in ln and not ln.startswith("你：") and not ln.startswith("（")]
            out = {"diary": "（模拟）今天又说了几句话。", "salience": 2,
                   "impression": "（模拟）话不多，句句有分量。", "facts": [], "commitments_new": [],
                   "commitments_done": [], "told": [],
                   "quotes": [{"topic": "闲聊", "text": s} for s in said if len(s) >= 4][:1]}
            if '"relay"' in sys_:
                m = re.search(r"^  - 对(.+?)：", sys_, re.M)
                if m:
                    out["relay"] = [{"to": m.group(1), "text": "（模拟转述）掌柜这次说了些话。"}]
            return out
        if p == "life":
            return {"mood": "（模拟）平静", "doing": "（模拟）照常过日子。", "on_mind": "（模拟）眼前的事。",
                    "small_news": ["（模拟）今天雪小了些"], "openers": ["正在忙"]}
        return {}


def make_gateway(cfg: dict):
    return MockGateway(cfg) if cfg.get("mode", "mock") == "mock" else OpenAICompatGateway(cfg)
