"""存储。SQLite 实现，另有 PostgreSQL 实现（PgStore）。正式游戏可以换成任何数据库，只要实现同样的方法。

每个玩家有自己的世界，所以几乎所有数据都以玩家为键：
- players：玩家（名字、据点名、性别），以及他走到第几个阶段（stage，Demo 里就是章节）和阶段说明（stage_info）
- bond / episodes / commitments / told / heard / journal / quotes：NPC 和这个玩家之间的一切
- bond_log：每个阶段交情的起点、最高点和终点（宿主可以拿来判断结局，比如"掉马前的情分"）
- life：NPC 在每个玩家世界里、每个阶段的生活推演
- news：消息。player 为空表示全服都能听到
- sessions / messages：对话。kind = chat（自由对话）或 scene（导演场景），spec 存这次相处的说明
- lines：生成过的专属台词（缓存，回看时不变）
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS players (
  id TEXT PRIMARY KEY, name TEXT, shop TEXT, gender TEXT DEFAULT '', created REAL,
  stage INTEGER DEFAULT 1, stage_info TEXT DEFAULT '{}');

CREATE TABLE IF NOT EXISTS bond (
  player TEXT, npc TEXT, trust INTEGER DEFAULT 0, warmth INTEGER DEFAULT 0,
  impression TEXT DEFAULT '', facts TEXT DEFAULT '[]', meetings INTEGER DEFAULT 0,
  PRIMARY KEY (player, npc));
CREATE TABLE IF NOT EXISTS bond_log (
  player TEXT, npc TEXT, stage INTEGER,
  start_trust INTEGER, start_warmth INTEGER, last_trust INTEGER, last_warmth INTEGER,
  max_trust INTEGER, max_warmth INTEGER, max_sum INTEGER,
  PRIMARY KEY (player, npc, stage));
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, npc TEXT, stage INTEGER, text TEXT, salience INTEGER);
CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, npc TEXT, stage INTEGER, who TEXT, text TEXT,
  status TEXT DEFAULT 'open', done_stage INTEGER);
CREATE TABLE IF NOT EXISTS told (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, npc TEXT, stage INTEGER, item TEXT);
CREATE TABLE IF NOT EXISTS heard (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, npc TEXT, stage INTEGER, text TEXT,
  source TEXT, certainty TEXT);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, npc TEXT, stage INTEGER, topic TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, stage INTEGER, text TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player TEXT, stage INTEGER, text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS life (
  player TEXT, npc TEXT, stage INTEGER, state TEXT, PRIMARY KEY (player, npc, stage));
CREATE TABLE IF NOT EXISTS lines (
  player TEXT, slot TEXT, text TEXT, PRIMARY KEY (player, slot));
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, player TEXT, npc TEXT, stage INTEGER, kind TEXT, spec TEXT DEFAULT '{}',
  started REAL, ended REAL, reflected INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, role TEXT, kind TEXT, text TEXT,
  meta TEXT DEFAULT '{}', created REAL);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, purpose TEXT, prompt TEXT, output TEXT,
  ms INTEGER, created REAL);
"""


class Store:
    def __init__(self, path: str | Path):
        self._calls = 0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.db.close()

    # ---- 通用 ----
    def q(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql: str, *args):
        with self.lock:
            self.db.execute(sql, args)

    def tx(self):
        """with store.tx(): 多条写入要么全成功要么全不写。"""
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                try:
                    store.db.execute("BEGIN")
                except BaseException:
                    store.lock.release()
                    raise

            def __exit__(self, et, ev, tb):
                try:
                    store.db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store.lock.release()
        return _Tx()

    # ---- 元信息 ----
    def get_meta(self, k: str, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", k)
        return json.loads(r["v"]) if r else default

    def set_meta(self, k: str, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               k, json.dumps(v, ensure_ascii=False))

    # ---- 玩家 ----
    def add_player(self, name: str, shop: str, gender: str = "") -> str:
        pid = "p_" + uuid.uuid4().hex[:8]
        self.x("INSERT INTO players(id,name,shop,gender,created,stage,stage_info) VALUES(?,?,?,?,?,1,'{}')",
               pid, name, shop, gender, time.time())
        return pid

    def player(self, pid: str) -> dict | None:
        r = self.one("SELECT * FROM players WHERE id=?", pid)
        if r:
            r["stage_info"] = json.loads(r.get("stage_info") or "{}")
        return r

    def players(self) -> list[dict]:
        return self.q("SELECT id,name,shop,gender,created,stage FROM players ORDER BY created")

    def set_stage(self, pid: str, stage: int, info: dict):
        self.x("UPDATE players SET stage=?, stage_info=? WHERE id=?", stage, json.dumps(info, ensure_ascii=False), pid)

    # ---- 关系 ----
    def bond(self, player: str, npc: str, start: dict | None = None) -> dict:
        r = self.one("SELECT * FROM bond WHERE player=? AND npc=?", player, npc)
        if not r:
            start = start or {}
            self.x("INSERT INTO bond(player,npc,trust,warmth) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   player, npc, int(start.get("trust", 0)), int(start.get("warmth", 0)))
            r = self.one("SELECT * FROM bond WHERE player=? AND npc=?", player, npc)
        r["facts"] = json.loads(r["facts"])
        return r

    def set_bond_values(self, player: str, npc: str, trust: int, warmth: int):
        """只改信任和亲近（每轮对话后）。"""
        self.x("UPDATE bond SET trust=?, warmth=? WHERE player=? AND npc=?", trust, warmth, player, npc)

    def set_bond_memory(self, player: str, npc: str, impression: str, facts: list, meetings: int):
        """只改看法、事实和见面次数（记忆整理后），不碰信任和亲近。"""
        self.x("UPDATE bond SET impression=?, facts=?, meetings=? WHERE player=? AND npc=?",
               impression, json.dumps(facts, ensure_ascii=False), meetings, player, npc)

    def save_bond(self, b: dict):
        self.x("UPDATE bond SET trust=?, warmth=?, impression=?, facts=?, meetings=? WHERE player=? AND npc=?",
               b["trust"], b["warmth"], b["impression"], json.dumps(b["facts"], ensure_ascii=False),
               b["meetings"], b["player"], b["npc"])

    def log_bond(self, player: str, npc: str, stage: int, trust: int, warmth: int):
        """记下这个阶段交情的起点（第一次记录时）、最高点和终点。"""
        r = self.one("SELECT * FROM bond_log WHERE player=? AND npc=? AND stage=?", player, npc, stage)
        if not r:
            self.x("INSERT INTO bond_log VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                   player, npc, stage, trust, warmth, trust, warmth, trust, warmth, trust + warmth)
            return
        self.x("""UPDATE bond_log SET last_trust=?, last_warmth=?, max_trust=?, max_warmth=?, max_sum=?
                  WHERE player=? AND npc=? AND stage=?""",
               trust, warmth, max(r["max_trust"], trust), max(r["max_warmth"], warmth),
               max(r["max_sum"], trust + warmth), player, npc, stage)

    # ---- 会话 ----
    def new_session(self, player: str, npc: str, stage: int, kind: str = "chat", spec: dict | None = None) -> str:
        sid = "s_" + uuid.uuid4().hex[:10]
        self.x("INSERT INTO sessions(id,player,npc,stage,kind,spec,started) VALUES(?,?,?,?,?,?,?)",
               sid, player, npc, stage, kind, json.dumps(spec or {}, ensure_ascii=False), time.time())
        return sid

    def session(self, sid: str) -> dict | None:
        r = self.one("SELECT * FROM sessions WHERE id=?", sid)
        if r:
            r["spec"] = json.loads(r.get("spec") or "{}")
        return r

    def add_message(self, sid: str, role: str, kind: str, text: str, meta: dict | None = None):
        self.x("INSERT INTO messages(session,role,kind,text,meta,created) VALUES(?,?,?,?,?,?)",
               sid, role, kind, text, json.dumps(meta or {}, ensure_ascii=False), time.time())

    def messages(self, sid: str) -> list[dict]:
        rows = self.q("SELECT * FROM messages WHERE session=? ORDER BY id", sid)
        for r in rows:
            r["meta"] = json.loads(r["meta"])
        return rows

    def log_call(self, sid: str | None, purpose: str, prompt, output: str, ms: int):
        self.x("INSERT INTO calls(session,purpose,prompt,output,ms,created) VALUES(?,?,?,?,?,?)",
               sid, purpose, json.dumps(prompt, ensure_ascii=False), output, ms, time.time())
        self._calls += 1
        if self._calls % 50 == 0:  # 完整提示词很占地方，只留最近 300 次调用
            self.x("DELETE FROM calls WHERE id < (SELECT MAX(id) FROM calls) - 300")


class PgStore(Store):
    """同样的接口，存在 PostgreSQL 里。设置环境变量 DATABASE_URL 即启用。"""

    def __init__(self, url: str):
        from .pgwire import Connection
        self._calls = 0
        self.url = url
        self.lock = threading.RLock()
        self._Connection = Connection
        self.conn = Connection(url)
        self._in_tx = False
        self._last = time.time()
        schema = (SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                  .replace(" REAL", " DOUBLE PRECISION"))
        with self.lock:
            self.conn.script(schema)

    def close(self):
        with self.lock:
            self.conn.close()

    def _reconnect(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = self._Connection(self.url)

    def _run(self, sql: str, args):
        # 连接空闲久了可能已经被断开：先探一下，断了就重连，免得写入失败
        if not self._in_tx and time.time() - self._last > 30:
            try:
                self.conn.execute("SELECT 1")
            except (OSError, ConnectionError):
                self._reconnect()
        try:
            rows = self.conn.execute(sql, args)
        except (OSError, ConnectionError):
            if self._in_tx:
                raise
            self._reconnect()
            # 只读语句可以放心重试；写入不重试（可能已经执行过了），让调用方看到错误
            if not sql.lstrip().upper().startswith("SELECT"):
                raise
            rows = self.conn.execute(sql, args)
        self._last = time.time()
        return rows

    def q(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return self._run(sql, args)

    def x(self, sql: str, *args):
        with self.lock:
            self._run(sql, args)

    def tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                try:
                    store._run("SELECT 1", ())     # 顺便确认连接还活着
                    store.conn.execute("BEGIN")
                except BaseException:
                    store.lock.release()
                    raise
                store._in_tx = True

            def __exit__(self, et, ev, tb):
                store._in_tx = False
                try:
                    store.conn.execute("ROLLBACK" if et else "COMMIT")
                except (OSError, ConnectionError):
                    store._reconnect()          # 连接断了：事务已经作废，换一条新连接
                    if not et:
                        raise
                finally:
                    store.lock.release()
        return _Tx()


def open_store(data_dir) -> Store:
    """有 DATABASE_URL 就用 PostgreSQL，否则用数据目录里的 SQLite 文件。"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return PgStore(url)
    return Store(Path(data_dir) / "world.sqlite3")
