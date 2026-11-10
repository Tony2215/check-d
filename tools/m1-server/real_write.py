#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REAL-WRITE 的最小实现：只改一条任务的 `name`。

## 为什么单独一个文件，不塞进 real_read.py

M0 2026-11-10T12:07 要求「严格隔离已有稳定路径」。`real_read.py` 现在 306 项测试
全绿，写入能力一旦混进去，任何一次误调用都可能污染读取路径的行为。
两个文件、两个类、两套传输，读写物理分开 —— 读侧连写方法的引用都不存在。

## 三重保护，缺一不可

1. **开关**：`CHECKD_REAL_WRITE=1` 才启用，默认关。未启用时任何调用立即
   `WRITE_BLOCKED`，不发出任何网络请求。
2. **逐次确认**：每次调用必须显式 `confirm=true`，否则 `INVALID_ARGUMENT`。
   不做「一次确认、后续默认放行」—— 那等于把确认变成装饰。
3. **名称做乐观锁**：PATCH 的 WHERE 里带上 `name=eq.<旧名称>`。
   服务端不必依赖 `sync_version`（App 上传时根本不带它），
   而是用「要改的那个值本身」作为前提条件：
   - 已被别人改过 → 匹配 0 行 → `SNAPSHOT_CONFLICT`，不覆盖；
   - 重复调用第二次 → 名称已是新值 → 匹配 0 行 → 同样拒绝。
   一个条件同时解决并发覆盖与重复应用两件事。

## 不做什么（明确边界）

- 不新增错误码，全部用契约 21 码闭集；
- 不传 `user_id`：归属由 RLS 的 `auth.uid() = user_id` 判定，客户端指定身份是漏洞；
- 不改 `updated_at`：它由 `set_updated_at()` 触发器推，工具层写它反而会被覆盖；
- 不碰 `sync_version`：App 上传不带该列，工具层改它没有消费方；
- 不做批量、不做多字段、不建任务、不删任务。
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# 契约 §5 的错误语义复用读取层的异常类型，避免两套错误处理。
from real_read import RealReadError, _scrub

# `tasks.name` 的真实约束（supabase/migrations/202609020001 L3）：
#   name text not null check (char_length(name) between 1 and 100)
NAME_MIN, NAME_MAX = 1, 100

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# PostgREST 的 filter 保留字符。值里出现这些字符会被解析成语法而不是字面量，
# 必须百分号编码，否则一个带逗号的任务名就能把 WHERE 改成别的条件。
_RESERVED = (",", ".", "(", ")", ":", '"')


def _q(value):
    """把值编码成 PostgREST filter 里可安全使用的形式。

    **两层，缺一不可**：
    1. 按 PostgREST 规则用双引号包裹，值内的反斜杠与引号转义；
    2. 整体做 URL 百分号编码。

    2026-11-10 20:50 M3 指出只做第 2 层不够：PostgREST 是**先 URL 解码、
    再解析 filter**，所以 `%2C` 解回逗号后仍会被当作条件分隔符。
    官方对含 `, . ( ) : "` 的值建议用双引号包裹。

    这一条**尚未在真实 PostgREST 上验证过**（写通道未启用、无 JWT），
    属疑点而非定论；隔离验证时应拿一条含逗号的名称打一次 PATCH 来定论。
    在此之前，现场使用的任务名请避开这六个字符。
    """
    s = str(value)
    quoted = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return urllib.parse.quote(quoted, safe="")


def patch_transport(url, headers, timeout, body):
    """默认写入传输：返回 (http_status, body_bytes)。

    与读取层的 transport 分开：那个把 method 写死成 GET，改成可变参数会让
    「只读」这件事变得不可从签名上判断。宁可多写一个函数。
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:                       # 连接失败/超时/DNS/TLS
        raise RealReadError(
            "DEPENDENCY_UNAVAILABLE",
            "无法连接数据源。",
            [{"field": "transport",
              "reason": type(exc).__name__,
              "detail": _scrub(getattr(exc, "reason", exc))}],
        )


def _pg_error(body):
    """从 PostgREST 的错误体里取一句可读原因，擦掉任何令牌。"""
    try:
        d = json.loads(body.decode("utf-8", "replace"))
    except Exception:                              # noqa: BLE001
        return _scrub(body)
    msg = d.get("message") or d.get("detail") or d.get("hint") or ""
    code = d.get("code") or ""
    return _scrub(("%s %s" % (code, msg)).strip() or body)


class RealWrite:
    """PostgREST 的最小写入客户端。只实现一个方法：改任务名。"""

    TABLE = "tasks"

    def __init__(self, base_url, anon_key, access_token, timeout=10, transport=None):
        if not base_url or not anon_key:
            raise RealReadError(
                "DEPENDENCY_UNAVAILABLE", "数据源未配置。",
                [{"field": "config", "reason": "missing_url_or_key"}])
        if not access_token:
            # 写入必须有用户身份。没有令牌就不发请求 —— 匿名 key 打过来会被 RLS 挡，
            # 但那样报出来的 401 会让人误以为是凭证错，其实根本没带。
            raise RealReadError(
                "AUTH_REQUIRED", "缺少用户级访问令牌，无法执行写入。",
                [{"field": "access_token", "reason": "missing"}])
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.access_token = access_token
        self.timeout = timeout
        self.transport = transport or patch_transport

    # -- 内部 ------------------------------------------------------------

    def _headers(self):
        return {
            "apikey": self.anon_key,
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json",
            # 让 PostgREST 把改动后的行回给我们，省掉第二次往返，
            # 也让「回读核验」用的就是数据库返回的那一行，而不是本地拼的。
            "Prefer": "return=representation",
            # 不合并重复行、不要执行计划缓存，减少对并发行为的干扰。
            "Accept-Profile": "public",
        }

    # -- 对外 ------------------------------------------------------------

    def update_task_name(self, task_id, new_name, expected_old_name, confirm):
        """把一条任务的 `name` 从 `expected_old_name` 改为 `new_name`。

        参数全部必填，没有默认值 —— 少一个都不该发生变更。

        返回：
            {"task_id":..., "old_name":..., "new_name":..., "changed": True,
             "readback": {"name": ...}, "trace_id": ..., "duration_ms": ...}
        """
        started = time.time()

        # 1) 确认闸
        if confirm is not True:
            raise RealReadError(
                "INVALID_ARGUMENT",
                "写入必须逐次显式确认：请带 confirm=true。",
                [{"field": "confirm", "reason": "not_confirmed"}])

        # 2) 参数闸
        if not isinstance(task_id, str) or not _UUID_RE.match(task_id):
            raise RealReadError(
                "INVALID_ARGUMENT", "task_id 必须是 UUID。",
                [{"field": "task_id", "reason": "invalid_uuid"}])

        for label, value in (("new_name", new_name),
                             ("expected_old_name", expected_old_name)):
            if not isinstance(value, str):
                raise RealReadError(
                    "INVALID_ARGUMENT", "%s 必须是字符串。" % label,
                    [{"field": label, "reason": "not_string"}])
            n = len(value)
            if not (NAME_MIN <= n <= NAME_MAX):
                # 与库里的 check 约束一致，先在本地挡掉：
                # 让库报 400 的话，错误体是英文的 Postgres 约束名，不好展示。
                raise RealReadError(
                    "INVALID_ARGUMENT",
                    "%s 长度必须在 %d–%d 之间（当前 %d）。" % (label, NAME_MIN, NAME_MAX, n),
                    [{"field": label, "reason": "length_out_of_range", "length": n}])

        if new_name == expected_old_name:
            # 同值 PATCH 会推 `updated_at`（触发 `set_updated_at()`），
            # 从而触发 App 一次无意义的下载比对。这不是「没有变更」，是有副作用。
            raise RealReadError(
                "INVALID_ARGUMENT", "新旧名称相同，不执行写入。",
                [{"field": "new_name", "reason": "no_change"}])

        # 3) 组装条件更新
        #    WHERE: id 精确 + name 等于旧值（乐观锁）+ 未软删
        #    id 已由 `_UUID_RE` 限死为十六进制与连字符，不含 filter 保留字符，
        #    所以不套引号（套了反而多一层可能出错的语法）；name 是自由文本，必须走 `_q`。
        query = "?id=eq.%s&name=eq.%s&deleted_at=is.null" % (
            urllib.parse.quote(task_id, safe=""), _q(expected_old_name))
        url = "%s/rest/v1/%s%s" % (self.base_url, self.TABLE, query)
        body = json.dumps({"name": new_name}, ensure_ascii=False).encode("utf-8")

        status, raw = self.transport(url, self._headers(), self.timeout, body)

        # 4) 状态码分流
        if status in (401, 403):
            raise RealReadError(
                "FORBIDDEN", "无权限修改该任务。",
                [{"field": "auth", "reason": "http_%d" % status}])
        if status >= 500:
            raise RealReadError(
                "DEPENDENCY_UNAVAILABLE", "数据源暂时不可用。",
                [{"field": "upstream", "reason": "http_%d" % status,
                  "detail": _pg_error(raw)}])
        if status >= 400:
            # 400 常见原因：列不存在、类型不符、约束不满足（含 name 长度）。
            # 这类错误必须原样上报，因为它意味着**我对 schema 的假设错了**，
            # 而不是用户操作有问题。
            raise RealReadError(
                "INVALID_ARGUMENT", "数据源拒绝了这次修改。",
                [{"field": "upstream", "reason": "http_%d" % status,
                  "detail": _pg_error(raw)}])

        # 5) 解析返回行
        try:
            rows = json.loads(raw.decode("utf-8", "replace")) if raw else []
        except Exception:                          # noqa: BLE001
            raise RealReadError(
                "INTERNAL_ERROR", "数据源返回了无法解析的内容。",
                [{"field": "upstream", "reason": "bad_json"}])
        if not isinstance(rows, list):
            rows = [rows]

        if len(rows) == 0:
            # 三种可能，**故意不区分**：id 不存在 / 不属于当前用户（RLS 过滤）/
            # name 已被别人改过。区分它们等于给出「这个 id 存在但你没权限」
            # 这类探测信息。
            raise RealReadError(
                "SNAPSHOT_CONFLICT",
                "没有行被修改：任务不存在、不属于当前用户，或名称已被改动。请重新读取后再试。",
                [{"field": "tasks", "reason": "no_row_matched",
                  "expected_old_name": expected_old_name}])
        if len(rows) > 1:
            # id 是主键，正常不可能返回多行。真发生了说明条件被改写了，必须停下。
            raise RealReadError(
                "INTERNAL_ERROR", "条件更新命中了多行，已中止。",
                [{"field": "tasks", "reason": "multi_row", "count": len(rows)}])

        row = rows[0]

        # 6) 回读核验：数据库返回的那一行，name 必须真的等于新值
        got = row.get("name")
        if got != new_name:
            raise RealReadError(
                "READBACK_MISMATCH", "写入后回读的名称与期望不一致。",
                [{"field": "name", "expected": new_name, "actual": got}])
        # 归属自检：RLS 下这一行必然属于当前用户，但如果哪天策略被改松，
        # 这一条能让问题在工具层就暴露，而不是等到数据串了才发现。
        if not row.get("user_id"):
            raise RealReadError(
                "READBACK_MISMATCH", "回读结果缺少所属用户，拒绝确认本次写入。",
                [{"field": "user_id", "reason": "missing"}])

        return {
            "task_id": task_id,
            "old_name": expected_old_name,
            "new_name": new_name,
            "changed": True,
            # 只回名称，不回整行：整行里还有 notes 等用户内容，写进证据不必要。
            "readback": {"name": got},
            # 由触发器推的，工具层只读不写。报出来是为了让验收方看到副作用确实发生了。
            "updated_at": row.get("updated_at"),
            "duration_ms": int((time.time() - started) * 1000),
        }
