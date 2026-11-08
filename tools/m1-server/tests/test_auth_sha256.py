"""方案 A：服务端只存口令指纹（SHA-256）、不存明文时的鉴权行为。

背景见 app.py 中 DEMO_TOKEN_SHA256 处的注释。这里钉住五件事：

  1. **只配指纹也能工作** —— 否则方案 A 等于没实现，M0 的连接器照样 401。
  2. **错误口令仍然 401** —— 不能因为多了一条比对分支就顺手放行。
  3. **两者都不配仍 503 fail-closed** —— 安全默认不能因为这次改动变松，
     这是整个文件的第 3 条铁律。
  4. **明文模式不受影响** —— 回归。现网仍跑明文，改坏了就是又一次 503。
  5. **指纹不落进任何响应** —— /health 里既不能出现明文，也不能出现指纹。

真起 HTTP 进程来测，不 mock。理由：鉴权是传输层行为，mock 掉 Handler
就等于只测了自己写的 if。
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(HERE)
APP = os.path.join(SRC_DIR, "app.py")
# app.py 里 `import tool_server`，而 tool_server.py 在 tools/m1-mock/。
# 部署时 build_deploy.py 会把两者拷进同一目录，本地跑就得靠 PYTHONPATH 补。
MOCK_DIR = os.path.abspath(os.path.join(SRC_DIR, "..", "m1-mock"))

_RELEVANT_ENV = ("CHECKD_DEMO_TOKEN", "CHECKD_DEMO_TOKEN_SHA256",
                 "CHECKD_ADMIN_KEY", "CHECKD_HEALTH_MINIMAL")

# 哨兵：区分「没传这个参数」与「显式传 None」
_OMITTED = object()


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _Server:
    """起一个真实的 app.py 子进程，用完杀掉。"""

    def __init__(self, **env_extra):
        self.port = _free_port()
        env = dict(os.environ)
        for k in _RELEVANT_ENV:
            env.pop(k, None)
        env.update({"PORT": str(self.port), "BIND": "127.0.0.1",
                    "CHECKD_SNAPSHOT_MODE": "off"})
        env["PYTHONPATH"] = os.pathsep.join(
            [MOCK_DIR] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.update(env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, APP], env=env, cwd=SRC_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("app.py 启动即退出，exit=%s" % self.proc.returncode)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("20 秒内没起来（port=%d）" % self.port)

    def call(self, token=_OMITTED, path="/tools/list_tasks"):
        payload = {
            "api_version": "checkd.agent.v1",
            "request_id": "t-auth-1",
            "timezone": "Asia/Shanghai",
            "input": {"date_range": {"start": "2026-11-08", "end": "2026-11-08"}},
        }
        headers = {"Content-Type": "application/json"}
        if token is not _OMITTED:
            headers["X-Demo-Token"] = token
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw}

    def health(self):
        url = "http://127.0.0.1:%d/health" % self.port
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _sha(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


class TestSha256OnlyMode(unittest.TestCase):
    """只配 CHECKD_DEMO_TOKEN_SHA256，一条明文都没有。"""

    TOKEN = "m0-self-held-9f2c7a41b8"

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server(CHECKD_DEMO_TOKEN_SHA256=_sha(cls.TOKEN),
                          CHECKD_HEALTH_MINIMAL="0")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_correct_token_gets_200_success(self):
        code, body = self.srv.call(self.TOKEN)
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("status"), "success")
        self.assertIn("tasks", body.get("data") or {})

    def test_wrong_token_is_invalid_401(self):
        code, body = self.srv.call("obviously-wrong")
        self.assertEqual(code, 401)
        self.assertEqual(body["error"]["details"][0]["reason"], "invalid")

    def test_no_token_is_401_not_503(self):
        """没配明文不等于没配鉴权 —— 必须是 401（缺凭证），不能退化成 503。"""
        code, _ = self.srv.call()
        self.assertEqual(code, 401)

    def test_health_auth_configured_true(self):
        self.assertTrue(self.srv.health().get("auth_configured"))

    def test_health_leaks_neither_plaintext_nor_digest(self):
        blob = json.dumps(self.srv.health(), ensure_ascii=False)
        self.assertNotIn(self.TOKEN, blob)
        self.assertNotIn(_sha(self.TOKEN), blob)


class TestSha256MultipleCallers(unittest.TestCase):
    """逗号分隔多个指纹：各调用方独立轮换，一个失效不影响另一个。"""

    A = "caller-a-token-111"
    B = "caller-b-token-222"

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server(
            CHECKD_DEMO_TOKEN_SHA256="%s , %s" % (_sha(cls.A).upper(), _sha(cls.B)))

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_first_caller_ok(self):
        self.assertEqual(self.srv.call(self.A)[0], 200)

    def test_second_caller_ok(self):
        self.assertEqual(self.srv.call(self.B)[0], 200)

    def test_case_and_space_tolerant(self):
        """环境变量里写成大写加空格也要认 —— 否则运维稍一手抖就 401。"""
        self.assertEqual(self.srv.call(self.A)[0], 200)

    def test_third_party_still_rejected(self):
        self.assertEqual(self.srv.call("caller-c-token-333")[0], 401)


class TestBothModesCoexist(unittest.TestCase):
    """明文 + 指纹并存：两者都该通过（现网就是这种过渡态）。"""

    PLAIN = "legacy-plain-token"
    HASHED = "new-hashed-token"

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server(CHECKD_DEMO_TOKEN=cls.PLAIN,
                          CHECKD_DEMO_TOKEN_SHA256=_sha(cls.HASHED))

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_plaintext_mode_still_works(self):
        self.assertEqual(self.srv.call(self.PLAIN)[0], 200)

    def test_sha256_mode_works(self):
        self.assertEqual(self.srv.call(self.HASHED)[0], 200)

    def test_neither_of_the_two_matches(self):
        self.assertEqual(self.srv.call("some-other-token")[0], 401)


class TestFailClosed(unittest.TestCase):
    """两个变量都不配 → 503。这次改动不许把安全默认放松。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_503_when_nothing_configured(self):
        code, body = self.srv.call("anything")
        self.assertEqual(code, 503, body)
        self.assertEqual(body["error"]["code"], "DEPENDENCY_UNAVAILABLE")

    def test_empty_hash_list_does_not_count_as_configured(self):
        """"   " / "," 这种空串不能当成"配了指纹"。"""
        srv = _Server(CHECKD_DEMO_TOKEN_SHA256=" , , ")
        try:
            code, _ = srv.call("anything")
            self.assertEqual(code, 503)
        finally:
            srv.stop()


class TestDigestParsing(unittest.TestCase):
    """不改进程、只校验解析规则本身。"""

    def _parse(self, raw):
        return tuple(x.strip().lower() for x in raw.split(",") if x.strip())

    def test_strips_and_lowercases(self):
        h = _sha("x")
        self.assertEqual(self._parse("  %s  " % h.upper()), (h,))

    def test_drops_empty_pieces(self):
        h1, h2 = _sha("a"), _sha("b")
        self.assertEqual(self._parse("%s,,%s," % (h1, h2)), (h1, h2))


if __name__ == "__main__":
    unittest.main()
