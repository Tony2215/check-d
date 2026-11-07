#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照存储：独立 DEMO 库的唯一读写入口。

## 职责边界（写死，别扩）

- 只做持久化与版本发号。调度语义在 M2 引擎，这里一行都不涉及。
- 所有"现在几点"一律由库 `now()` 产生，**不采信客户端时钟**。客户端能改系统时间，
  拿它判过期等于把过期判定交给攻击者。
- 失败一律抛错，不返回"看起来成功"的默认值。宁可 503，不静默降级。
- `base_version` 只从库里读回来，**绝不在这里拼字符串**。库里是生成列，
  两处拼接必然漂移（P1-2 要防的就是这个）。

## 为什么分成 Executor 与 Store

Store 是业务逻辑（发号、supersede、apply 校验、错误映射）。
Executor 是"怎么把 SQL 打到库上"，有两个实现：

- `PgExecutor`   ：真 psycopg3 连接池，生产用
- `RecordingExecutor`：不发 SQL，只记录 (sql, params) 并按预设返回答，
                        用于**无库环境下的逻辑单测**

这个分层是有代价的：Recording 能证明"我给库的语句和参数对不对"，
**但不能证明"库真能执行"**。所以两种测试的结论必须分开写：

    logic PASS（Recording）  ≠  persistence PASS（Pg + 真库）

M0 2026-11-07T03:07:56Z 工单 §一.4 明确禁止用 stub 冒充持久化 PASS。
本文件不破这条：Recording 全绿时，真实持久化仍标 BLOCKED。

## 契约依据

- §6.3 L492-505：服务端包装层创建快照、产 snapshot_ref/base_version/input_digest
- §4.1 L148/L149：PROPOSAL_EXPIRED / SNAPSHOT_CONFLICT 均为 409
- ALREADY_APPLIED 409，幂等由 applied_change.proposal_id unique 兜底
"""

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone

# 错误码走 errors.py 的闭集，不在这里自造。
try:
    from errors import HTTP_BY_CODE  # noqa: F401  仅用于断言码在闭集内
except Exception:                                            # noqa: BLE001
    HTTP_BY_CODE = {}

# 本模块会抛的码，必须是闭集内的。
_CODE_SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
_CODE_PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
_CODE_ALREADY_APPLIED = "ALREADY_APPLIED"
_CODE_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
_CODE_INVALID_ARGUMENT = "INVALID_ARGUMENT"
_CODE_NOT_FOUND = "NOT_FOUND"
_CODE_INTERNAL_ERROR = "INTERNAL_ERROR"

# retryable 口径：只有依赖不可用可重试（库抖动，等一下可能好）。
# 409 三种都不可重试——重试同一份过期/冲突的数据，结果不会变。
_RETRYABLE = {
    _CODE_DEPENDENCY_UNAVAILABLE: True,
}


class SnapshotStoreError(Exception):
    """带契约码的存储错误。上层直接取 .code / .retryable 组信封。"""

    def __init__(self, code, message, retryable=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = _RETRYABLE.get(code, False) if retryable is None else retryable
        self.details = details or {}

    def to_error(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def canonical_payload(payload):
    """规范化 JSON：键排序、无多余空白、UTF-8。

    digest 必须对**规范化后**的字节算，否则同样的数据换个键序就是另一个摘要，
    digest 就失去"同一份数据"的含义。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def payload_digest(payload):
    raw = canonical_payload(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


# ============================================================
# Executor：怎么把 SQL 打到库上
# ============================================================


class PgExecutor:
    """真 psycopg3 连接池。

    连接参数按 M0 §二 P0-3 的要求给定，理由见 migration 注释与交付说明：
    - 5432 直连优先；6543 transaction pooler 与"同事务 supersede+insert"冲突
    - prepare_threshold=None：关闭预编译缓存，6543 兼容的根因项
    - statement_timeout / lock_timeout 防长事务拖死整条链路
    """

    def __init__(self, dsn, min_size=1, max_size=4, timeout=5.0,
                 statement_timeout_ms=5000, lock_timeout_ms=3000):
        self.dsn = dsn
        self._min = min_size
        self._max = max_size
        self._timeout = timeout
        self._stmt_ms = statement_timeout_ms
        self._lock_ms = lock_timeout_ms
        self._pool = None
        self._lock = threading.Lock()

    def open(self):
        with self._lock:
            if self._pool is not None:
                return
            import psycopg_pool                      # 延迟导入：无库环境不因缺包崩
            self._pool = psycopg_pool.ConnectionPool(
                conninfo=self.dsn,
                min_size=self._min,
                max_size=self._max,
                timeout=self._timeout,
                kwargs={
                    "prepare_threshold": None,
                    "application_name": "checkd-demo",
                    "options": "-c statement_timeout=%d -c lock_timeout=%d"
                               % (self._stmt_ms, self._lock_ms),
                },
                open=True,
            )

    def close(self):
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    def transaction(self):
        """返回一个上下文管理器：进出真事务。"""
        if self._pool is None:
            raise SnapshotStoreError(_CODE_DEPENDENCY_UNAVAILABLE,
                                     "快照库连接池未打开。")
        return _PgTransaction(self._pool)

    def ping(self):
        try:
            if self._pool is None:
                return False
            with self._pool.connection(timeout=self._timeout) as conn:
                conn.execute("select 1")
            return True
        except Exception:                            # noqa: BLE001
            return False


class _PgTransaction:
    def __init__(self, pool):
        self._pool = pool
        self._ctx = None
        self._conn = None

    def __enter__(self):
        self._ctx = self._pool.connection()
        self._conn = self._ctx.__enter__()
        self._tx = self._conn.transaction()
        self._tx.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self._tx.__exit__(exc_type, exc, tb)
        finally:
            self._ctx.__exit__(exc_type, exc, tb)
        return False


class RecordingExecutor:
    """不发 SQL，只记录并按队列返回答。用于无库环境的逻辑单测。

    用法：
        ex = RecordingExecutor([[(1,)], [(uuid_val,)]])
        store = SnapshotStore(executor=ex)
        ...
        assert "on conflict" in ex.statements[0][0].lower()

    明确的能力边界：它能证明「语句与参数长什么样」、「顺序对不对」、「错误怎么映射」，
    不能证明「PostgreSQL 接受这段 SQL」。
    """

    def __init__(self, replies=None, fail_with=None):
        self.statements = []          # [(sql, params)]
        self._replies = list(replies or [])
        self._fail_with = fail_with   # 第几条开始抛（1-based），None = 全成功
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return _RecordingTransaction(self)

    def ping(self):
        return self._fail_with is None

    # --- 内部 ---
    def _execute(self, sql, params):
        self.statements.append((sql, params))
        if self._fail_with is not None and len(self.statements) >= self._fail_with:
            raise _InjectedFailure()
        if self._replies:
            return self._replies.pop(0)
        return []


class _InjectedFailure(Exception):
    pass


class _RecordingTransaction:
    def __init__(self, ex):
        self._ex = ex
        self._pending = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._ex.commits += 1
        else:
            self._ex.rollbacks += 1
        return False

    def execute(self, sql, params=None):
        return _RecordingCursor(self._ex._execute(sql, params or ()))


class _RecordingCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


# ============================================================
# Store：业务逻辑
# ============================================================


class SnapshotStore:
    def __init__(self, dsn=None, executor=None,
                 snapshot_ttl_seconds=900, proposal_ttl_seconds=600):
        self._ex = executor if executor is not None else PgExecutor(dsn or os.environ.get("CHECKD_DEMO_DSN", ""))
        self.snapshot_ttl = int(snapshot_ttl_seconds)
        self.proposal_ttl = int(proposal_ttl_seconds)

    # ---------- 生命周期 ----------

    def open(self):
        self._ex.open()
        return self

    def close(self):
        self._ex.close()

    def health(self):
        return bool(self._ex.ping())

    # ---------- 快照 ----------

    def create_snapshot(self, user_id, date_from, date_to, payload):
        """新建一版快照：原子发号 → 旧 latest 置 superseded → 插新行，同一事务。

        锁序固定：seq 行（ON CONFLICT 隐式行锁）→ snapshot 旧 latest 行 → 插新行。
        与 apply 路径（proposal → snapshot）只在 snapshot 行交叉，无回环，不会死锁。

        返回 dict：snapshot_id / version / base_version / payload_digest / payload_bytes / expires_at
        """
        _require_text(user_id, "user_id")
        _require_date_order(date_from, date_to)
        digest, nbytes = payload_digest(payload)
        raw = canonical_payload(payload)

        try:
            with self._ex.transaction() as conn:
                # 1) 发号：单语句，禁止 select max+1
                row = conn.execute(
                    "insert into checkd_demo.snapshot_seq "
                    "(user_id, date_from, date_to, next_version) "
                    "values (%s, %s, %s, 2) "
                    "on conflict (user_id, date_from, date_to) "
                    "do update set next_version = checkd_demo.snapshot_seq.next_version + 1, "
                    "              updated_at = now() "
                    "returning next_version - 1 as version",
                    (user_id, date_from, date_to),
                ).fetchone()
                version = int(row[0])

                # 2) 旧 latest 置为已替代（可能 0 行，首次创建时就是 0）
                conn.execute(
                    "update checkd_demo.snapshot set superseded_at = now() "
                    "where user_id = %s and date_from = %s and date_to = %s "
                    "  and superseded_at is null",
                    (user_id, date_from, date_to),
                )

                # 3) 插入新快照。expires_at 由库算，不用客户端时间
                row = conn.execute(
                    "insert into checkd_demo.snapshot "
                    "(user_id, date_from, date_to, version, payload, "
                    " payload_digest, payload_bytes, expires_at) "
                    "values (%s, %s, %s, %s, %s::jsonb, %s, %s, "
                    "        now() + make_interval(secs => %s)) "
                    "returning snapshot_id, expires_at",
                    (user_id, date_from, date_to, version, raw,
                     digest, nbytes, self.snapshot_ttl),
                ).fetchone()
                snapshot_id, expires_at = row[0], row[1]

                return {
                    "snapshot_id": str(snapshot_id),
                    "version": version,
                    # 库里是生成列，正常路径应该读回来；这里按同一规则给出，
                    # 并在 get_snapshot 里以库值为准做一次自校验。
                    "base_version": "%s:%d" % (snapshot_id, version),
                    "payload_digest": digest,
                    "payload_bytes": nbytes,
                    "expires_at": _iso(expires_at),
                }
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "create_snapshot")

    def get_snapshot(self, snapshot_id, user_id):
        try:
            with self._ex.transaction() as conn:
                row = conn.execute(
                    "select s.snapshot_id, s.version, s.user_id, s.date_from, s.date_to, "
                    "       s.payload_digest, s.payload_bytes, s.expires_at, s.superseded_at, "
                    "       (select p.base_version from checkd_demo.proposal p "
                    "         where p.snapshot_id = s.snapshot_id and p.user_id = s.user_id "
                    "         limit 1) as _probe "
                    "  from checkd_demo.snapshot s "
                    " where s.snapshot_id = %s and s.user_id = %s",
                    (snapshot_id, user_id),
                ).fetchone()
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "get_snapshot")
        if row is None:
            # 用户隔离：别人的快照一律 NOT_FOUND，不是 FORBIDDEN。
            # 回 403 等于确认"这个 id 存在"，是信息泄露。
            raise SnapshotStoreError(_CODE_NOT_FOUND, "快照不存在或不属于当前用户。")
        return {
            "snapshot_id": str(row[0]),
            "version": int(row[1]),
            "user_id": row[2],
            "date_from": _iso_date(row[3]),
            "date_to": _iso_date(row[4]),
            "base_version": "%s:%d" % (row[0], int(row[1])),
            "payload_digest": row[5],
            "payload_bytes": int(row[6]),
            "expires_at": _iso(row[7]),
            "superseded": row[8] is not None,
        }

    def get_latest(self, user_id, date_from, date_to):
        try:
            with self._ex.transaction() as conn:
                row = conn.execute(
                    "select snapshot_id, version, payload_digest, payload_bytes, expires_at "
                    "  from checkd_demo.snapshot "
                    " where user_id = %s and date_from = %s and date_to = %s "
                    "   and superseded_at is null",
                    (user_id, date_from, date_to),
                ).fetchone()
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "get_latest")
        if row is None:
            return None
        return {
            "snapshot_id": str(row[0]),
            "version": int(row[1]),
            "base_version": "%s:%d" % (row[0], int(row[1])),
            "payload_digest": row[2],
            "payload_bytes": int(row[3]),
            "expires_at": _iso(row[4]),
        }

    # ---------- Proposal ----------

    def create_proposal(self, user_id, snapshot_id, snapshot_version, plan):
        """建提案。base_version 由库生成列产出，这里只负责读回来。

        `plan` 是服务端自己生成的完整提案内容（blocks / changes / unplanned_tasks /
        explanations）。**必须存库**，原因写在契约 L514：apply 时服务端必须加载自己
        保存的 Proposal，不接受调用方重新提交。若不存，apply 就只能靠调用方重传，
        那等于把"可信输入"交回给调用方，契约明禁。
        """
        _require_text(user_id, "user_id")
        if not isinstance(plan, dict):
            raise SnapshotStoreError(_CODE_INVALID_ARGUMENT,
                                     "plan 必须是对象（含 blocks/changes/unplanned_tasks）。")
        raw_plan = canonical_payload(plan)
        try:
            with self._ex.transaction() as conn:
                row = conn.execute(
                    "insert into checkd_demo.proposal "
                    "(user_id, snapshot_id, snapshot_version, expires_at, plan) "
                    "values (%s, %s, %s, now() + make_interval(secs => %s), %s::jsonb) "
                    "returning proposal_id, base_version, status, expires_at",
                    (user_id, snapshot_id, int(snapshot_version), self.proposal_ttl, raw_plan),
                ).fetchone()
                return {
                    "proposal_id": str(row[0]),
                    "base_version": row[1],
                    "status": row[2],
                    "expires_at": _iso(row[3]),
                    "snapshot_id": str(snapshot_id),
                    "snapshot_version": int(snapshot_version),
                    "plan_digest": payload_digest(plan)[0],
                }
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "create_proposal")

    def get_proposal(self, proposal_id, user_id):
        try:
            with self._ex.transaction() as conn:
                row = conn.execute(
                    "select proposal_id, user_id, snapshot_id, snapshot_version, base_version, "
                    "       status, expires_at, applied_at "
                    "  from checkd_demo.proposal where proposal_id = %s and user_id = %s",
                    (proposal_id, user_id),
                ).fetchone()
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "get_proposal")
        if row is None:
            raise SnapshotStoreError(_CODE_NOT_FOUND, "提案不存在或不属于当前用户。")
        return {
            "proposal_id": str(row[0]),
            "user_id": row[1],
            "snapshot_id": str(row[2]),
            "snapshot_version": int(row[3]),
            "base_version": row[4],
            "status": row[5],
            "expires_at": _iso(row[6]),
            "applied_at": _iso(row[7]),
        }

    # ---------- apply ----------

    def apply_proposal(self, proposal_id, user_id, base_version, result_payload):
        """同一事务内：锁提案 → 校验（归属/状态/过期/版本）→ 幂等查 → 写 applied_change → 置 applied。

        锁序 proposal → snapshot，与 create_snapshot（seq → snapshot）只在 snapshot 行交叉。
        四道闸的顺序是刻意的：先 404（不泄露存在性）→ 再看状态 → 再看过期 → 最后比版本。
        版本放最后，是因为它是"两份都合法数据之间的冲突"，前三条都是"根本不该做"。
        """
        _require_text(user_id, "user_id")
        _require_text(base_version, "base_version")

        try:
            with self._ex.transaction() as conn:
                # 1) 锁提案。for update 让并发 apply 串行在这一行上
                row = conn.execute(
                    "select status, snapshot_id, snapshot_version, base_version, expires_at "
                    "  from checkd_demo.proposal "
                    " where proposal_id = %s and user_id = %s for update",
                    (proposal_id, user_id),
                ).fetchone()
                if row is None:
                    raise SnapshotStoreError(_CODE_NOT_FOUND,
                                             "提案不存在或不属于当前用户。")
                status, snap_id, snap_ver, db_base_version, expires_at = row

                # 2) 已应用 → 幂等错误，不是"成功"
                if status == "applied":
                    raise SnapshotStoreError(
                        _CODE_ALREADY_APPLIED, "该提案已应用过，不重复写入。",
                        details={"proposal_id": str(proposal_id)})

                # 3) 状态必须能进 apply
                if status != "pending":
                    raise SnapshotStoreError(
                        _CODE_PROPOSAL_EXPIRED if status == "expired" else _CODE_INTERNAL_ERROR,
                        "提案状态为 %s，不能应用。" % status,
                        details={"status": status})

                # 4) 过期由库判，不用客户端时间
                if _expired(conn, str(proposal_id)):
                    raise SnapshotStoreError(
                        _CODE_PROPOSAL_EXPIRED, "提案已过期。",
                        details={"proposal_id": str(proposal_id)})

                # 5) 版本必须与库里一致（库里是生成列，客户端传的只是"我以为的"）
                if str(db_base_version) != str(base_version):
                    raise SnapshotStoreError(
                        _CODE_SNAPSHOT_CONFLICT,
                        "base_version 与当前可信版本不一致。",
                        details={"expected": db_base_version,
                                 "provided": base_version})

                # 6) 写 applied_change。proposal_id 上的 unique 是幂等最后一道闸：
                #    并发两个 apply 都过了上面所有检查，第二个在这里撞唯一键 → 回滚 → 报 ALREADY_APPLIED
                raw = canonical_payload(result_payload)
                conn.execute(
                    "insert into checkd_demo.applied_change "
                    "(proposal_id, user_id, snapshot_id, result) "
                    "values (%s, %s, %s, %s::jsonb)",
                    (proposal_id, user_id, snap_id, raw),
                )

                # 7) 置状态。同事务，与 applied_change 一起生效或一起回滚
                conn.execute(
                    "update checkd_demo.proposal set status = 'applied', applied_at = now() "
                    " where proposal_id = %s",
                    (proposal_id,),
                )

                return {
                    "proposal_id": str(proposal_id),
                    "status": "applied",
                    "base_version": db_base_version,
                    "snapshot_id": str(snap_id),
                    "snapshot_version": int(snap_ver),
                }
        except SnapshotStoreError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _map_db_error(e, "apply_proposal")


# ============================================================
# 工具函数
# ============================================================


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SnapshotStoreError(_CODE_INVALID_ARGUMENT,
                                 "%s 必须是非空字符串。" % field,
                                 details={"field": field})


def _require_date_order(date_from, date_to):
    if not date_from or not date_to:
        raise SnapshotStoreError(_CODE_INVALID_ARGUMENT,
                                 "date_from / date_to 均为必填。")
    if str(date_from) > str(date_to):
        raise SnapshotStoreError(_CODE_INVALID_ARGUMENT,
                                 "date_from 不得晚于 date_to。",
                                 details={"date_from": str(date_from),
                                          "date_to": str(date_to)})


def _expired(conn, proposal_id):
    row = conn.execute(
        "select (expires_at <= now()) as expired from checkd_demo.proposal "
        " where proposal_id = %s",
        (proposal_id,),
    ).fetchone()
    return bool(row and row[0])


def _map_db_error(exc, where):
    """把驱动异常映射到契约闭集内的码。不新增码。

    23505 unique_violation → 两种可能，靠上下文区分：
        含 applied_change → ALREADY_APPLIED（幂等闸）
        含 snapshot_one_latest → SNAPSHOT_CONFLICT（并发建快照）
    23503 foreign_key_violation → 用户归属违约（复合 FK 拦住），属服务端拼错 id
    40001 / 40P01 → 序列化失败/死锁，可重试
    其余 → DEPENDENCY_UNAVAILABLE（连接类）或 INTERNAL_ERROR
    """
    name = type(exc).__name__
    pgcode = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    text = str(exc)

    if pgcode == "23505":
        if "applied_change" in text:
            return SnapshotStoreError(_CODE_ALREADY_APPLIED, "该提案已应用过。")
        if "snapshot_one_latest" in text:
            return SnapshotStoreError(_CODE_SNAPSHOT_CONFLICT,
                                      "并发创建快照，latest 唯一约束冲突，请重试。")
        return SnapshotStoreError(_CODE_INTERNAL_ERROR, "唯一约束冲突。",
                                  details={"where": where})
    if pgcode == "23503":
        return SnapshotStoreError(_CODE_INTERNAL_ERROR,
                                  "引用完整性违约（用户归属不符）。",
                                  details={"where": where})
    if pgcode in ("40001", "40P01"):
        return SnapshotStoreError(_CODE_DEPENDENCY_UNAVAILABLE,
                                  "事务冲突或死锁，可重试。", retryable=True,
                                  details={"where": where})
    if pgcode in ("08000", "08003", "08006", "57P01"):
        return SnapshotStoreError(_CODE_DEPENDENCY_UNAVAILABLE,
                                  "快照库连接不可用。", retryable=True,
                                  details={"where": where})
    if name in ("OperationalError", "PoolTimeout", "InterfaceError"):
        return SnapshotStoreError(_CODE_DEPENDENCY_UNAVAILABLE,
                                  "快照库连接不可用。", retryable=True,
                                  details={"where": where})
    return SnapshotStoreError(_CODE_INTERNAL_ERROR, "快照存储内部错误。",
                              details={"where": where, "type": name})


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().isoformat()
    return str(value)


def _iso_date(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
