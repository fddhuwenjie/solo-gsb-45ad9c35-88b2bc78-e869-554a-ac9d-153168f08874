"""陶瓷合窑装载确认台 — Flask + SQLite 后端。

核心规则：
- 批次登记：批次编号 + 允许峰温闭区间 + 保温分钟闭区间，每次修改 version + 1。
- 每台窑有固定编号窑位（1..N），装窑草案中一批占一位。
- 预览返回共同可用的峰温/保温区间、占位情况，以及草案版本与各批次版本。
- 确认必须携带预览时的草案版本与批次版本；期间草案（窑位）或批次工艺范围
  被改动则拒绝并提示刷新（409 STALE_DRAFT / STALE_BATCH）。
- 确认时在单个事务内核对：窑位合法且不重复、两项区间均有交集、
  批次未被其他已确认窑次使用；任一失败则整体回滚，草案与已确认记录不变。
- 成功则一次性写入不可变的装载清单（批次数据按当时快照保存）及共同工艺区间。
"""
import os
import sqlite3
import threading

from flask import Flask, current_app, g, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "kiln.db")

# 串行化确认流程；配合 BEGIN IMMEDIATE，保证两个草案争用同一批次时只有一个能提交。
_confirm_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no   TEXT    NOT NULL UNIQUE,
    peak_min   REAL    NOT NULL,
    peak_max   REAL    NOT NULL,
    soak_min   INTEGER NOT NULL,
    soak_max   INTEGER NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS kilns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT    NOT NULL UNIQUE,
    position_count INTEGER NOT NULL CHECK (position_count > 0)
);
CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kiln_id    INTEGER NOT NULL UNIQUE REFERENCES kilns(id),
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS draft_items (
    draft_id    INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    position_no INTEGER NOT NULL,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    PRIMARY KEY (draft_id, position_no),
    UNIQUE (draft_id, batch_id)
);
CREATE TABLE IF NOT EXISTS loads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_no     TEXT    NOT NULL UNIQUE,
    kiln_id    INTEGER NOT NULL REFERENCES kilns(id),
    kiln_code  TEXT    NOT NULL,
    peak_min   REAL    NOT NULL,
    peak_max   REAL    NOT NULL,
    soak_min   INTEGER NOT NULL,
    soak_max   INTEGER NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS load_items (
    load_id     INTEGER NOT NULL REFERENCES loads(id),
    position_no INTEGER NOT NULL,
    batch_id    INTEGER NOT NULL,
    batch_no    TEXT    NOT NULL,
    peak_min    REAL    NOT NULL,
    peak_max    REAL    NOT NULL,
    soak_min    INTEGER NOT NULL,
    soak_max    INTEGER NOT NULL,
    PRIMARY KEY (load_id, position_no),
    UNIQUE (load_id, batch_id)
);
"""

SEED_KILNS = [("K01", 8), ("K02", 6)]


def get_db():
    if "db" not in g:
        path = current_app.config["DB_PATH"]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


def err(status, code, message, **details):
    """统一错误响应：message 指出冲突批次或窑位，details 携带结构化信息。"""
    return jsonify({"error": message, "code": code, "details": details}), status


def parse_range_payload(data):
    batch_no = str(data.get("batch_no") or "").strip()
    if not batch_no:
        raise ValueError("批次编号不能为空")
    try:
        peak_min = float(data.get("peak_min"))
        peak_max = float(data.get("peak_max"))
        soak_min = int(data.get("soak_min"))
        soak_max = int(data.get("soak_max"))
    except (TypeError, ValueError):
        raise ValueError("峰温与保温分钟必须为数字")
    if peak_min > peak_max:
        raise ValueError(f"峰温区间无效：下限 {peak_min}℃ 大于上限 {peak_max}℃")
    if soak_min > soak_max:
        raise ValueError(f"保温区间无效：下限 {soak_min} 分钟大于上限 {soak_max} 分钟")
    if soak_min < 0:
        raise ValueError("保温分钟不能为负数")
    return batch_no, peak_min, peak_max, soak_min, soak_max


def batch_json(row, used_by=None):
    return {
        "id": row["id"],
        "batch_no": row["batch_no"],
        "peak_min": row["peak_min"],
        "peak_max": row["peak_max"],
        "soak_min": row["soak_min"],
        "soak_max": row["soak_max"],
        "version": row["version"],
        "used_by": used_by,
    }


def draft_json(db, draft):
    items = db.execute(
        """SELECT di.position_no, b.id AS batch_id, b.batch_no, b.version,
                  b.peak_min, b.peak_max, b.soak_min, b.soak_max
           FROM draft_items di JOIN batches b ON b.id = di.batch_id
           WHERE di.draft_id = ? ORDER BY di.position_no""",
        (draft["id"],),
    ).fetchall()
    return {
        "id": draft["id"],
        "kiln_id": draft["kiln_id"],
        "version": draft["version"],
        "items": [dict(it) for it in items],
    }


def bump_draft(db, draft_id):
    db.execute(
        "UPDATE drafts SET version = version + 1, updated_at = datetime('now','localtime') WHERE id = ?",
        (draft_id,),
    )


def used_batch_map(db, batch_ids):
    """批次 -> 已使用它的窑次 run_no。"""
    if not batch_ids:
        return {}
    marks = ",".join("?" for _ in batch_ids)
    rows = db.execute(
        f"""SELECT li.batch_id, l.run_no FROM load_items li
            JOIN loads l ON l.id = li.load_id WHERE li.batch_id IN ({marks})""",
        list(batch_ids),
    ).fetchall()
    return {r["batch_id"]: r["run_no"] for r in rows}


def build_preview(db, draft):
    """计算预览：共同峰温/保温区间、占位情况、问题列表。不修改任何数据。"""
    kiln = db.execute("SELECT * FROM kilns WHERE id = ?", (draft["kiln_id"],)).fetchone()
    items = db.execute(
        """SELECT di.position_no, b.id AS batch_id, b.batch_no, b.version,
                  b.peak_min, b.peak_max, b.soak_min, b.soak_max
           FROM draft_items di JOIN batches b ON b.id = di.batch_id
           WHERE di.draft_id = ? ORDER BY di.position_no""",
        (draft["id"],),
    ).fetchall()

    problems = []
    common_peak = None
    common_soak = None

    if not items:
        problems.append("草案为空：请先将批次放入窑位")
    else:
        p_lo = max(it["peak_min"] for it in items)
        p_hi = min(it["peak_max"] for it in items)
        if p_lo <= p_hi:
            common_peak = [p_lo, p_hi]
        else:
            hi = max(items, key=lambda it: it["peak_min"])
            lo = min(items, key=lambda it: it["peak_max"])
            problems.append(
                f"峰温区间无交集：批次 {hi['batch_no']} 要求 ≥{p_lo}℃，"
                f"批次 {lo['batch_no']} 仅允许 ≤{p_hi}℃"
            )
        s_lo = max(it["soak_min"] for it in items)
        s_hi = min(it["soak_max"] for it in items)
        if s_lo <= s_hi:
            common_soak = [s_lo, s_hi]
        else:
            hi = max(items, key=lambda it: it["soak_min"])
            lo = min(items, key=lambda it: it["soak_max"])
            problems.append(
                f"保温区间无交集：批次 {hi['batch_no']} 要求 ≥{s_lo} 分钟，"
                f"批次 {lo['batch_no']} 仅允许 ≤{s_hi} 分钟"
            )
        used = used_batch_map(db, [it["batch_id"] for it in items])
        for it in items:
            if it["batch_id"] in used:
                problems.append(f"批次 {it['batch_no']} 已被窑次 {used[it['batch_id']]} 确认使用")

    occupancy = []
    by_pos = {it["position_no"]: it for it in items}
    for n in range(1, kiln["position_count"] + 1):
        it = by_pos.get(n)
        occupancy.append({"position_no": n, "batch_no": it["batch_no"] if it else None})

    return {
        "draft_id": draft["id"],
        "draft_version": draft["version"],
        "kiln": {"id": kiln["id"], "code": kiln["code"], "position_count": kiln["position_count"]},
        "items": [dict(it) for it in items],
        "batch_versions": {str(it["batch_id"]): it["version"] for it in items},
        "occupancy": occupancy,
        "common_peak": common_peak,
        "common_soak": common_soak,
        "ok": not problems,
        "problems": problems,
    }


def load_json(db, load_row):
    items = db.execute(
        "SELECT * FROM load_items WHERE load_id = ? ORDER BY position_no",
        (load_row["id"],),
    ).fetchall()
    return {
        "id": load_row["id"],
        "run_no": load_row["run_no"],
        "kiln_id": load_row["kiln_id"],
        "kiln_code": load_row["kiln_code"],
        "peak_min": load_row["peak_min"],
        "peak_max": load_row["peak_max"],
        "soak_min": load_row["soak_min"],
        "soak_max": load_row["soak_max"],
        "created_at": load_row["created_at"],
        "items": [
            {
                "position_no": it["position_no"],
                "batch_id": it["batch_id"],
                "batch_no": it["batch_no"],
                "peak_min": it["peak_min"],
                "peak_max": it["peak_max"],
                "soak_min": it["soak_min"],
                "soak_max": it["soak_max"],
            }
            for it in items
        ],
    }


def create_app(db_path=None):
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["DB_PATH"] = db_path or os.environ.get("KILN_DB", DEFAULT_DB_PATH)

    db_parent = os.path.dirname(app.config["DB_PATH"])
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)
    with sqlite3.connect(app.config["DB_PATH"]) as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM kilns").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO kilns (code, position_count) VALUES (?, ?)", SEED_KILNS
            )
        conn.commit()

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ---------------- 页面 ----------------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    # ---------------- 批次 ----------------

    @app.get("/api/batches")
    def list_batches():
        db = get_db()
        rows = db.execute("SELECT * FROM batches ORDER BY id").fetchall()
        used = used_batch_map(db, [r["id"] for r in rows])
        return jsonify([batch_json(r, used.get(r["id"])) for r in rows])

    @app.post("/api/batches")
    def create_batch():
        data = request.get_json(silent=True) or {}
        try:
            batch_no, p_min, p_max, s_min, s_max = parse_range_payload(data)
        except ValueError as e:
            return err(400, "INVALID_RANGE", str(e))
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO batches (batch_no, peak_min, peak_max, soak_min, soak_max) VALUES (?,?,?,?,?)",
                (batch_no, p_min, p_max, s_min, s_max),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return err(409, "DUPLICATE_BATCH", f"批次编号 {batch_no} 已存在", batch_no=batch_no)
        row = db.execute("SELECT * FROM batches WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(batch_json(row)), 201

    @app.put("/api/batches/<int:batch_id>")
    def update_batch(batch_id):
        db = get_db()
        row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return err(404, "NOT_FOUND", f"批次 #{batch_id} 不存在")
        run_no = used_batch_map(db, [batch_id]).get(batch_id)
        if run_no:
            return err(
                409, "BATCH_USED",
                f"批次 {row['batch_no']} 已被窑次 {run_no} 确认使用，工艺范围不可再修改",
                batch_no=row["batch_no"], run_no=run_no,
            )
        data = request.get_json(silent=True) or {}
        try:
            batch_no, p_min, p_max, s_min, s_max = parse_range_payload(data)
        except ValueError as e:
            return err(400, "INVALID_RANGE", str(e))
        try:
            db.execute(
                """UPDATE batches SET batch_no=?, peak_min=?, peak_max=?, soak_min=?, soak_max=?,
                   version = version + 1 WHERE id = ?""",
                (batch_no, p_min, p_max, s_min, s_max, batch_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return err(409, "DUPLICATE_BATCH", f"批次编号 {batch_no} 已存在", batch_no=batch_no)
        row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return jsonify(batch_json(row))

    @app.delete("/api/batches/<int:batch_id>")
    def delete_batch(batch_id):
        db = get_db()
        row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return err(404, "NOT_FOUND", f"批次 #{batch_id} 不存在")
        run_no = used_batch_map(db, [batch_id]).get(batch_id)
        if run_no:
            return err(
                409, "BATCH_USED",
                f"批次 {row['batch_no']} 已被窑次 {run_no} 确认使用，不可删除",
                batch_no=row["batch_no"], run_no=run_no,
            )
        in_draft = db.execute(
            """SELECT d.id, k.code FROM draft_items di
               JOIN drafts d ON d.id = di.draft_id JOIN kilns k ON k.id = d.kiln_id
               WHERE di.batch_id = ?""",
            (batch_id,),
        ).fetchone()
        if in_draft:
            return err(
                409, "BATCH_IN_DRAFT",
                f"批次 {row['batch_no']} 正在窑 {in_draft['code']} 的草案中，请先从草案移除",
                batch_no=row["batch_no"],
            )
        db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        db.commit()
        return jsonify({"deleted": batch_id})

    # ---------------- 窑 ----------------

    @app.get("/api/kilns")
    def list_kilns():
        db = get_db()
        rows = db.execute(
            """SELECT k.*, d.id AS draft_id FROM kilns k
               LEFT JOIN drafts d ON d.kiln_id = k.id ORDER BY k.id"""
        ).fetchall()
        return jsonify(
            [
                {
                    "id": r["id"],
                    "code": r["code"],
                    "position_count": r["position_count"],
                    "draft_id": r["draft_id"],
                }
                for r in rows
            ]
        )

    @app.post("/api/kilns")
    def create_kiln():
        data = request.get_json(silent=True) or {}
        code = str(data.get("code") or "").strip()
        if not code:
            return err(400, "INVALID_KILN", "窑编号不能为空")
        try:
            count = int(data.get("position_count"))
        except (TypeError, ValueError):
            return err(400, "INVALID_KILN", "窑位数量必须为整数")
        if not 1 <= count <= 64:
            return err(400, "INVALID_KILN", "窑位数量须在 1..64 之间")
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO kilns (code, position_count) VALUES (?, ?)", (code, count)
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return err(409, "DUPLICATE_KILN", f"窑编号 {code} 已存在", code=code)
        row = db.execute("SELECT * FROM kilns WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify({"id": row["id"], "code": row["code"],
                        "position_count": row["position_count"], "draft_id": None}), 201

    # ---------------- 草案 ----------------

    @app.get("/api/kilns/<int:kiln_id>/draft")
    def get_or_create_draft(kiln_id):
        db = get_db()
        kiln = db.execute("SELECT * FROM kilns WHERE id = ?", (kiln_id,)).fetchone()
        if kiln is None:
            return err(404, "NOT_FOUND", f"窑 #{kiln_id} 不存在")
        draft = db.execute("SELECT * FROM drafts WHERE kiln_id = ?", (kiln_id,)).fetchone()
        if draft is None:
            try:
                db.execute("INSERT INTO drafts (kiln_id) VALUES (?)", (kiln_id,))
                db.commit()
            except sqlite3.IntegrityError:  # 并发下已被他人创建
                db.rollback()
            draft = db.execute("SELECT * FROM drafts WHERE kiln_id = ?", (kiln_id,)).fetchone()
        return jsonify(draft_json(db, draft))

    def _load_draft(db, draft_id):
        draft = db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if draft is None:
            return None, err(404, "NOT_FOUND", f"草案 #{draft_id} 不存在")
        kiln = db.execute("SELECT * FROM kilns WHERE id = ?", (draft["kiln_id"],)).fetchone()
        return (draft, kiln), None

    @app.put("/api/drafts/<int:draft_id>/items/<int:position_no>")
    def put_draft_item(draft_id, position_no):
        db = get_db()
        loaded, error = _load_draft(db, draft_id)
        if error:
            return error
        draft, kiln = loaded
        if not 1 <= position_no <= kiln["position_count"]:
            return err(
                400, "POSITION_INVALID",
                f"窑位 {position_no} 不存在：窑 {kiln['code']} 的窑位为 1..{kiln['position_count']}",
                position_no=position_no,
            )
        data = request.get_json(silent=True) or {}
        try:
            batch_id = int(data.get("batch_id"))
        except (TypeError, ValueError):
            return err(400, "INVALID_BATCH", "缺少有效的 batch_id")
        batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            return err(404, "NOT_FOUND", f"批次 #{batch_id} 不存在")
        run_no = used_batch_map(db, [batch_id]).get(batch_id)
        if run_no:
            return err(
                409, "BATCH_USED",
                f"批次 {batch['batch_no']} 已被窑次 {run_no} 确认使用，不能再装入草案",
                batch_no=batch["batch_no"], run_no=run_no,
            )
        # 同一批次在草案内只能占一个窑位：先移除旧占位再放入新窑位。
        db.execute("DELETE FROM draft_items WHERE draft_id = ? AND batch_id = ?",
                   (draft_id, batch_id))
        db.execute(
            "INSERT OR REPLACE INTO draft_items (draft_id, position_no, batch_id) VALUES (?,?,?)",
            (draft_id, position_no, batch_id),
        )
        bump_draft(db, draft_id)
        db.commit()
        draft = db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return jsonify(draft_json(db, draft))

    @app.delete("/api/drafts/<int:draft_id>/items/<int:position_no>")
    def delete_draft_item(draft_id, position_no):
        db = get_db()
        loaded, error = _load_draft(db, draft_id)
        if error:
            return error
        draft, _kiln = loaded
        db.execute("DELETE FROM draft_items WHERE draft_id = ? AND position_no = ?",
                   (draft_id, position_no))
        bump_draft(db, draft_id)
        db.commit()
        draft = db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return jsonify(draft_json(db, draft))

    @app.post("/api/drafts/<int:draft_id>/preview")
    def preview_draft(draft_id):
        db = get_db()
        loaded, error = _load_draft(db, draft_id)
        if error:
            return error
        draft, _kiln = loaded
        return jsonify(build_preview(db, draft))

    # ---------------- 确认 ----------------

    @app.post("/api/drafts/<int:draft_id>/confirm")
    def confirm_draft(draft_id):
        payload = request.get_json(silent=True) or {}
        client_draft_version = payload.get("draft_version")
        try:
            client_batch_versions = {
                int(k): int(v) for k, v in (payload.get("batch_versions") or {}).items()
            }
        except (TypeError, ValueError):
            return err(400, "INVALID_VERSIONS", "batch_versions 格式无效")

        # 全局锁 + BEGIN IMMEDIATE：两个草案争用同一批次时，只有一个能提交成功。
        with _confirm_lock:
            db = get_db()
            db.execute("BEGIN IMMEDIATE")
            try:
                draft = db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
                if draft is None:
                    db.rollback()
                    return err(404, "NOT_FOUND", f"草案 #{draft_id} 不存在")
                kiln = db.execute(
                    "SELECT * FROM kilns WHERE id = ?", (draft["kiln_id"],)
                ).fetchone()

                # 1) 草案版本：预览之后有人改了窑位占用则拒绝。
                if client_draft_version != draft["version"]:
                    db.rollback()
                    return err(
                        409, "STALE_DRAFT",
                        f"草案已被修改（当前版本 v{draft['version']}，预览版本 "
                        f"v{client_draft_version}）：窑位占用在你预览后发生变化，请刷新预览后再确认",
                        current_version=draft["version"],
                    )

                items = db.execute(
                    """SELECT di.position_no, b.id AS batch_id, b.batch_no, b.version,
                              b.peak_min, b.peak_max, b.soak_min, b.soak_max
                       FROM draft_items di JOIN batches b ON b.id = di.batch_id
                       WHERE di.draft_id = ? ORDER BY di.position_no""",
                    (draft_id,),
                ).fetchall()
                if not items:
                    db.rollback()
                    return err(400, "EMPTY_DRAFT", "草案为空：请先将批次放入窑位")

                # 2) 批次版本：预览之后有人改了工艺范围则拒绝。
                for it in items:
                    seen = client_batch_versions.get(it["batch_id"])
                    if seen is None or seen != it["version"]:
                        db.rollback()
                        return err(
                            409, "STALE_BATCH",
                            f"批次 {it['batch_no']} 的工艺范围在你预览后已被修改"
                            f"（当前版本 v{it['version']}），请刷新预览后再确认",
                            batch_no=it["batch_no"], current_version=it["version"],
                        )

                # 3) 窑位合法且不重复。
                seen_positions = set()
                for it in items:
                    pos = it["position_no"]
                    if not 1 <= pos <= kiln["position_count"]:
                        db.rollback()
                        return err(
                            409, "POSITION_INVALID",
                            f"窑位 {pos} 不存在：窑 {kiln['code']} 的窑位为 1..{kiln['position_count']}",
                            position_no=pos,
                        )
                    if pos in seen_positions:
                        db.rollback()
                        return err(
                            409, "POSITION_DUPLICATE",
                            f"窑位 {pos} 被重复占用，请刷新草案",
                            position_no=pos,
                        )
                    seen_positions.add(pos)

                # 4) 两项区间均须有交集，失败时指出冲突批次。
                p_lo = max(it["peak_min"] for it in items)
                p_hi = min(it["peak_max"] for it in items)
                if p_lo > p_hi:
                    hi = max(items, key=lambda it: it["peak_min"])
                    lo = min(items, key=lambda it: it["peak_max"])
                    db.rollback()
                    return err(
                        409, "NO_INTERSECTION_PEAK",
                        f"峰温区间无交集：批次 {hi['batch_no']} 要求 ≥{p_lo}℃，"
                        f"批次 {lo['batch_no']} 仅允许 ≤{p_hi}℃",
                        batches=[hi["batch_no"], lo["batch_no"]],
                    )
                s_lo = max(it["soak_min"] for it in items)
                s_hi = min(it["soak_max"] for it in items)
                if s_lo > s_hi:
                    hi = max(items, key=lambda it: it["soak_min"])
                    lo = min(items, key=lambda it: it["soak_max"])
                    db.rollback()
                    return err(
                        409, "NO_INTERSECTION_SOAK",
                        f"保温区间无交集：批次 {hi['batch_no']} 要求 ≥{s_lo} 分钟，"
                        f"批次 {lo['batch_no']} 仅允许 ≤{s_hi} 分钟",
                        batches=[hi["batch_no"], lo["batch_no"]],
                    )

                # 5) 批次未被其他已确认窑次使用。
                used = used_batch_map(db, [it["batch_id"] for it in items])
                for it in items:
                    if it["batch_id"] in used:
                        db.rollback()
                        return err(
                            409, "BATCH_USED",
                            f"批次 {it['batch_no']} 已被窑次 {used[it['batch_id']]} 确认使用",
                            batch_no=it["batch_no"], run_no=used[it["batch_id"]],
                        )

                # 6) 全部通过：一次性写入不可变装载清单（批次数据按当时快照保存）。
                seq = db.execute("SELECT COUNT(*) FROM loads").fetchone()[0] + 1
                run_no = f"R{seq:04d}"
                cur = db.execute(
                    """INSERT INTO loads (run_no, kiln_id, kiln_code, peak_min, peak_max, soak_min, soak_max)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run_no, kiln["id"], kiln["code"], p_lo, p_hi, s_lo, s_hi),
                )
                load_id = cur.lastrowid
                for it in items:
                    db.execute(
                        """INSERT INTO load_items
                           (load_id, position_no, batch_id, batch_no, peak_min, peak_max, soak_min, soak_max)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (load_id, it["position_no"], it["batch_id"], it["batch_no"],
                         it["peak_min"], it["peak_max"], it["soak_min"], it["soak_max"]),
                    )
                # 草案已兑现为窑次：清空占位并提升版本，使其他端上的旧预览立即失效。
                db.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft_id,))
                bump_draft(db, draft_id)
                db.commit()
            except sqlite3.Error:
                db.rollback()
                raise

        load_row = db.execute("SELECT * FROM loads WHERE id = ?", (load_id,)).fetchone()
        return jsonify(load_json(db, load_row)), 201

    # ---------------- 已确认窑次（快照查询） ----------------

    @app.get("/api/loads")
    def list_loads():
        db = get_db()
        rows = db.execute("SELECT * FROM loads ORDER BY id DESC").fetchall()
        return jsonify([load_json(db, r) for r in rows])

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)
