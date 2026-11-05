#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CheckD 比赛版工具服务：把已验证的 Mock 工具层暴露成单端口 HTTP 服务。

设计约束（逐条对应 M0 2026-11-05 12:53:29Z 的工作包 B/C 项与用户红线）：

1. 单一实现。工具逻辑不在这里，全部来自 tools/m1-mock/tool_server.py。
   本文件只做传输层：鉴权、JSON、HTTP 状态、证据记录。改工具行为必须改 mock，
   不在这里长第二套逻辑。
2. 只绑回环。默认 BIND=127.0.0.1；只有部署到平台沙箱时才由启动命令显式传
   BIND=0.0.0.0。绝不在本机默认对外监听。
3. 鉴权失败即关闭（fail-closed）。未设置 CHECKD_DEMO_TOKEN 时，所有业务端点
   返回 503，不给"先跑起来再补密码"的空间。未设置 CHECKD_ADMIN_KEY 时，
   reset 与 evidence 端点整体禁用。
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
import sys
import threading
import time
import traceback
import uuid
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
ADMIN_KEY = os.environ.get("CHECKD_ADMIN_KEY", "")
# 口令通过后固定使用的演示身份；不对外暴露 mock 内部 token
DEMO_USER_TOKEN = os.environ.get("CHECKD_DEMO_USER", "demo-token-alice")

_LOCK = threading.Lock()
_STATS = {"starts_at": None, "calls": 0, "errors": 0}
_EVIDENCE = []          # 最近 500 条工具调用
_EVIDENCE_CAP = 500


# ---------------------------------------------------------------- 工具函数

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fp(secret):
    """口令指纹：只用于核对"环境变量是不是这份值"，不可逆，不泄露明文。"""
    if not secret:
        return ""
    import hashlib
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


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


def _call(name, args):
    """调用一个工具，返回 (http_status, body, evidence_entry)。"""
    started = time.time()
    trace = uuid.uuid4().hex[:16]
    ok = False
    code = None
    try:
        result = mock.call_tool(name, args, DEMO_USER_TOKEN)
        body = result.get("structuredContent") or {}
        ok = not result.get("isError", False)
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

    # ---- 基础工具

    def _send(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _fail(self, status, code, message, request_id=None):
        trace = uuid.uuid4().hex[:16]
        if errs.is_known_code(code):
            body = errs.to_envelope(code, message, request_id=request_id, trace_id=trace)
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
        raw = self.rfile.read(n).decode("utf-8")
        return json.loads(raw)

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
        if not DEMO_TOKEN:
            self._fail(503, "DEPENDENCY_UNAVAILABLE",
                       "服务未配置访问口令，已按失败即关闭处理。")
            return False
        if DEMO_TOKEN not in self._candidates():
            self._fail(401, "APPROVAL_UNTRUSTED", "凭证无效。")
            return False
        return True

    def _admin_ok(self):
        if not ADMIN_KEY:
            self._fail(404, "NOT_FOUND", "该端点未启用。")
            return False
        if (self.headers.get("X-Admin-Key") or "") != ADMIN_KEY:
            self._fail(401, "APPROVAL_UNTRUSTED", "管理凭证无效。")
            return False
        return True

    def log_message(self, fmt, *a):  # 请求日志进 stderr，不进响应
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # ---- 路由

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/health"):
            # 健康检查不鉴权，但不泄露演示数据细节。
            # 只给配置指纹（不可逆），用来判断"环境变量到底进没进来"，
            # 不打印明文口令。
            self._send(200, {
                "ok": True,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mock_version": getattr(mock, "SERVER_VERSION", "?"),
                "tool_count": len(mock.TOOLS),
                "auth_required": True,
                "auth_configured": bool(DEMO_TOKEN),
                "admin_configured": bool(ADMIN_KEY),
                "token_sha8": _fp(DEMO_TOKEN),
                "admin_sha8": _fp(ADMIN_KEY),
                "bind": os.environ.get("BIND", "127.0.0.1"),
                "port": os.environ.get("PORT", "8790"),
                "starts_at": _STATS["starts_at"],
                "calls": _STATS["calls"],
                "errors": _STATS["errors"],
            })
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


def main():
    bind = os.environ.get("BIND", "127.0.0.1")
    port = int(os.environ.get("PORT", "8790"))
    _STATS["starts_at"] = _now()

    if not DEMO_TOKEN:
        sys.stderr.write("WARN: CHECKD_DEMO_TOKEN 未设置，业务端点将全部拒答（fail-closed）。\n")
    if not ADMIN_KEY:
        sys.stderr.write("WARN: CHECKD_ADMIN_KEY 未设置，reset/evidence 已禁用。\n")
    if bind not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("WARN: 绑定 %s 非回环。仅限平台沙箱；本机运行请用默认值。\n" % bind)

    srv = ThreadingHTTPServer((bind, port), Handler)
    sys.stderr.write("%s %s on %s:%d (tools=%d)\n"
                     % (SERVICE_NAME, SERVICE_VERSION, bind, port, len(mock.TOOLS)))
    srv.serve_forever()


if __name__ == "__main__":
    main()
