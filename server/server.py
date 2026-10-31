#!/usr/bin/env python3
"""
PTI 二手机批签服务端（测试 / 内部版）

只用 Python 3 标准库，零第三方依赖，镜像就是 python:3.12-alpine，约 50MB 级别。
数据库是 SQLite 单文件，路径由环境变量 PTI_DB_PATH 指定，Docker 里挂载为 /data/pti.db；
转移服务 = 停容器、拷走宿主机 ./data 文件夹、在新机器上再 up，数据库跟着文件夹走，不进镜像。

鉴权（App 与服务端通信的安全落点）：
- 登录：手机号 + 密码，密码用 PBKDF2-HMAC-SHA256 + 每人随机盐存哈希，不存明文。
- 登录成功发 Token（随机 64 hex，有效期 7 天，存 tokens 表），之后所有业务接口必须带
  Authorization: Bearer <token>，无 Token、Token 过期、账号已停用，一律 401。
- 角色在服务端逐接口校验：前端隐藏按钮不算权限。管理类接口只允许管理员。
- 首个管理员不写死密码：只在数据库为空且环境变量 PTI_ADMIN_PHONE / PTI_ADMIN_PASSWORD
  都给了时才创建；没给就不创建、不留默认账号。密码只放宿主机 .env，不进镜像、不进代码。

规则裁决也在服务端：申请与发货核验各做一次，App 端只做展示与输入，不做最终裁决。
测试版规则只有一条：已过保（end_date < 今天）直接拦截，不生成单号。
"""
import hashlib
import json
import os
import re
import shutil
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("PTI_DB_PATH", "/data/pti.db")
PORT = int(os.environ.get("PORT", "8000"))
TOKEN_DAYS = 7
MAX_BACKUPS = 5
BACKUP_HOUR = int(os.environ.get("PTI_BACKUP_HOUR", "2"))  # 每天备份时间，默认凌晨 2 点
DB_LOCK = threading.RLock()  # 可重入：手动备份/恢复前备份在接口持锁时还会再进 do_backup

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '启用',
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    contact TEXT DEFAULT '',
    remark TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_no TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    customer TEXT NOT NULL DEFAULT '',
    power_box_sn TEXT DEFAULT '',
    board_sn TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    last_sign_date TEXT DEFAULT '',
    asset_photo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT UNIQUE NOT NULL,
    machine_no TEXT NOT NULL,
    machine_name TEXT DEFAULT '',
    applicant_name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL,
    rule_result TEXT DEFAULT '',
    checker_name TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""



def backup_dir():
    d = os.path.join(os.path.dirname(DB_PATH) or ".", "backups")
    os.makedirs(d, exist_ok=True)
    return d


def backup_filename():
    base = "pti-" + datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S") + ".db"
    path = os.path.join(backup_dir(), base)
    if not os.path.exists(path):
        return base
    i = 1
    while True:
        name = base[:-3] + "-%d.db" % i
        if not os.path.exists(os.path.join(backup_dir(), name)):
            return name
        i += 1


def list_backups():
    out = []
    d = backup_dir()
    for name in sorted(os.listdir(d)):
        if not re.match(r"^pti-\d{8}-\d{6}(-\d+)?\.db$", name):
            continue
        fp = os.path.join(d, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append({"name": name, "size": st.st_size, "created_at": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat()})
    out.sort(key=lambda x: x["created_at"])
    return out


def prune_backups():
    items = list_backups()
    for old in items[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(backup_dir(), old["name"]))
        except OSError:
            pass


def do_backup(reason="daily"):
    """用 SQLite backup API 做一致性备份（不是直接拷正在写的文件），然后按 5 份循环清理。"""
    name = backup_filename()
    dest = os.path.join(backup_dir(), name)
    with DB_LOCK:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        prune_backups()
    return name


def backup_scheduler():
    """每天备份线程：每分钟看一次，到了备份小时且今天还没备份就备份一次。"""
    last_date = None
    while True:
        try:
            now = datetime.now().astimezone()
            if now.hour >= BACKUP_HOUR and last_date != now.date():
                # 今天已有备份就不重复（比如重启后）
                today_prefix = "pti-" + now.strftime("%Y%m%d") + "-"
                if not any(b["name"].startswith(today_prefix) for b in list_backups()):
                    name = do_backup("daily")
                    print(f"[pti] daily backup: {name}", flush=True)
                last_date = now.date()
        except Exception as e:
            print(f"[pti] backup scheduler error: {e}", flush=True)
        time.sleep(60)


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with DB_LOCK:
        conn = db()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            phone = os.environ.get("PTI_ADMIN_PHONE", "").strip()
            password = os.environ.get("PTI_ADMIN_PASSWORD", "").strip()
            name = os.environ.get("PTI_ADMIN_NAME", "管理员").strip() or "管理员"
            if n == 0 and phone and password:
                salt = secrets.token_hex(16)
                h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
                conn.execute(
                    "INSERT INTO users (phone, name, role, status, salt, password_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                    (phone, name, "管理员", "启用", salt, h, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                print(f"[pti] first admin created: {name} ({phone})", flush=True)
            elif n == 0:
                print("[pti] no users and no PTI_ADMIN_PHONE/PASSWORD set: no account created (no default password)", flush=True)
        finally:
            conn.close()


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()


def audit(conn, actor, action, detail):
    conn.execute(
        "INSERT INTO audit_logs (created_at, actor_name, action, detail) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), actor, action, detail),
    )


def today():
    return date.today().isoformat()


def next_order_no(conn):
    prefix = "PTI-" + today().replace("-", "")
    row = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_no LIKE ?", (prefix + "%",)
    ).fetchone()
    return "%s-%03d" % (prefix, (row[0] if row else 0) + 1)


def row_to_dict(row):
    return dict(row) if row is not None else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def auth_user(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        with DB_LOCK:
            conn = db()
            try:
                row = conn.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
                if row is None or row["expires_at"] < datetime.now(timezone.utc).isoformat():
                    return None
                user = conn.execute("SELECT * FROM users WHERE phone = ?", (row["phone"],)).fetchone()
                if user is None or user["status"] != "启用":
                    return None
                return row_to_dict(user)
            finally:
                conn.close()

    def require_auth(self, roles=None):
        user = self.auth_user()
        if user is None:
            self.send_json(401, {"error": "unauthorized: need login token"})
            return None
        if roles and user["role"] not in roles:
            self.send_json(403, {"error": "forbidden: role not allowed"})
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/health":
            self.send_json(200, {"ok": True, "service": "pti-batch-sign"})
            return
        user = self.require_auth()
        if user is None:
            return
        with DB_LOCK:
            conn = db()
            try:
                if path == "/api/me":
                    self.send_json(200, {"phone": user["phone"], "name": user["name"], "role": user["role"]})
                elif path == "/api/machines":
                    q = (qs.get("q", [""])[0] or "").strip()
                    customer = (qs.get("customer", [""])[0] or "").strip()
                    sql = "SELECT * FROM machines WHERE 1=1"
                    args = []
                    if q:
                        sql += " AND (machine_no LIKE ? OR display_name LIKE ? OR power_box_sn LIKE ? OR board_sn LIKE ?)"
                        args += ["%" + q + "%"] * 4
                    if customer:
                        sql += " AND customer = ?"
                        args.append(customer)
                    sql += " ORDER BY machine_no"
                    rows = conn.execute(sql, args).fetchall()
                    self.send_json(200, {"machines": [row_to_dict(r) for r in rows]})
                elif path.startswith("/api/machines/"):
                    no = path.rsplit("/", 1)[-1]
                    row = conn.execute("SELECT * FROM machines WHERE machine_no = ?", (no,)).fetchone()
                    if row is None:
                        self.send_json(404, {"error": "machine not found"})
                    else:
                        orders = conn.execute(
                            "SELECT * FROM orders WHERE machine_no = ? ORDER BY applied_at DESC", (no,)
                        ).fetchall()
                        d = row_to_dict(row)
                        d["orders"] = [row_to_dict(o) for o in orders]
                        self.send_json(200, d)
                elif path == "/api/orders":
                    machine_no = (qs.get("machine_no", [""])[0] or "").strip()
                    sql = "SELECT * FROM orders WHERE 1=1"
                    args = []
                    if machine_no:
                        sql += " AND machine_no = ?"
                        args.append(machine_no)
                    sql += " ORDER BY applied_at DESC, id DESC"
                    rows = conn.execute(sql, args).fetchall()
                    self.send_json(200, {"orders": [row_to_dict(r) for r in rows]})
                elif path == "/api/alerts":
                    rows = conn.execute("SELECT * FROM machines ORDER BY machine_no").fetchall()
                    alerts = []
                    for r in rows:
                        d = row_to_dict(r)
                        expired = bool(d["end_date"]) and d["end_date"] < today()
                        idle = None
                        if d["last_sign_date"]:
                            try:
                                idle = (date.today() - date.fromisoformat(d["last_sign_date"])).days
                            except ValueError:
                                idle = None
                        if expired or (idle is not None and idle >= 180):
                            d["expired"] = expired
                            d["idle_days"] = idle
                            alerts.append(d)
                    self.send_json(200, {"alerts": alerts})
                elif path == "/api/admin/backups":
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    items = list_backups()
                    self.send_json(200, {
                        "backups": items,
                        "count": len(items),
                        "max": MAX_BACKUPS,
                        "backup_hour": BACKUP_HOUR,
                        "next_daily": "每天 %02d:00 自动备份，超 %d 份删最旧的" % (BACKUP_HOUR, MAX_BACKUPS),
                    })
                elif path == "/api/audit":
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200").fetchall()
                    self.send_json(200, {"audit": [row_to_dict(r) for r in rows]})
                else:
                    self.send_json(404, {"error": "not found"})
            finally:
                conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_json()
        if path == "/api/login":
            phone = (body.get("phone") or "").strip()
            password = body.get("password") or ""
            with DB_LOCK:
                conn = db()
                try:
                    user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
                    if user is None or user["status"] != "启用" or hash_pw(password, user["salt"]) != user["password_hash"]:
                        self.send_json(401, {"error": "phone or password wrong"})
                        return
                    token = secrets.token_hex(32)
                    exp = (datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)).isoformat()
                    conn.execute("INSERT INTO tokens (token, phone, expires_at) VALUES (?,?,?)", (token, phone, exp))
                    conn.commit()
                    self.send_json(200, {"token": token, "name": user["name"], "role": user["role"], "expires_at": exp})
                finally:
                    conn.close()
            return
        user = self.require_auth()
        if user is None:
            return
        with DB_LOCK:
            conn = db()
            try:
                if path == "/api/orders":
                    # 申请批签：服务端规则裁决，已过保直接拦截
                    machine_no = (body.get("machine_no") or "").strip()
                    m = conn.execute("SELECT * FROM machines WHERE machine_no = ?", (machine_no,)).fetchone()
                    if m is None:
                        self.send_json(404, {"error": "machine not found"})
                        return
                    md = row_to_dict(m)
                    if md["end_date"] and md["end_date"] < today():
                        audit(conn, user["name"], "规则拦截", f"{machine_no} 已过保（到期 {md['end_date']}），申请被拦截")
                        conn.commit()
                        self.send_json(403, {"error": "blocked: expired", "rule_result": f"已过保：保修到期日 {md['end_date']}"})
                        return
                    no = next_order_no(conn)
                    conn.execute(
                        "INSERT INTO orders (order_no, machine_no, machine_name, applicant_name, applied_at, status, rule_result) VALUES (?,?,?,?,?,?,?)",
                        (no, machine_no, md["display_name"], user["name"], today(), "已通过", "通过"),
                    )
                    audit(conn, user["name"], "申请提交", f"{machine_no} 单号 {no} 生成")
                    conn.commit()
                    self.send_json(200, {"order_no": no, "status": "已通过"})
                elif path == "/api/shipment/verify":
                    order_no = (body.get("order_no") or "").strip()
                    machine_no = (body.get("machine_no") or "").strip()
                    o = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
                    if o is None:
                        self.send_json(403, {"error": "blocked: order not found"})
                        return
                    od = row_to_dict(o)
                    if od["machine_no"] != machine_no:
                        self.send_json(403, {"error": "blocked: order does not match machine"})
                        return
                    if od["status"] != "已通过":
                        self.send_json(403, {"error": f"blocked: order status is {od['status']}"})
                        return
                    conn.execute(
                        "UPDATE orders SET status = '已发货', checker_name = ? WHERE order_no = ?",
                        (user["name"], order_no),
                    )
                    audit(conn, user["name"], "发货核验", f"{machine_no} 单号 {order_no} 核验通过，已发货")
                    conn.commit()
                    self.send_json(200, {"ok": True, "order_no": order_no, "status": "已发货"})
                elif path == "/api/admin/users":
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    phone = (body.get("phone") or "").strip()
                    name = (body.get("name") or "").strip()
                    role = (body.get("role") or "申请人").strip()
                    password = body.get("password") or ""
                    if not phone or not name or not password:
                        self.send_json(400, {"error": "phone, name, password required"})
                        return
                    salt = secrets.token_hex(16)
                    conn.execute(
                        "INSERT INTO users (phone, name, role, status, salt, password_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                        (phone, name, role, "启用", salt, hash_pw(password, salt), datetime.now(timezone.utc).isoformat()),
                    )
                    audit(conn, user["name"], "账号创建", f"{name}（{phone}）角色 {role}")
                    conn.commit()
                    self.send_json(200, {"ok": True, "phone": phone, "name": name, "role": role})
                elif path == "/api/admin/seed":
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    machines = body.get("machines") or []
                    count = 0
                    for m in machines:
                        conn.execute(
                            "INSERT OR REPLACE INTO machines (machine_no, display_name, customer, power_box_sn, board_sn, start_date, end_date, last_sign_date, asset_photo) VALUES (?,?,?,?,?,?,?,?,?)",
                            (m.get("machine_no"), m.get("display_name", ""), m.get("customer", ""), m.get("power_box_sn", ""), m.get("board_sn", ""), m.get("start_date", ""), m.get("end_date", ""), m.get("last_sign_date", ""), m.get("asset_photo", "")),
                        )
                        count += 1
                    audit(conn, user["name"], "模拟数据导入", f"导入机器 {count} 台（测试用）")
                    conn.commit()
                    self.send_json(200, {"ok": True, "machines": count})
                elif path == "/api/admin/backups":
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    name = do_backup("manual")
                    audit(conn, user["name"], "手动备份", f"生成备份 {name}")
                    conn.commit()
                    self.send_json(200, {"ok": True, "name": name, "backups": list_backups()})
                elif path.startswith("/api/admin/backups/") and path.endswith("/restore"):
                    if user["role"] != "管理员":
                        self.send_json(403, {"error": "forbidden: admin only"})
                        return
                    name = path.split("/")[-2]
                    if not re.match(r"^pti-\d{8}-\d{6}(-\d+)?\.db$", name):
                        self.send_json(400, {"error": "bad backup name"})
                        return
                    src = os.path.join(backup_dir(), name)
                    if not os.path.exists(src):
                        self.send_json(404, {"error": "backup not found"})
                        return
                    # 先给当前库做一份恢复前备份，再覆盖；恢复后当前 Token 可能失效（回到备份时的 Token），需重新登录
                    pre = do_backup("pre-restore")
                    with DB_LOCK:
                        shutil.copyfile(src, DB_PATH)
                    self.send_json(200, {"ok": True, "restored": name, "pre_restore_backup": pre, "note": "恢复后请重新登录，Token 以备份时为准"})
                else:
                    self.send_json(404, {"error": "not found"})
            finally:
                conn.close()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        user = self.require_auth(roles=["管理员"])
        if user is None:
            return
        if path.startswith("/api/admin/backups/"):
            name = path.rsplit("/", 1)[-1]
            if not re.match(r"^pti-\d{8}-\d{6}(-\d+)?\.db$", name):
                self.send_json(400, {"error": "bad backup name"})
                return
            fp = os.path.join(backup_dir(), name)
            if not os.path.exists(fp):
                self.send_json(404, {"error": "backup not found"})
                return
            try:
                os.remove(fp)
            except OSError as e:
                self.send_json(500, {"error": str(e)})
                return
            self.send_json(200, {"ok": True, "deleted": name})
        else:
            self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=backup_scheduler, daemon=True).start()
    print(f"[pti] listening on 0.0.0.0:{PORT}, db={DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
