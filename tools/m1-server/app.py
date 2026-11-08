#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CheckD 比赛版工具服务：把已验证的 Mock 工具层暴露成单端口 HTTP 服务。

设计约束（逐条对应 M0 2026-11-05 12:53:29Z 的工作包 B/C 项与用户红线）：

1. 单一实现。工具逻辑不在这里，全部来自 tools/m1-mock/tool_server.py。
   本文件只做传输层：鉴权、JSON、HTTP 状态、证据记录。改工具行为必须改 mock，
   不在这里长第二套逻辑。
2. 只绑回环。默认 BIND=127.0.0.1；只有部署到平台沙箱时才由启动命令显式传
   BIND=0.0.0.0。绝不在本机默认对外监听。
3. 鉴权失败即关闭（fail-closed）。CHECKD_DEMO_TOKEN 与 CHECKD_DEMO_TOKEN_SHA256
   都未设置时，所有业务端点返回 503，不给"先跑起来再补密码"的空间。
   两者任一已设即可工作；指纹模式见 DEMO_TOKEN_SHA256 处的说明。
   未设置 CHECKD_ADMIN_KEY 时，reset 与 evidence 端点整体禁用。
4. 对外只认一把随机口令，内部演示身份与口令解耦。口令匹配后固定以演示用户
   身份调用工具，mock 自带的 demo-token-alice/bob 不出现在任何对外文案里。
5. HTTP 状态取自契约 §4.1 的 21 码表（errors.py，M3 已复验）。未知码不包装成
   信封透传，改为脱敏的 500。
6. 每次工具调用写入证据日志：工具名、参数、成功与否、错误码、耗时。这是
   M0 要求的「可追溯记录」，平台侧不向模型透出 tool_call id，只能由服务端出。
7. 演示数据全在进程内存，reset 用重新加载模块的方式回到初始 fixture，
   不写任何数据库。
"""

import importlib
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
import hmac
import hashlib
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import errors as errs  # noqa: E402  PR-A 的 21 码 HTTP 映射

import tool_server as mock  # noqa: E402  唯一的工具实现

SERVICE_NAME = "checkd-agent-tools"
SERVICE_VERSION = "1.0.0"

DEMO_TOKEN = os.environ.get("CHECKD_DEMO_TOKEN", "")

# 方案 A（2026-11-08，M0 15:55 询问后定）：允许只配置**口令的指纹**，不配置明文。
#
# 起因：共享口令要求「服务端与调用方持有同一个字符串」，而「同一个」只能靠传输。
# M0 同时要求「任何渠道不得出现明文」，于是三条一起构成死锁——不是流程没写细，
# 是共享口令这个机制本身走不通。
#
# 改成存指纹：调用方自持明文，服务端只拿来访值算一遍 SHA-256 去比。
# 指纹泄露造不出口令（SHA-256 不可逆），因此部署目录里即便留的是指纹，
# 平台内部读到也拿不到可用凭证。
#
# 逗号分隔，支持多个 —— 每个调用方各自轮换，互不影响。
#
# 只接受 64 位小写十六进制，其余一律丢弃并告警。
# 起因：M2 2026-11-08 19:19 报 P1 —— 配置侧写进非 ASCII 时，比对循环里的
# want.encode("ascii") 会抛 UnicodeEncodeError，客户端拿到的是**空应答**（curl 52），
# 既不是 401 也不是 503。那等于把一个配置笔误放大成「服务不可用」，
# 排查时还会误以为是网络问题。在启动阶段就过滤，非法项永远进不了比对循环。
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _parse_sha256_list(raw):
    good, bad = [], []
    for piece in raw.split(","):
        piece = piece.strip().lower()
        if not piece:
            continue
        if _SHA256_RE.fullmatch(piece):
            good.append(piece)
        else:
            bad.append(len(piece))     # 只记长度，绝不把疑似口令的内容打到日志
    for n in bad:
        sys.stderr.write(
            "WARN: CHECKD_DEMO_TOKEN_SHA256 含 %d 字符的非法项（须为 64 位十六进制），已丢弃。\n" % n)
    return tuple(good)


DEMO_TOKEN_SHA256 = _parse_sha256_list(os.environ.get("CHECKD_DEMO_TOKEN_SHA256", ""))

ADMIN_KEY = os.environ.get("CHECKD_ADMIN_KEY", "")
# 口令通过后固定使用的演示身份；不对外暴露 mock 内部 token
DEMO_USER_TOKEN = os.environ.get("CHECKD_DEMO_USER", "demo-token-alice")

_LOCK = threading.Lock()
_STATS = {"starts_at": None, "calls": 0, "errors": 0}
_EVIDENCE = []          # 最近 500 条工具调用
_EVIDENCE_CAP = 500

# ---------------------------------------------------------------- 防护
# 2026-11-05 用户要求「公网那边被攻击了别影响到我」。
# 四道闸，全部可用环境变量收紧，默认值按公网部署的保守面取：
#   1) body 上限    —— 防超大 payload 打爆内存
#   2) 滑动窗口限流 —— 防刷；管理端点额度更小
#   3) socket 超时  —— 防慢速连接耗尽线程
#   4) health 精简  —— 公网不暴露 bind/port/口令指纹
# 说明：这些都不改变契约内的 21 个错误码闭集，限流复用 DEPENDENCY_UNAVAILABLE(503)。
MAX_BODY_BYTES = int(os.environ.get("CHECKD_MAX_BODY", "262144"))        # 256 KiB
RATE_LIMIT_PER_MIN = int(os.environ.get("CHECKD_RATE_PER_MIN", "120"))
# 管理端点是"脚本在调"不是"人在点"，额度要给够：一次完整闭环会连打 reset + demo_confirm。
RATE_LIMIT_ADMIN_PER_MIN = int(os.environ.get("CHECKD_RATE_ADMIN_PER_MIN", "60"))
SOCKET_TIMEOUT = float(os.environ.get("CHECKD_SOCKET_TIMEOUT", "15"))
HEALTH_MINIMAL = os.environ.get("CHECKD_HEALTH_MINIMAL", "1") == "1"

# M2 引擎（可选）：CHECKD_ENGINE=1 时在容器内回环上拉起 Dart 引擎。
# 默认关，保证「引擎跑不起来也不影响已有链路」；开的时候 /diag 会如实报状态，
# 绝不在引擎不可用时假装成功。
ENGINE_ENABLED = os.environ.get("CHECKD_ENGINE", "0") == "1"
ENGINE_PORT = int(os.environ.get("M2_ENGINE_PORT", "8787"))
ENGINE_ENTRY = os.environ.get("CHECKD_ENGINE_ENTRY", "tools/m2-engine/bin/serve.dart")
_ENGINE = {"pid": None, "error": None}

# 工具层是否真的把 generate/validate 转发给 M2 引擎。
#
# 与 ENGINE_ENABLED 是两件事，必须分开：
#   ENGINE_ENABLED=1 只表示「引擎进程被拉起来了」（/diag 的 engine.up 看这个）
#   ENGINE_WIRED=1   才表示「工具调用真的打到引擎」（/health 的 scheduler_mode 看这个）
#
# 2026-11-06 现状：ENGINE_ENABLED=1 但 ENGINE_WIRED=0 —— 引擎活着、工具层仍走 mock。
# 这正是 M0 14:16:20Z 放行令第 4 条要禁止的「静默回退」形态：引擎明明在，
# 调用却不经过它，而外部看不出来。所以这个标识必须对外可见，且不许写死成 REAL。
ENGINE_WIRED = os.environ.get("CHECKD_ENGINE_WIRED", "0") == "1"

# 快照库接线开关（三态，见 _snapshot_state）。
#
#   off（默认）：走现有 mock 路径，行为与 2026-11-07 之前完全一致
#   on  + DSN 可用：工具走真快照
#   on  + DSN 缺失/连不上：503 fail-closed，**绝不回退 mock**
#
# 为什么不把默认设成 on：线上当前没有 DSN，设 on 会让能演示的链路全部 503。
# M0 2026-11-07T03:36:48Z 工单 §三 要求的是「开了就必须真」，不是「关着也要 503」。
SNAPSHOT_MODE = os.environ.get("CHECKD_SNAPSHOT_MODE", "off").lower()
DEMO_DSN = os.environ.get("CHECKD_DEMO_DSN", "")

# 需要快照支撑的工具。只有这四个在 mode=on 时受 gate 约束，其余五个不受影响。
SNAPSHOT_TOOLS = frozenset({
    "generate_schedule", "validate_schedule",
    "request_approval", "apply_approved_changes",
})

_store = {"instance": None, "error": None}


def _snapshot_store():
    """懒加载快照存储。mode=off 或没 DSN 时返回 None，不抛。"""
    if SNAPSHOT_MODE != "on" or not DEMO_DSN:
        return None
    if _store["instance"] is not None:
        return _store["instance"]
    try:
        import snapshot_store
        st = snapshot_store.SnapshotStore(dsn=DEMO_DSN)
        st.open()
        if not st.health():
            raise RuntimeError("pool_open_but_unhealthy")
        _store["instance"] = st
        _store["error"] = None
        return st
    except Exception as e:                                   # noqa: BLE001
        # 记住失败原因，但不把 DSN 或异常原文带回公网（可能含连接串）。
        _store["error"] = type(e).__name__
        return None


def _snapshot_state():
    """给 /health 与 /diag 的只读状态。不主动探测时返回静态值。"""
    if SNAPSHOT_MODE != "on":
        return {"mode": SNAPSHOT_MODE, "dsn_configured": bool(DEMO_DSN), "ready": False}
    st = _snapshot_store()
    return {"mode": SNAPSHOT_MODE, "dsn_configured": True,
            "ready": st is not None, "error": _store.get("error")}

_RATE = {}              # (ip, bucket) -> deque[monotonic ts]
_RATE_LOCK = threading.Lock()


def _rate_ok(ip, limit, bucket="biz"):
    """滑动窗口计数。True = 放行；False = 已超限。

    **按 (IP, 类别) 分别计数**。2026-11-05 实测踩到的坑：起初只按 IP 记一个 deque，
    结果业务请求把窗口塞满后，管理端点（额度更小）被连带拒掉——
    demo_loop 的 admin/reset 与 demo/confirm 双双 503，链路在第 5、7 步断。
    两类流量必须各自成桶，否则收紧 A 就等于误伤 B。
    """
    now = time.monotonic()
    cutoff = now - 60.0
    key = (ip, bucket)
    with _RATE_LOCK:
        dq = _RATE.get(key)
        if dq is None:
            dq = _RATE[key] = deque()
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        if len(_RATE) > 4096:          # 防字典无限增长（长跑场景）
            for k in [k for k, v in list(_RATE.items()) if not v][:1024]:
                _RATE.pop(k, None)
    return True


class _BodyTooLarge(Exception):
    def __init__(self, size):
        super().__init__(size)
        self.size = size


# ---------------------------------------------------------------- 工具函数

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fp(secret):
    """口令指纹：只用于核对"环境变量是不是这份值"，不可逆，不泄露明文。"""
    if not secret:
        return ""
    import hashlib
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def _env_diag():
    """只读环境诊断。回答"容器能不能跑第二个进程"这一类问题，用实测替代推测。

    约束：不返回任何口令值；不监听任何端口（只 bind 后立即关闭）；不写文件时
    只试一次临时文件并删除；所有外部动作都有超时，失败即记 False 而不抛异常。
    """
    import platform
    import shutil
    import socket
    import subprocess
    import tempfile

    diag = {
        "os": {"platform": sys.platform, "release": platform.release(),
               "machine": platform.machine(), "python": platform.python_version()},
        "cpu_count": os.cpu_count(),
    }

    # 1) 有没有可用的子进程运行时：先看可执行文件在不在，再真跑一次。
    binaries = ("dart", "flutter", "node", "git", "bash", "sh", "curl", "java")
    diag["binaries"] = {b: shutil.which(b) for b in binaries}
    # 关键版本号：Dart 3 的语法（switch 表达式等）在 Dart 2 上编不过，
    # 这是「引擎能不能在本容器跑」的第一判据，必须报出来。
    #
    # 并发跑 + 收紧超时。原来串行 timeout=25，实测 /diag 要 29 秒——
    # 多数客户端默认 30s 就断，等于诊断端点自己先超时，失去诊断意义。
    # Dart VM 冷启动确实要几秒，所以给 8s 而不是 2s；两个并发，总耗时≈最慢那个。
    versions = {}
    vjobs = [b for b in ("dart", "flutter") if diag["binaries"].get(b)]

    def _ver(b):
        try:
            pv = subprocess.run([b, "--version"], capture_output=True,
                                text=True, timeout=8)
            versions[b] = ((pv.stdout or "") + (pv.stderr or "")).strip().splitlines()[:2]
        except Exception as e:                               # noqa: BLE001
            versions[b] = [type(e).__name__]

    vthreads = [threading.Thread(target=_ver, args=(b,), daemon=True) for b in vjobs]

    # spawn 探测跟着同一批并发跑：它也是一次 subprocess，串在后面就是白等 5 秒。
    spawn = {"ok": False}

    def _spawn_probe():
        try:
            p = subprocess.run([sys.executable, "-c", "print('spawn-ok')"],
                               capture_output=True, text=True, timeout=5)
            spawn.update({"ok": p.returncode == 0,
                          "stdout": (p.stdout or "").strip()[:40],
                          "returncode": p.returncode})
        except Exception as e:                               # noqa: BLE001
            spawn.update({"ok": False, "error": type(e).__name__})

    vthreads.append(threading.Thread(target=_spawn_probe, daemon=True))
    for t in vthreads:
        t.start()
    vdeadline = time.time() + 9.0
    for t in vthreads:
        t.join(max(0.05, vdeadline - time.time()))
    diag["versions"] = versions
    diag["subprocess"] = spawn

    # 2) 回环端口能否监听（第二个进程要靠它把引擎挂起来）。
    loop = {"ok": False}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        loop = {"ok": True, "port": s.getsockname()[1]}
        s.close()
    except Exception as e:                                   # noqa: BLE001
        loop = {"ok": False, "error": type(e).__name__}
    diag["loopback_listen"] = loop

    # 3) 出站能不能连出去（决定构建期能否拉依赖；只测连接，不发数据）。
    # 出站探测：并发跑，总耗时≈最慢一项而不是累加。
    # 串行时 6 个目标 × 4s 超时 ≈ 24s，加引擎探测实测 /diag 要 31s——
    # 多数客户端默认 30s 超时，等于诊断端点自己先超时，失去意义。
    egress = {}
    targets = (("1.1.1.1", 443), ("pypi.org", 443),
               ("pub.dev", 443), ("storage.googleapis.com", 443),
               ("raw.githubusercontent.com", 443), ("github.com", 443))

    def _probe(host, port):
        key = "%s:%d" % (host, port)
        try:
            s = socket.create_connection((host, port), timeout=4)
            s.close()
            egress[key] = True
        except Exception as e:                               # noqa: BLE001
            egress[key] = type(e).__name__

    workers = [threading.Thread(target=_probe, args=t, daemon=True) for t in targets]
    for w in workers:
        w.start()
    deadline = time.time() + 5.0
    for w in workers:
        w.join(max(0.05, deadline - time.time()))
    diag["egress"] = egress

    # 4) 工作目录是否可写（引擎二进制要落盘）。
    try:
        with tempfile.NamedTemporaryFile(dir=os.getcwd(), delete=True):
            diag["cwd_writable"] = True
    except Exception:                                        # noqa: BLE001
        diag["cwd_writable"] = False
    diag["cwd"] = os.getcwd()

    # 5) 只报环境变量名，不报值。
    diag["env_keys"] = sorted(k for k in os.environ
                              if k.startswith("CHECKD_") or k in ("BIND", "PORT", "PATH"))
    # 6) M2 引擎存活状态（只走回环）。
    diag["engine"] = _engine_status()
    # 快照库：mode=on 时才真去连（这里被低频访问，连一次可以接受）。
    diag["snapshot"] = _snapshot_state()
    # explanations 过滤统计：被剔除多少条、有没有未登记的码。
    # 只报码名与条数，不报被剔除的内容。
    try:
        import explanations_filter
        diag["explanations_filter"] = explanations_filter.STATS.snapshot()
    except Exception:                                        # noqa: BLE001
        diag["explanations_filter"] = {"error": "unavailable"}
    return diag


def _reset_demo_state():
    """重新加载 mock 模块，回到源码里的初始 fixture。

    不使用逐表清空：逐表清空要维护一份与 STATE 结构同步的清单，结构一变就漏。
    reload 的语义就是"回到初始"，没有维护成本。
    """
    global mock
    with _LOCK:
        mock = importlib.reload(mock)
    return {
        "tasks": len(mock.STATE["tasks"]),
        "courses": len(mock.STATE["courses"]),
        "proposals": len(mock.STATE["proposals"]),
        "approvals": len(mock.STATE["approvals"]),
        "writes": mock.STATE["writes"],
    }


def _snapshot_counts():
    return {
        "tasks": len(mock.STATE["tasks"]),
        "courses": len(mock.STATE["courses"]),
        "proposals": len(mock.STATE["proposals"]),
        "approvals": len(mock.STATE["approvals"]),
        "writes": mock.STATE["writes"],
    }


def _sanitize_explanations(body):
    """把响应里的 explanations 过公开白名单（契约 L316-318）。

    只处理确实带 explanations 数组的层，找不到就什么都不做——
    过滤层出问题不能影响主流程，但也不能假装过滤过。
    """
    try:
        import explanations_filter
    except Exception:                                        # noqa: BLE001
        sys.stderr.write("explanations_filter unavailable; skipped\n")
        return
    if not isinstance(body, dict):
        return
    layers = [body]
    for key in ("data", "result"):
        inner = body.get(key)
        if isinstance(inner, dict):
            layers.append(inner)
    for layer in layers:
        if isinstance(layer.get("explanations"), list):
            layer["explanations"] = explanations_filter.filter_explanations(
                layer["explanations"])


def _detail_list(details):
    """把各种 details 形状收成契约要求的数组。

    `SnapshotStoreError.details` 是 dict，`ToolError.details` 是 list，
    而 `errors.to_envelope` 只接受 list —— 直接传 dict 会 TypeError，
    于是一次参数错误会变成 500，把可诊断的失败伪装成服务端故障。
    """
    if not details:
        return None
    if isinstance(details, list):
        return details
    if isinstance(details, dict):
        return [{"field": str(k), "reason": str(v)[:200]} for k, v in details.items()]
    return None


def _snapshot_dispatch(name, args, trace, started):
    """mode=on 时的分派：四个快照工具走持久快照，其余工具仍走 mock。

    刻意独立成函数、且异常在这里就地转信封 —— `_call` 那段 try/except 是
    mock 路径的，两套错误面混在一起会让「库连不上」被记成「工具崩溃」。
    """
    st = _snapshot_store()
    ok = False
    code = None

    if st is None:                      # 双保险：调用方前面已拦过一次
        status = 503
        code = "DEPENDENCY_UNAVAILABLE"
        body = errs.to_envelope(
            code, "快照库不可用，本次调用未执行。",
            request_id=(args or {}).get("request_id"), trace_id=trace)
    else:
        try:
            import snapshot_tools
            user = mock.user_from_token(DEMO_USER_TOKEN)
            body = snapshot_tools.SnapshotTools(st).call(name, args, user)
            _sanitize_explanations(body)
            if not body.get("trace_id"):
                body["trace_id"] = trace
            status = 200
            ok = True
        except Exception as exc:                             # noqa: BLE001
            code = getattr(exc, "code", None)
            if code and errs.is_known_code(code):
                status = errs.http_status(code)
                body = errs.to_envelope(
                    code, getattr(exc, "message", "") or "请求未能完成。",
                    request_id=(args or {}).get("request_id"), trace_id=trace,
                    details=_detail_list(getattr(exc, "details", None)))
            else:
                sys.stderr.write("snapshot tool %s crashed:\n%s\n"
                                 % (name, traceback.format_exc()))
                code = "INTERNAL_ERROR"
                status = 500
                body = errs.to_envelope(
                    code, "服务端内部错误，请稍后重试并附 trace_id。",
                    request_id=(args or {}).get("request_id"), trace_id=trace)

    entry = {
        "ts": _now(),
        "trace_id": trace,
        "tool": name,
        "arguments": args,
        "ok": ok,
        "error_code": code,
        "duration_ms": int((time.time() - started) * 1000),
        "path": "snapshot",
    }
    with _LOCK:
        _STATS["calls"] += 1
        if not ok:
            _STATS["errors"] += 1
        _EVIDENCE.append(entry)
        if len(_EVIDENCE) > _EVIDENCE_CAP:
            del _EVIDENCE[:len(_EVIDENCE) - _EVIDENCE_CAP]
    return status, body, entry


def _call(name, args):
    """调用一个工具，返回 (http_status, body, evidence_entry)。"""
    started = time.time()
    trace = uuid.uuid4().hex[:16]
    ok = False
    code = None

    # 快照闸：仅在 CHECKD_SNAPSHOT_MODE=on 时生效。
    # 开了就必须真——库不可用一律 503，绝不悄悄回退 mock（M0 §三）。
    # 关着时（默认）本分支直接跳过，行为与接线前完全一致。
    if SNAPSHOT_MODE == "on" and name in SNAPSHOT_TOOLS and _snapshot_store() is None:
        body = errs.to_envelope(
            "DEPENDENCY_UNAVAILABLE",
            "快照库不可用，本次调用未执行。",
            request_id=(args or {}).get("request_id"), trace_id=trace)
        entry = {"ts": _now(), "trace_id": trace, "tool": name, "arguments": args,
                 "ok": False, "error_code": "DEPENDENCY_UNAVAILABLE", "duration_ms": 0,
                 "gate": "snapshot_not_ready"}
        with _LOCK:
            _STATS["calls"] += 1
            _STATS["errors"] += 1
            _EVIDENCE.append(entry)
        return 503, body, entry

    # mode=on 时四个快照工具改走持久快照（snapshot_tools）；其余工具仍走 mock，
    # 所以 get_approval_status / list_tasks 这些读写不受影响，演示链路不会整条断。
    if SNAPSHOT_MODE == "on" and name in SNAPSHOT_TOOLS:
        return _snapshot_dispatch(name, args, trace, started)

    try:
        result = mock.call_tool(name, args, DEMO_USER_TOKEN)
        body = result.get("structuredContent") or {}
        ok = not result.get("isError", False)
        # 引擎/工具的 explanations 一律过公开白名单（契约 L316-318）。
        # 非 dict 或没有 explanations 时原样返回，不因过滤层出错影响主流程。
        if ok:
            _sanitize_explanations(body)
        if ok:
            status = 200
        else:
            code = ((body.get("error") or {}).get("code")) or "INTERNAL_ERROR"
            status = errs.http_status(code) if errs.is_known_code(code) else 500
        if not body.get("trace_id"):
            body["trace_id"] = trace
    except Exception:
        sys.stderr.write("tool %s crashed:\n%s" % (name, traceback.format_exc()))
        code = "INTERNAL_ERROR"
        status = 500
        body = errs.to_envelope(code, "服务端内部错误，请稍后重试并附 trace_id。",
                                request_id=(args or {}).get("request_id"), trace_id=trace)

    entry = {
        "ts": _now(),
        "trace_id": trace,
        "tool": name,
        "arguments": args,
        "ok": ok,
        "error_code": code,
        "duration_ms": int((time.time() - started) * 1000),
    }
    with _LOCK:
        _STATS["calls"] += 1
        if not ok:
            _STATS["errors"] += 1
        _EVIDENCE.append(entry)
        if len(_EVIDENCE) > _EVIDENCE_CAP:
            del _EVIDENCE[:len(_EVIDENCE) - _EVIDENCE_CAP]
    return status, body, entry


def _demo_confirm(aid):
    """演示确认：把一条待审记录置为 approved，仅用于隔离演示环境。

    这不是真实审批。真实 approval_token 链（G5）要求独立审批服务、私钥签发、
    撤销机制与外部确认页，全部仍 BLOCKED。这里只发一个"演示用"令牌，
    让纵向闭环能在演示数据上真的跑完，并在响应里明写 demo=true。

    为什么必须同时写文件：mock 的 refresh_approval 会用审批文件覆盖内存状态，
    只改内存会在下一次读取时被打回 pending。
    """
    rec = mock.STATE["approvals"].get(aid)
    if not rec:
        return None
    rec["status"] = "approved"
    mock.STATE["approvals"][aid] = rec
    persisted = False
    try:
        approvals = mock.read_approvals()
        approvals[aid] = rec
        mock.write_approvals(approvals)
        persisted = True
    except Exception as exc:
        sys.stderr.write("demo_confirm persist failed: %s\n" % exc)
    return {"approval_id": aid, "status": "approved", "demo": True, "persisted": persisted}


# ---------------------------------------------------------------- MCP over HTTP

MCP_PROTOCOL = "2024-11-05"


def _mcp_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _mcp_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _mcp_handle(msg):
    """处理一条 JSON-RPC 报文。

    无状态实现：不发放 session id，每次请求自带全部语义。这样反向代理后面
    多副本也能工作，也不会因为一个客户端崩溃留下僵死会话。
    """
    if not isinstance(msg, dict):
        return _mcp_error(None, -32600, "invalid request")
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _mcp_result(rid, {
            "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # 通知不回包

    if method == "ping":
        return _mcp_result(rid, {})

    if method == "tools/list":
        return _mcp_result(rid, {"tools": mock.tool_defs()})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in mock.TOOLS:
            return _mcp_error(rid, -32602, "unknown tool: %s" % name)
        # 只调用一次：_call 里已经执行并记了证据，这里只把结果包成 MCP 形状。
        # 早先写成"先 _call 再 call_tool"会把工具跑两遍，写操作会被执行两次。
        status, body, _entry = _call(name, args)
        return _mcp_result(rid, {
            "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "structuredContent": body,
            "isError": status >= 400,
        })

    return _mcp_error(rid, -32601, "method not found: %s" % method)


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (SERVICE_NAME, SERVICE_VERSION)
    protocol_version = "HTTP/1.1"
    # 单连接读超时：慢速发 body 的连接到点即断，不会一直占着线程。
    timeout = SOCKET_TIMEOUT

    # ---- 基础工具

    def _send(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _fail(self, status, code, message, request_id=None, details=None):
        trace = uuid.uuid4().hex[:16]
        if errs.is_known_code(code):
            body = errs.to_envelope(code, message, request_id=request_id,
                                    trace_id=trace, details=details)
        else:
            body = {"api_version": "checkd.agent.v1", "request_id": request_id,
                    "trace_id": trace, "status": "error",
                    "error": {"code": "INTERNAL_ERROR", "message": message,
                              "retryable": False, "details": []}}
        self._send(status, body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            # 一个字节都不读：读了才拒等于自己先吃掉一次攻击。
            raise _BodyTooLarge(n)
        raw = self.rfile.read(n).decode("utf-8")
        return json.loads(raw)

    def _body_too_large(self, exc):
        self._fail(413, "INVALID_ARGUMENT",
                   "请求体超过上限 %d 字节（收到 %d）。" % (MAX_BODY_BYTES, exc.size))

    def _rate_check(self):
        """按 IP + 类别限流。管理端点单独成桶，额度独立。"""
        ip = self.client_address[0] if self.client_address else "-"
        path = self.path.split("?")[0]
        admin_like = path.startswith("/admin/") or path == "/demo/confirm"
        if admin_like:
            limit, bucket = RATE_LIMIT_ADMIN_PER_MIN, "admin"
        else:
            limit, bucket = RATE_LIMIT_PER_MIN, "biz"
        if _rate_ok(ip, limit, bucket):
            return True
        # 契约闭集里没有专门的限流码，复用 DEPENDENCY_UNAVAILABLE(503)；
        # 额外带 Retry-After 让客户端知道多久后重试。
        self.send_response(503)
        body = errs.to_envelope("DEPENDENCY_UNAVAILABLE", "请求过于频繁，请稍后再试。",
                                request_id=None, trace_id=uuid.uuid4().hex[:16])
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Retry-After", "60")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
        return False

    def _candidates(self):
        """收集所有可能承载口令的头。

        为什么不能只看 Authorization：平台反代会注入/覆盖这个头，
        自定义头也会被拼进来。实测结论——只认 Authorization 时永远 401，
        而 X-Admin-Key 能正常透传。所以这里把两个来源都收齐，逐个比对。
        """
        out = []
        for raw in (self.headers.get_all("X-Demo-Token") or []):
            for part in raw.split(","):
                if part.strip():
                    out.append(part.strip())
        for raw in (self.headers.get_all("Authorization") or []):
            for part in raw.split(","):
                part = part.strip()
                if part.lower().startswith("bearer "):
                    out.append(part[7:].strip())
                elif part:
                    out.append(part)
        return out

    def _auth_ok(self):
        # 明文与指纹任一配置即可工作（方案 A：两者并行，不破坏既有能力）。
        if not DEMO_TOKEN and not DEMO_TOKEN_SHA256:
            self._fail(503, "DEPENDENCY_UNAVAILABLE",
                       "服务未配置访问口令，已按失败即关闭处理。")
            return False
        cands = self._candidates()
        # N6（M0 2026-11-07 令 D）：区分「完全没带凭证」与「凭证无效」。
        # 错误码仍是闭集内的 APPROVAL_UNTRUSTED，不新增码；可区分性靠 message +
        # details.reason，客户端可以按字段判定，不必解析中文文案。
        if not cands:
            self._fail(401, "APPROVAL_UNTRUSTED",
                       "缺少访问凭证：请在 X-Demo-Token 请求头提供。",
                       details=[{"field": "X-Demo-Token", "reason": "missing"}])
            return False
        # 常量时间比对：逐个候选走 compare_digest，不用 in / == 提前返回。
        # 方案 A：明文命中 或 指纹命中，都算通过。
        ok = False
        for cand in cands:
            if DEMO_TOKEN and hmac.compare_digest(cand, DEMO_TOKEN):
                ok = True
            if DEMO_TOKEN_SHA256:
                # 用 bytes 比较：指纹是纯 ASCII，但候选值可能含非 ASCII
                # （请求头被塞了奇怪内容），str 版 compare_digest 会抛 TypeError 变 500。
                got = hashlib.sha256(cand.encode("utf-8", "surrogatepass")).hexdigest()
                for want in DEMO_TOKEN_SHA256:
                    if hmac.compare_digest(got.encode("ascii"), want.encode("ascii")):
                        ok = True
        if not ok:
            self._fail(401, "APPROVAL_UNTRUSTED", "凭证无效。",
                       details=[{"field": "X-Demo-Token", "reason": "invalid"}])
            return False
        return True

    def _admin_ok(self):
        if not ADMIN_KEY:
            self._fail(404, "NOT_FOUND", "该端点未启用。")
            return False
        if not hmac.compare_digest(self.headers.get("X-Admin-Key") or "", ADMIN_KEY):
            self._fail(401, "APPROVAL_UNTRUSTED", "管理凭证无效。")
            return False
        return True

    def log_message(self, fmt, *a):  # 请求日志进 stderr，不进响应
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # ---- 路由

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/health"):
            # 健康检查不鉴权，但也不给攻击者任何可用情报。
            # CHECKD_HEALTH_MINIMAL=1（公网默认）只回三样：活着、叫什么、什么版本。
            # 置 0 才给 bind/port/口令指纹——那是部署时自检用的，不该挂在公网。
            if HEALTH_MINIMAL:
                self._send(200, {"ok": True, "service": SERVICE_NAME,
                                 "version": SERVICE_VERSION,
                                 "scheduler_mode": _scheduler_mode()})
                return
            # 只给配置指纹（不可逆），用来判断"环境变量到底进没进来"，
            # 不打印明文口令。
            self._send(200, {
                "ok": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mock_version": getattr(mock, "SERVER_VERSION", "?"),
                "tool_count": len(mock.TOOLS),
                "auth_required": True,
                "auth_configured": bool(DEMO_TOKEN or DEMO_TOKEN_SHA256),
                "admin_configured": bool(ADMIN_KEY),
                # M0 2026-11-07 令 D：删掉 token_sha8 / admin_sha8。口令指纹即便不可逆，
                # 也让人可以离线比对字典，且对运维没有用处（要验「变量进没进来」只看
                # 上面两个 bool 就够）。
                "bind": os.environ.get("BIND", "127.0.0.1"),
                "port": os.environ.get("PORT", "8790"),
                "starts_at": _STATS["starts_at"],
                "calls": _STATS["calls"],
                "errors": _STATS["errors"],
                # M0 14:16:20Z 放行令第 4 条：保留可识别的 REAL/MOCK 状态。
                # 当前恒为 MOCK（工具层未接线），引擎进程存活另见 /diag 的 engine.up。
                "scheduler_mode": _scheduler_mode(),
                "engine_enabled": ENGINE_ENABLED,
                # 快照库接线状态。只报静态配置事实，不在这里连库——
                # /health 被高频探活，不能因为它去 ping 数据库。
                "snapshot_mode": SNAPSHOT_MODE,
                "snapshot_dsn_configured": bool(DEMO_DSN),
            })
            return

        if path == "/diag":
            # 只读环境诊断，仅管理凭证可读，且不返回任何口令值。
            # 用途：回答 M2 2026-11-05 16:54:03Z 的三个问题（能否跑第二进程 /
            # 容器里有没有 dart / 回环端口能否监听），用实测替代推测。
            if not self._admin_ok():
                return
            self._send(200, _env_diag())
            return

        if path == "/tools":
            if not self._auth_ok():
                return
            self._send(200, {"tools": mock.tool_defs(), "count": len(mock.TOOLS)})
            return

        if path == "/evidence":
            if not self._admin_ok():
                return
            with _LOCK:
                items = list(_EVIDENCE)
            self._send(200, {"count": len(items), "items": items})
            return

        if path == "/state":
            if not self._admin_ok():
                return
            self._send(200, _snapshot_counts())
            return

        if path == "/mcp":
            # 无状态实现不提供服务端推送流；明确拒绝而不是留空连接，
            # 让客户端立刻走 POST 路径，不会挂在等 SSE 上。
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._fail(404, "NOT_FOUND", "路径不存在。")

    def do_POST(self):
        if not self._rate_check():
            return
        path = self.path.split("?")[0]

        if path == "/admin/reset":
            if not self._admin_ok():
                return
            counts = _reset_demo_state()
            with _LOCK:
                _EVIDENCE.clear()
                _STATS["calls"] = 0
                _STATS["errors"] = 0
            self._send(200, {"ok": True, "reset": True, "counts": counts})
            return

        if path == "/mcp":
            if not self._auth_ok():
                return
            try:
                payload = self._json_body()
            except _BodyTooLarge as exc:
                self._body_too_large(exc)
                return
            except Exception:
                self._send(400, _mcp_error(None, -32700, "parse error"))
                return
            if isinstance(payload, list):          # 批量报文
                out = [r for r in (_mcp_handle(m) for m in payload) if r is not None]
                self._send(200, out)
                return
            resp = _mcp_handle(payload)
            if resp is None:                        # 通知：无 body
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, resp)
            return

        if path == "/demo/confirm":
            # 演示确认：只允许管理口令调用，且响应里带 demo=true。
            if not self._admin_ok():
                return
            try:
                body = self._json_body()
            except _BodyTooLarge as exc:
                self._body_too_large(exc)
                return
            except Exception:
                self._fail(400, "INVALID_ARGUMENT", "请求体不是合法 JSON。")
                return
            aid = (body or {}).get("approval_id")
            if not aid:
                self._fail(400, "INVALID_ARGUMENT", "approval_id 必填。")
                return
            out = _demo_confirm(aid)
            if not out:
                self._fail(404, "NOT_FOUND", "找不到该审批请求。")
                return
            self._send(200, out)
            return

        if path.startswith("/tools/"):
            if not self._auth_ok():
                return
            name = path[len("/tools/"):]
            if name not in mock.TOOLS:
                self._fail(404, "NOT_FOUND", "工具不存在：%s" % name)
                return
            try:
                args = self._json_body()
            except _BodyTooLarge as exc:
                self._body_too_large(exc)
                return
            except Exception:
                self._fail(400, "INVALID_ARGUMENT", "请求体不是合法 JSON。")
                return
            if not isinstance(args, dict):
                self._fail(400, "INVALID_ARGUMENT", "请求体必须是对象。")
                return
            status, body, _entry = _call(name, args)
            self._send(status, body)
            return

        self._fail(404, "NOT_FOUND", "路径不存在。")

    def do_DELETE(self):
        self._fail(405, "INVALID_ARGUMENT", "不支持的方法。")


def _scheduler_mode():
    """工具层实际走哪条路。**不探测引擎**，纯静态判定，保证 /health 不因回环探测变慢。

    MOCK              ：工具调用走 tools/m1-mock/tool_server.py（当前状态）
    REAL              ：工具调用已转发 M2 引擎（ENGINE_WIRED=1 且引擎已启用）
    MOCK_ENGINE_WIRED ：接线开关打开但引擎没启用——配置自相矛盾，必须显式暴露

    注意：REAL 只表示「转发已启用」，不表示引擎此刻可用。引擎存活看 /diag 的 engine.up。
    两件事分开报，免得把「进程在」误读成「链路通」。
    """
    if not ENGINE_WIRED:
        return "MOCK"
    if not ENGINE_ENABLED:
        return "MOCK_ENGINE_WIRED"
    return "REAL"


def _engine_status():
    """查容器内的 M2 引擎是否活着。只读，只走回环。"""
    import urllib.request
    st = {"enabled": ENGINE_ENABLED, "port": ENGINE_PORT,
          "entry": ENGINE_ENTRY, "entry_exists": os.path.exists(ENGINE_ENTRY),
          "pid": _ENGINE.get("pid"), "spawn_error": _ENGINE.get("error"),
          # 接线状态与进程状态分开报：前者是配置事实，后者要探测。
          "wired": ENGINE_WIRED, "scheduler_mode": _scheduler_mode()}
    if not ENGINE_ENABLED:
        return st
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % ENGINE_PORT,
                                    timeout=5) as r:
            st["health"] = json.loads(r.read().decode("utf-8"))
            st["up"] = True
    except Exception as e:                                   # noqa: BLE001
        st["up"] = False
        st["health_error"] = type(e).__name__
    # 引擎日志尾部：起不来时唯一能定位原因的东西。只回最后 12 行。
    try:
        with open(os.path.join(os.getcwd(), "engine.log"), "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()[-12:]
        st["log_tail"] = lines
    except Exception:                                        # noqa: BLE001
        st["log_tail"] = []
    return st


def _start_engine():
    """把 M2 的 Dart 引擎拉到容器内回环上跑。失败不致命，但会被 /diag 如实暴露。

    只用相对路径 `dart run tools/m2-engine/bin/serve.dart`：M2 2026-11-05
    17:33:55Z 已确认这套包相对导入、不要 pubspec、不要 pub get。
    """
    import subprocess
    if not ENGINE_ENABLED:
        return
    if not os.path.exists(ENGINE_ENTRY):
        _ENGINE["error"] = "entry_missing"
        return
    env = dict(os.environ)
    env["M2_ENGINE_PORT"] = str(ENGINE_PORT)
    env.setdefault("M2_ENGINE_HOST", "127.0.0.1")
    try:
        log = open(os.path.join(os.getcwd(), "engine.log"), "ab")
        # 直接执行，不用 `dart run`：容器里没有 pubspec，`dart run` 会去找
        # package config 而失败；脚本本身只有相对导入，直接跑即可。
        p = subprocess.Popen(["dart", ENGINE_ENTRY], env=env,
                             stdout=log, stderr=log, cwd=os.getcwd())
        _ENGINE["pid"] = p.pid
        sys.stderr.write("engine: spawned pid=%d port=%d entry=%s\n"
                         % (p.pid, ENGINE_PORT, ENGINE_ENTRY))
    except Exception as e:                                   # noqa: BLE001
        _ENGINE["error"] = "%s: %s" % (type(e).__name__, e)
        sys.stderr.write("engine: spawn failed %s\n" % _ENGINE["error"])


def main():
    bind = os.environ.get("BIND", "127.0.0.1")
    port = int(os.environ.get("PORT", "8790"))
    _STATS["starts_at"] = _now()

    if not DEMO_TOKEN and not DEMO_TOKEN_SHA256:
        sys.stderr.write("WARN: CHECKD_DEMO_TOKEN / CHECKD_DEMO_TOKEN_SHA256 均未设置，"
                         "业务端点将全部拒答（fail-closed）。\n")
    if not ADMIN_KEY:
        sys.stderr.write("WARN: CHECKD_ADMIN_KEY 未设置，reset/evidence 已禁用。\n")
    if bind not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("WARN: 绑定 %s 非回环。仅限平台沙箱；本机运行请用默认值。\n" % bind)

    _start_engine()
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("%s %s on %s:%d (tools=%d)\n"
                     % (SERVICE_NAME, SERVICE_VERSION, bind, port, len(mock.TOOLS)))
    sys.stderr.write("guard: body<=%d B, rate=%d/min (admin %d/min), timeout=%.0fs, health_minimal=%s\n"
                     % (MAX_BODY_BYTES, RATE_LIMIT_PER_MIN, RATE_LIMIT_ADMIN_PER_MIN,
                        SOCKET_TIMEOUT, HEALTH_MINIMAL))
    srv.serve_forever()


if __name__ == "__main__":
    main()
