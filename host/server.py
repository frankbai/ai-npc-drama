"""Demo 宿主：模拟"游戏"这一侧。

- host/story.py：剧情导演（章节、CG、事件、线索、指认、结局）
- 这里：HTTP 接口、静态页面、线上模式（口令、管理员、限流、用量统计）、模型配置

运行时（npc/）完全不知道章节、CG、事件是什么，它只收：阶段、事件、记忆、导演场景、专属台词指示。

本机运行：python3 run.py。设置环境变量 ACCESS_CODE 即进入线上模式（见 docs/部署到Render.md）。
换一个内容包：设置环境变量 CONTENT_DIR 指向你的内容目录（见 docs/内容包写作指南.md）。
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from npc import content as content_mod  # noqa: E402
from npc.gateway import make_gateway  # noqa: E402
from npc.runtime import Closed, Runtime  # noqa: E402
from npc.store import open_store  # noqa: E402
from host.story import Director  # noqa: E402

WEB = Path(__file__).resolve().parent / "web"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon"}
DEFAULT_CONFIG = {"mode": "mock", "base_url": "https://api.openai.com/v1", "api_key": "",
                  "model": "gpt-6.1-sol", "fast_model": "", "reasoning_effort": "low",
                  "timeout": 60, "max_tokens_param": "max_completion_tokens", "json_mode": True,
                  "stream_usage": True}
# 环境变量 → 模型配置项。每一项的含义见 docs/配置说明.md
ENV_CONFIG = {"NPC_MODE": "mode", "NPC_BASE_URL": "base_url", "OPENAI_API_KEY": "api_key",
              "NPC_MODEL": "model", "NPC_FAST_MODEL": "fast_model",
              "NPC_REASONING_EFFORT": "reasoning_effort", "NPC_MAX_TOKENS_PARAM": "max_tokens_param",
              "NPC_TIMEOUT": "timeout", "NPC_JSON_MODE": "json_mode", "NPC_STREAM_USAGE": "stream_usage"}
CN = timezone(timedelta(hours=8))
COOKIE = "npc_auth"
CONTENT = Path(os.environ.get("CONTENT_DIR") or (ROOT / "content")).resolve()


def env_value(key: str, raw: str):
    """把环境变量的文字转成配置值：none/off 表示不发送这个参数；开关类接受 1/0、true/false。"""
    raw = raw.strip()
    if key in ("json_mode", "stream_usage"):
        return raw.lower() in ("1", "true", "yes", "on")
    if key == "timeout":
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_CONFIG["timeout"]
    if key in ("reasoning_effort", "fast_model") and raw.lower() in ("none", "off", "null", "no"):
        return None
    return raw


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Host:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        # ---- 线上模式的门禁与限制 ----
        self.access_code = os.environ.get("ACCESS_CODE", "").strip()
        self.admin_code = os.environ.get("ADMIN_CODE", "").strip()
        self.online = bool(self.access_code)
        if self.online and not self.admin_code:
            raise SystemExit("线上模式需要同时设置 ADMIN_CODE")
        if self.online and self.admin_code == self.access_code:
            raise SystemExit("ADMIN_CODE 必须和 ACCESS_CODE 不同")
        # 本机模式只接受这些 Host 访问（防止别的网页借 DNS 重绑定读写本机服务）。逗号分隔，可以加自己的域名
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", "[::1]"} | {
            h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
        self.show_mind = os.environ.get("SHOW_MIND", "0") == "1"
        self.turns_per_min = env_int("TURNS_PER_MINUTE", 10)
        self.turns_per_day = env_int("TURNS_PER_DAY", 300)
        self.turns_total_per_day = env_int("TURNS_TOTAL_PER_DAY", 0)    # 所有访客加起来每天最多几句，0 = 不限
        self._fails: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.prices = (env_float("PRICE_INPUT", 2.0), env_float("PRICE_CACHED", 0.1), env_float("PRICE_OUTPUT", 10.0))
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._daily: dict[tuple, int] = collections.defaultdict(int)
        self._rl_lock = threading.Lock()

        # ---- 模型配置 ----
        self.cfg_path = data_dir / "config.json"
        self.cfg = {**DEFAULT_CONFIG, **(json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {})}
        for env, key in ENV_CONFIG.items():
            if os.environ.get(env, "").strip():
                self.cfg[key] = env_value(key, os.environ[env])
        if os.environ.get("OPENAI_API_KEY") and not os.environ.get("NPC_MODE"):
            self.cfg["mode"] = "live"

        # ---- 内容 ----
        self.content = content_mod.load(CONTENT)

        # ---- 存储与运行时 ----
        self.store = open_store(data_dir)
        for sql in ("CREATE TABLE IF NOT EXISTS owners (player TEXT PRIMARY KEY, vid TEXT)",
                    "CREATE TABLE IF NOT EXISTS api_usage (day TEXT, model TEXT, input INTEGER, cached INTEGER, "
                    "output INTEGER, calls INTEGER, PRIMARY KEY (day, model))"):
            self.store.x(sql)
        for k, v in (self.store.get_meta("model_overrides") or {}).items():
            if k in DEFAULT_CONFIG and k != "api_key":
                self.cfg[k] = v
        # 登录凭证的签名密钥：优先用 SESSION_SECRET；没设就随机生成一个存进库里（不能从口令推出来）
        secret = os.environ.get("SESSION_SECRET", "").strip()
        if not secret:
            secret = self.store.get_meta("session_secret")
            if not secret:
                secret = secrets.token_hex(32)
                self.store.set_meta("session_secret", secret)
        self.secret = secret.encode()
        self.rt = Runtime(self.content, self.store, None)
        self._set_gateway()
        self.d = Director(CONTENT, self.rt)

    # ================================================================ 模型与用量
    def _set_gateway(self):
        g = make_gateway(self.cfg)
        if hasattr(g, "on_usage"):
            g.on_usage = self._record_usage
        self.rt.g = g

    def _record_usage(self, model: str, u: dict):
        inp = int(u.get("prompt_tokens") or 0)
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        out = int(u.get("completion_tokens") or 0)
        day = datetime.now(CN).strftime("%Y-%m-%d")
        self.store.x("""INSERT INTO api_usage VALUES(?,?,?,?,?,1) ON CONFLICT(day, model) DO UPDATE SET
                        input=api_usage.input+excluded.input, cached=api_usage.cached+excluded.cached,
                        output=api_usage.output+excluded.output, calls=api_usage.calls+1""",
                     day, model, inp, cached, out)

    def usage(self) -> dict:
        rows = self.store.q("SELECT * FROM api_usage ORDER BY day DESC, model")
        pi, pc, po = self.prices
        for r in rows:
            r["cost_usd"] = round(((r["input"] - r["cached"]) * pi + r["cached"] * pc + r["output"] * po) / 1e6, 3)
        turns = self.store.one("SELECT COUNT(*) n FROM messages WHERE role='npc'")["n"]
        return {"rows": rows, "total_usd": round(sum(r["cost_usd"] for r in rows), 2), "npc_lines": turns,
                "players": len(self.store.players()), "prices_per_million": {"input": pi, "cached": pc, "output": po}}

    def public_cfg(self) -> dict:
        c = {k: v for k, v in self.cfg.items() if k != "api_key"}
        c["has_key"] = bool(self.cfg.get("api_key") or os.environ.get("OPENAI_API_KEY"))
        return c

    def update_cfg(self, new: dict):
        overrides = self.store.get_meta("model_overrides") or {}
        for k in DEFAULT_CONFIG:
            if k in new and not (k == "api_key" and new[k] == ""):
                self.cfg[k] = new[k]
                if k != "api_key":
                    overrides[k] = new[k]
        self.store.set_meta("model_overrides", overrides)
        if new.get("clear_key"):
            self.cfg["api_key"] = ""
        saved = {k: v for k, v in self.cfg.items() if not (self.online and k == "api_key")}
        self.cfg_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
        try:
            os.chmod(self.cfg_path, 0o600)
        except OSError:
            pass
        self._set_gateway()

    # ================================================================ 门禁
    def sign(self, vid: str, role: str) -> str:
        sig = hmac.new(self.secret, f"{vid}.{role}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{vid}.{role}.{sig}"

    def verify(self, token: str | None) -> tuple[str | None, str | None]:
        if not self.online:
            return "local", "admin"
        try:
            vid, role, _ = (token or "").split(".")
        except ValueError:
            return None, None
        if hmac.compare_digest(self.sign(vid, role).encode(), token.encode()) and role in ("player", "admin"):
            return vid, role
        return None, None          # 签名不对：什么都不认，连访客编号也不沿用

    def login(self, code: str) -> str | None:
        code = (code or "").strip().encode()
        if code and hmac.compare_digest(code, self.admin_code.encode()):
            return "admin"
        if code and hmac.compare_digest(code, self.access_code.encode()):
            return "player"
        return None

    def owns(self, vid: str, role: str, player: str) -> bool:
        if not player or not self.store.one("SELECT 1 FROM players WHERE id=?", player):
            return False                 # 没有这个存档（比如浏览器里记着别的程序、别的数据目录留下的编号）
        if role == "admin":
            return True
        r = self.store.one("SELECT vid FROM owners WHERE player=?", player)
        return bool(r and r["vid"] == vid)

    def rate_ok(self, vid: str, ip: str = "") -> str | None:
        """限流：每个访客、每个 IP（同一 IP 下可能有好几个人，额度放宽到 3 倍）、全站每天。"""
        now = time.time()
        today = datetime.now(CN).strftime("%Y-%m-%d")
        keys = [(vid, 1)] + ([(f"ip:{ip}", 3)] if ip else [])
        with self._rl_lock:
            for key, mult in keys:
                q = self._hits[key]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= self.turns_per_min * mult:
                    return "说得太快了，歇一会儿再聊。"
                if self._daily[(key, today)] >= self.turns_per_day * mult:
                    return "今天聊得够多了，明天再来吧。"
            if self.turns_total_per_day and self._daily[("*", today)] >= self.turns_total_per_day:
                return "今天来玩的人太多，额度用完了，明天再来吧。"
            for key, _ in keys:
                self._hits[key].append(now)
                self._daily[(key, today)] += 1
            self._daily[("*", today)] += 1
        return None

    def login_throttled(self, ip: str, failed: bool = False) -> bool:
        """同一个 IP 一分钟内输错 5 次口令，就先别试了。"""
        now = time.time()
        with self._rl_lock:
            q = self._fails[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if failed:
                q.append(now)
            return len(q) >= 5

    # ================================================================ 状态
    def players_for(self, vid: str, role: str) -> list[dict]:
        owners = {r["player"]: r["vid"] for r in self.store.q("SELECT * FROM owners")}
        out = []
        for p in self.store.players():
            mine = owners.get(p["id"]) == vid or not self.online
            if mine or role == "admin":
                out.append({**p, "mine": mine})
        return out

    def new_player(self, name: str, shop: str, gender: str, avatar: str, vid: str) -> str:
        pid = self.d.new_player(name, shop, gender, avatar)
        self.store.x("INSERT INTO owners VALUES(?,?)", pid, vid)
        return pid

    @staticmethod
    def assets() -> dict:
        """有哪些美术素材。页面只请求列表里有的图，没有的直接用文字场景卡和临时头像。每次现查，放进新图刷新就生效。"""
        out = {}
        for kind in ("cg", "avatars"):
            names = set()
            for d in (CONTENT / "assets" / kind, WEB / kind):
                if d.is_dir():
                    names |= {f.name for f in d.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")}
            out[kind] = sorted(names)
        return out

    def history(self, player: str, sid: str | None, role: str) -> dict:
        """一次相处到目前为止的对话（刷新页面后接着聊、接着演）。不给 sid 就是正在进行的自由对话。"""
        sid = sid or self.d.active_session(player)
        s = self.store.session(sid) if sid else None
        if not s or s["player"] != player:
            return {"session": None, "npc": None, "messages": []}
        visible, label = self.rt.inner_visibility(player, s["npc"])
        msgs = []
        for m in self.store.messages(sid):
            meta = m.get("meta") or {}
            item = {"role": m["role"], "kind": m["kind"], "text": m["text"], "expr": meta.get("expr")}
            if m["role"] == "npc":
                item["inner"] = meta.get("inner") if visible else None
                item["inner_label"] = None if visible else label
                if role == "admin":
                    item["meta"] = meta
            msgs.append(item)
        spec = s["spec"]
        out = {"session": sid, "npc": s["npc"], "kind": s["kind"], "ended": bool(s["ended"]), "messages": msgs,
               "options": spec.get("last_options") or [],
               "gestures": [] if s["ended"] else list(self.content.pack.get("gestures") or [])}
        if s["kind"] == "scene":
            played = sum(1 for m in msgs if m["role"] == "player" and m["kind"] != "arrive")
            out["scene"] = {"done": spec.get("done", []), "total": len(spec.get("beats", []))}
            out["rounds"] = {"used": played, "limit": spec.get("rounds")}
        return out

    def state(self, player: str | None, vid: str, role: str) -> dict:
        st = {"players": self.players_for(vid, role), "story": self.d.static_info(),
              "config": self.public_cfg() if role == "admin" else {"mode": self.cfg["mode"], "model": self.cfg["model"]},
              "me": {"role": role, "online": self.online, "show_mind": role == "admin" or self.show_mind},
              "assets": self.assets()}
        if player:
            st["player"] = player
            st.update(self.d.state(player))
        return st


HOST: Host | None = None


class Denied(Exception):
    def __init__(self, msg: str, code: int = 403):
        super().__init__(msg)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    server_version = "AINPCDrama/1"
    timeout = 60          # 慢吞吞不发完请求的连接，60 秒后断开（不影响已经开始的流式回复）

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    # ---- 工具 ----
    def _json(self, obj, code=200, cookie: str | None = None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax{secure}")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("请求格式不对") from None
        if n < 0 or n > 20000:
            raise ValueError("请求太大")
        if not n:
            return {}
        try:
            b = json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("请求格式不对") from None
        if not isinstance(b, dict):
            raise ValueError("请求格式不对")
        return b

    def _ip(self) -> str:
        """访客 IP。线上部署在反向代理后面，取 X-Forwarded-For 里最后一个（代理自己加上去的，不能伪造）。"""
        xff = self.headers.get("X-Forwarded-For") if HOST.online else None
        return (xff.split(",")[-1].strip() if xff else self.client_address[0]) or ""

    def _host_ok(self) -> bool:
        """本机模式（没有口令，谁都是管理员）只接受 localhost 访问。"""
        if HOST.online:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host.split("]")[0] + "]"
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in HOST.allowed_hosts

    def _token(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[COOKIE].value if COOKIE in c else None

    def _auth(self) -> tuple[str, str]:
        vid, role = HOST.verify(self._token())
        if not role:
            raise Denied("请先输入体验口令", 401)
        return vid, role

    def _need_admin(self, role):
        if role != "admin":
            raise Denied("只有管理员可以这样做")

    def _need_player(self, vid, role, player):
        if not player or not HOST.owns(vid, role, player):
            raise Denied("这不是你的存档")

    def _need_session(self, vid, role, sid):
        s = HOST.store.session(sid or "")
        if not s:
            raise ValueError("会话不存在")
        self._need_player(vid, role, s["player"])
        return s

    def _limit(self, vid, role):
        if HOST.online and role != "admin" and (msg := HOST.rate_ok(vid, self._ip())):
            raise ValueError(msg)

    def _sse(self, gen, head: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(ev):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        try:
            if head:
                send(head)
            for ev in gen:
                send(ev)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Closed as e:
            send({"t": "closed", "message": str(e)})
        except ValueError as e:
            send({"t": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            send({"t": "error", "message": f"内部错误：{e}"})

    def _static(self, path: str):
        path = "/index.html" if path == "/" else path
        rel = path.lstrip("/")
        f = None
        if rel.startswith(("cg/", "avatars/")):            # 美术素材：先找内容包里的，再找页面自带的
            cand = (CONTENT / "assets" / rel).resolve()
            if (CONTENT / "assets").resolve() in cand.parents and cand.is_file():
                f = cand
        if f is None:
            cand = (WEB / rel).resolve()
            if WEB.resolve() in cand.parents and cand.is_file():
                f = cand
        if f is None:
            return self._json({"error": "not found"}, 404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, fn):
        try:
            return fn()
        except Denied as e:
            return self._json({"error": str(e), "login": e.code == 401}, e.code)
        except Closed as e:
            return self._json({"closed": True, "message": str(e)})
        except (KeyError, ValueError) as e:
            return self._json({"error": str(e) if not isinstance(e, KeyError) else f"缺少参数 {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": f"内部错误：{e}"}, 500)

    # ---- 路由 ----
    def _forbidden_host(self):
        return self._json({"error": "本机模式只接受 localhost / 127.0.0.1 访问。要放到网上给别人玩，请设置 ACCESS_CODE 和 "
                                    "ADMIN_CODE（见 docs/配置说明.md）；只想换个域名访问，就把它加进 ALLOWED_HOSTS。"}, 403)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json({"ok": True})
        if not self._host_ok():
            return self._forbidden_host()
        if not u.path.startswith("/api/"):
            return self._static(u.path)
        return self._guard(lambda: self._get_api(u))

    def _get_api(self, u):
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        vid, role = self._auth()
        player = qs.get("player")
        if u.path == "/api/state":
            if player and not HOST.owns(vid, role, player):
                player = None
            return self._json(HOST.state(player, vid, role))
        if u.path == "/api/cg":
            self._need_player(vid, role, player)
            return self._json(HOST.d.cg_payload(player, qs.get("part", "cg")))
        if u.path == "/api/mind":
            self._need_player(vid, role, player)
            if not (role == "admin" or HOST.show_mind):
                raise Denied("心智面板未开放")
            npc = qs.get("npc") or next(iter(HOST.content.npcs))
            HOST.rt._check_npc(npc)
            return self._json(HOST.rt.view(player, npc))
        if u.path == "/api/prompt":
            self._need_admin(role)
            return self._json(HOST.rt.last_prompt(qs["session"]) or {})
        if u.path == "/api/history":
            self._need_player(vid, role, player)
            return self._json(HOST.history(player, qs.get("session"), role))
        if u.path == "/api/epilogue":
            self._need_player(vid, role, player)
            return self._json(HOST.d.epilogue(player))
        if u.path == "/api/admin/usage":
            self._need_admin(role)
            return self._json(HOST.usage())
        if u.path == "/api/admin/export":
            self._need_admin(role)
            tables = ["meta", "players", "owners", "player_meta", "story_progress", "story_vars", "story_clues",
                      "done_events", "bond", "bond_log", "episodes", "commitments", "told", "heard", "quotes",
                      "journal", "news", "life", "lines", "sessions", "messages", "api_usage"]
            dump = {"exported_at": datetime.now(CN).isoformat(),
                    "tables": {t: HOST.store.q(f"SELECT * FROM {t}") for t in tables}}
            data = json.dumps(dump, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=ai-npc-drama-export.json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._forbidden_host()
        # 只收 JSON：别的网站能悄悄发的"简单请求"（表单、text/plain）一律拒绝
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._json({"error": "只接受 application/json"}, 415)
        return self._guard(lambda: self._post_api(u))

    def _post_api(self, u):
        b = self._body()
        if u.path == "/api/login":
            ip = self._ip()
            if HOST.login_throttled(ip):
                raise Denied("试得太多了，过一分钟再来", 429)
            role = HOST.login(str(b.get("code", "")))
            if not role:
                HOST.login_throttled(ip, failed=True)
                time.sleep(1)
                raise Denied("口令不对", 401)
            old_vid, _ = HOST.verify(self._token())    # 只有签名对的旧凭证才沿用访客编号
            vid = old_vid if old_vid and old_vid != "local" else secrets.token_hex(8)
            return self._json({"role": role}, cookie=HOST.sign(vid, role))
        if u.path == "/api/logout":
            return self._json({"ok": True}, cookie="")
        vid, role = self._auth()
        player = b.get("player")
        if u.path == "/api/players":
            pc = HOST.content.pack.get("player") or {}
            name = (b.get("name") or "").strip()[:int((pc.get("name") or {}).get("max", 8))]
            shop = (b.get("shop") or "").strip()[:int((pc.get("shop") or {}).get("max", 10))]
            if not name or not shop:
                raise ValueError("请把名字填完整")
            mine = HOST.store.one("SELECT COUNT(*) n FROM owners WHERE vid=?", vid)["n"]
            if HOST.online and role != "admin" and mine >= 5:
                raise ValueError("每人最多建 5 个存档")
            self._limit(vid, role)           # 建档会触发生活推演（调用模型），也算进限流
            return self._json({"id": HOST.new_player(name, shop, b.get("gender", ""), b.get("avatar", ""), vid)})
        if u.path == "/api/cg/slot":
            self._need_player(vid, role, player)
            self._limit(vid, role)
            return self._json(HOST.d.compose_slot(player, b["slot"], b.get("part", "cg")))
        if u.path == "/api/cg/scene":
            self._need_player(vid, role, player)
            self._limit(vid, role)
            sid, gen = HOST.d.start_scene(player)
            return self._sse(gen, {"t": "session", "session": sid})
        if u.path == "/api/cg/phase_done":
            self._need_player(vid, role, player)
            phase = b.get("phase")
            return self._json(HOST.d.phase_done(player, int(phase) if isinstance(phase, int) else None))
        if u.path == "/api/session/start":
            self._need_player(vid, role, player)
            self._limit(vid, role)           # 先限流，再动现在的谈话
            sid, gen = HOST.d.start_chat(player, b.get("npc", ""))   # 预算用完时抛 Closed，不调用模型
            return self._sse(gen, {"t": "session", "session": sid, "npc": b.get("npc")})
        if u.path == "/api/turn":
            self._need_session(vid, role, b.get("session"))
            self._limit(vid, role)
            return self._sse(HOST.rt.say(b["session"], b.get("kind", "say"), b.get("text", "")))
        if u.path == "/api/session/end":
            if b.get("session"):            # 只结束指定的那一次（不会误伤刚切换过去的存档）
                s = self._need_session(vid, role, b["session"])
                if s["kind"] == "chat":
                    HOST.d.close_active(s["player"], s["id"])
                return self._json({"ok": True})
            self._need_player(vid, role, player)
            HOST.d.close_active(player)
            return self._json({"ok": True})
        if u.path == "/api/event":
            self._need_player(vid, role, player)
            return self._json(HOST.d.do_event(player, b["event"], b.get("choice")))
        if u.path == "/api/chapter/next":
            self._need_player(vid, role, player)
            return self._json({"chapter": HOST.d.next_chapter(player)})
        if u.path == "/api/accuse":
            self._need_player(vid, role, player)
            return self._json(HOST.d.accuse(player, b.get("suspect", ""), b.get("clues") or []))
        if u.path == "/api/config":
            self._need_admin(role)
            HOST.update_cfg(b)
            return self._json(HOST.public_cfg())
        return self._json({"error": "not found"}, 404)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="AI NPC 互动剧情 Demo")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8800)))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"))
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(ROOT / "local-data")))
    a = ap.parse_args(argv)
    global HOST
    HOST = Host(Path(a.data_dir))
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    mode = "模拟模式（不调用模型）" if HOST.cfg["mode"] == "mock" else f"真实模式：{HOST.cfg['model']}"
    gate = "线上模式（需要口令）" if HOST.online else "本机模式"
    print(f"已启动：http://{a.host}:{a.port}/  ·  《{HOST.content.pack.get('title', '')}》  ·  {mode}  ·  {gate}"
          f"  ·  数据目录 {a.data_dir}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
