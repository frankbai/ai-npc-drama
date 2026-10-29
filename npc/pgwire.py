"""极简 PostgreSQL 客户端，只用 Python 标准库。

为什么不用 psycopg：Demo 希望零依赖，本机和线上都能直接 python3 run.py。
只实现运行时需要的部分：连接（可选 SSL）、SCRAM-SHA-256 / MD5 / 明文认证、
带参数的查询（扩展协议，参数一律按文本传，由服务器推断类型）、多语句脚本。
正式游戏请换成成熟的驱动和连接池。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
from urllib.parse import parse_qs, unquote, urlparse


class PgError(Exception):
    def __init__(self, fields: dict):
        self.fields = fields
        super().__init__(f"{fields.get('S', 'ERROR')} {fields.get('C', '')}: {fields.get('M', '')}")


_INT = {20, 21, 23, 26}
_FLOAT = {700, 701, 1700}


def _conv(oid: int, raw: bytes | None):
    if raw is None:
        return None
    s = raw.decode("utf-8")
    if oid in _INT:
        return int(s)
    if oid in _FLOAT:
        return float(s)
    if oid == 16:
        return s == "t"
    return s


def qmark_to_dollar(sql: str) -> str:
    """把 ? 占位符换成 $1 $2 …（跳过引号里的内容）。"""
    out, n, quote = [], 0, None
    for ch in sql:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


class Connection:
    def __init__(self, url: str, timeout: float = 15.0):
        u = urlparse(url)
        if u.scheme not in ("postgres", "postgresql"):
            raise ValueError("DATABASE_URL 必须以 postgres:// 或 postgresql:// 开头")
        self.host = u.hostname or "localhost"
        self.port = u.port or 5432
        self.user = unquote(u.username or "")
        self.password = unquote(u.password or "")
        self.database = (u.path or "/").lstrip("/") or self.user
        self.sslmode = parse_qs(u.query).get("sslmode", ["prefer"])[0]
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self.connect()

    # ---- 底层收发 ----
    def _send(self, data: bytes):
        self.sock.sendall(data)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("数据库连接被关闭")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read(self) -> tuple[bytes, bytes]:
        head = self._recv_exact(5)
        kind, length = head[:1], struct.unpack("!I", head[1:])[0]
        return kind, self._recv_exact(length - 4)

    @staticmethod
    def _msg(kind: bytes, payload: bytes) -> bytes:
        return kind + struct.pack("!I", len(payload) + 4) + payload

    @staticmethod
    def _cstr(s: str) -> bytes:
        return s.encode("utf-8") + b"\0"

    @staticmethod
    def _fields(payload: bytes) -> dict:
        out = {}
        for part in payload.split(b"\0"):
            if part:
                out[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return out

    # ---- 连接与认证 ----
    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock, self._buf = raw, b""
        if self.sslmode != "disable":
            raw.sendall(struct.pack("!II", 8, 80877103))
            answer = raw.recv(1)
            if answer == b"S":
                ctx = ssl.create_default_context()
                if self.sslmode not in ("verify-ca", "verify-full"):
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
            elif self.sslmode in ("require", "verify-ca", "verify-full"):
                raise ConnectionError("数据库不支持 SSL，但连接串要求 SSL")
        params = b"".join(self._cstr(k) + self._cstr(v) for k, v in [
            ("user", self.user), ("database", self.database), ("client_encoding", "UTF8"),
            ("application_name", "npc-demo")]) + b"\0"
        payload = struct.pack("!I", 196608) + params
        self._send(struct.pack("!I", len(payload) + 4) + payload)
        self._auth()

    def _auth(self):
        scram = None
        while True:
            kind, p = self._read()
            if kind == b"E":
                raise PgError(self._fields(p))
            if kind == b"R":
                code = struct.unpack("!I", p[:4])[0]
                if code == 0:
                    continue
                if code == 3:
                    self._send(self._msg(b"p", self._cstr(self.password)))
                elif code == 5:
                    salt = p[4:8]
                    inner = hashlib.md5((self.password + self.user).encode()).hexdigest()
                    outer = "md5" + hashlib.md5(inner.encode() + salt).hexdigest()
                    self._send(self._msg(b"p", self._cstr(outer)))
                elif code == 10:
                    mechs = [m.decode() for m in p[4:].split(b"\0") if m]
                    if "SCRAM-SHA-256" not in mechs:
                        raise ConnectionError(f"不支持的认证方式：{mechs}")
                    scram = _Scram(self.password)
                    first = scram.client_first().encode()
                    self._send(self._msg(b"p", self._cstr("SCRAM-SHA-256") + struct.pack("!I", len(first)) + first))
                elif code == 11:
                    self._send(self._msg(b"p", scram.client_final(p[4:].decode()).encode()))
                elif code == 12:
                    scram.verify(p[4:].decode())
                else:
                    raise ConnectionError(f"不支持的认证方式：{code}")
            elif kind == b"Z":
                return
            # S（参数状态）、K（后端密钥）、N（通知）忽略

    # ---- 查询 ----
    def execute(self, sql: str, params=()) -> list[dict]:
        sql = qmark_to_dollar(sql)
        vals = []
        for v in params:
            if v is None:
                vals.append(struct.pack("!i", -1))
            else:
                if isinstance(v, bool):
                    v = "true" if v else "false"
                b = str(v).encode("utf-8")
                vals.append(struct.pack("!i", len(b)) + b)
        bind = (b"\0\0" + struct.pack("!H", 0) + struct.pack("!H", len(vals)) + b"".join(vals) + struct.pack("!H", 0))
        self._send(
            self._msg(b"P", b"\0" + self._cstr(sql) + struct.pack("!H", 0))
            + self._msg(b"B", bind)
            + self._msg(b"D", b"P\0")
            + self._msg(b"E", b"\0" + struct.pack("!I", 0))
            + self._msg(b"S", b""))
        return self._collect()

    def script(self, sql: str):
        self._send(self._msg(b"Q", self._cstr(sql)))
        self._collect()

    def _collect(self) -> list[dict]:
        cols, rows, err = [], [], None
        while True:
            kind, p = self._read()
            if kind == b"T":
                n = struct.unpack("!H", p[:2])[0]
                pos, cols = 2, []
                for _ in range(n):
                    end = p.index(b"\0", pos)
                    name = p[pos:end].decode()
                    pos = end + 1
                    oid = struct.unpack("!I", p[pos + 6:pos + 10])[0]
                    pos += 18
                    cols.append((name, oid))
            elif kind == b"D":
                n = struct.unpack("!H", p[:2])[0]
                pos, row = 2, {}
                for i in range(n):
                    ln = struct.unpack("!i", p[pos:pos + 4])[0]
                    pos += 4
                    raw = None if ln < 0 else p[pos:pos + ln]
                    pos += max(ln, 0)
                    row[cols[i][0]] = _conv(cols[i][1], raw)
                rows.append(row)
            elif kind == b"E":
                err = PgError(self._fields(p))
            elif kind == b"Z":
                if err:
                    raise err
                return rows
            # 1 2 n C I N S 等消息不需要处理

    def close(self):
        try:
            self._send(self._msg(b"X", b""))
            self.sock.close()
        except OSError:
            pass


class _Scram:
    def __init__(self, password: str):
        self.password = password.encode("utf-8")
        self.nonce = base64.b64encode(os.urandom(18)).decode()
        self.first_bare = f"n=,r={self.nonce}"
        self.server_sig = None

    def client_first(self) -> str:
        return "n,," + self.first_bare

    def client_final(self, server_first: str) -> str:
        attrs = dict(kv.split("=", 1) for kv in server_first.split(","))
        nonce, salt, it = attrs["r"], base64.b64decode(attrs["s"]), int(attrs["i"])
        if not nonce.startswith(self.nonce):
            raise ConnectionError("SCRAM 校验失败")
        salted = hashlib.pbkdf2_hmac("sha256", self.password, salt, it)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored = hashlib.sha256(client_key).digest()
        without_proof = f"c=biws,r={nonce}"
        auth_msg = f"{self.first_bare},{server_first},{without_proof}".encode()
        sig = hmac.new(stored, auth_msg, hashlib.sha256).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, sig))
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        self.server_sig = base64.b64encode(hmac.new(server_key, auth_msg, hashlib.sha256).digest()).decode()
        return f"{without_proof},p={base64.b64encode(proof).decode()}"

    def verify(self, server_final: str):
        attrs = dict(kv.split("=", 1) for kv in server_final.split(","))
        if attrs.get("v") != self.server_sig:
            raise ConnectionError("数据库服务器的 SCRAM 签名不对")
