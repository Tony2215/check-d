#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REAL-WRITE 最小工具的隔离测试。

**不连任何真实数据库、不使用任何真实凭证。** 全部靠注入假传输驱动。

测试重点不是「能不能改成功」，而是**三道闸是否真的挡得住**：

1. 未显式确认 —— 不发出任何请求；
2. 旧名称不匹配 —— 不覆盖、报冲突；
3. 只发 `name` 一个字段、只对一行、只走 PATCH。

这组测试通过 ≠ REAL-WRITE 可用。权限、RLS 实际行为、App 同步都只能由
M0 在受控环境下验，本文给不出那些结论。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import real_read as rr   # noqa: E402
import real_write as rw  # noqa: E402

TASK_ID = "2116742f-84b8-417b-bc0c-b6fc59ba3704"
OTHER_ID = "ffffffff-1111-2222-3333-444444444444"
OLD = "英语单词"
NEW = "英语单词·测试"

BASE = "https://example.supabase.co"
ANON = "anon-key"
JWT = "fake-jwt"


class Rec:
    """记录假传输收到的请求，便于断言「请求长什么样」。"""

    def __init__(self, status=200, rows=None, raw=None):
        self.status = status
        self.rows = rows if rows is not None else []
        self.raw = raw
        self.calls = []

    def __call__(self, url, headers, timeout, body):
        self.calls.append({"url": url, "headers": dict(headers),
                           "body": json.loads(body.decode("utf-8"))})
        if self.raw is not None:
            return self.status, self.raw
        return self.status, json.dumps(self.rows, ensure_ascii=False).encode("utf-8")

    @property
    def called(self):
        return len(self.calls) > 0

    @property
    def one(self):
        assert len(self.calls) == 1, "期望恰好一次请求，实际 %d 次" % len(self.calls)
        return self.calls[0]


def writer(transport, token=JWT):
    return rw.RealWrite(BASE, ANON, token, transport=transport)


def ok_row(name=NEW, uid="user-uuid"):
    return [{"id": TASK_ID, "name": name, "user_id": uid,
             "updated_at": "2026-11-10T12:40:00+00:00"}]


def do(w, **kw):
    args = dict(task_id=TASK_ID, new_name=NEW, expected_old_name=OLD, confirm=True)
    args.update(kw)
    return w.update_task_name(**args)


# --------------------------------------------------------------------------
# 一、闸门：不该发请求的时候一次都不能发
# --------------------------------------------------------------------------

class TestGates(unittest.TestCase):

    def test_without_confirm_no_request_is_sent(self):
        t = Rec(rows=ok_row())
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t), confirm=False)
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertFalse(t.called, "未确认时不得发出任何请求")

    def test_confirm_must_be_literal_true(self):
        """`confirm="true"` 这种字符串不算确认 —— 布尔判断不能靠真值。"""
        t = Rec(rows=ok_row())
        with self.assertRaises(rr.RealReadError):
            do(writer(t), confirm="true")
        self.assertFalse(t.called)

    def test_missing_token_refuses_before_network(self):
        t = Rec(rows=ok_row())
        with self.assertRaises(rr.RealReadError) as cm:
            rw.RealWrite(BASE, ANON, "", transport=t)
        self.assertEqual(cm.exception.code, "AUTH_REQUIRED")
        self.assertFalse(t.called)

    def test_invalid_uuid(self):
        t = Rec(rows=ok_row())
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t), task_id="not-a-uuid")
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertFalse(t.called)

    def test_name_length_bounds(self):
        """与库里的 check 约束一致：1–100 字符。"""
        t = Rec(rows=ok_row())
        for bad in ("", "x" * 101):
            with self.assertRaises(rr.RealReadError) as cm:
                do(writer(t), new_name=bad)
            self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertFalse(t.called)
        # 边界值必须放行
        t2 = Rec(rows=ok_row(name="x" * 100))
        do(writer(t2), new_name="x" * 100)
        self.assertTrue(t2.called)

    def test_same_name_rejected(self):
        """同值 PATCH 会推 updated_at，从而触发 App 一次无意义的下载比对。"""
        t = Rec(rows=ok_row())
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t), new_name=OLD)
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertFalse(t.called)


# --------------------------------------------------------------------------
# 二、乐观锁：条件更新的形状必须对
# --------------------------------------------------------------------------

class TestOptimisticLock(unittest.TestCase):

    def test_where_contains_id_and_old_name(self):
        """核心设计：用「要改的那个值本身」当条件，替代 App 不带的 sync_version。"""
        t = Rec(rows=ok_row())
        do(writer(t))
        url = t.one["url"]
        self.assertIn("/rest/v1/tasks", url)
        self.assertIn("id=eq." + TASK_ID, url)
        self.assertIn("name=eq.", url)
        self.assertIn("deleted_at=is.null", url, "软删的行不得被改")

    def test_body_carries_only_name(self):
        """只发白名单里的一个字段。多带任何一个列都会扩权。"""
        t = Rec(rows=ok_row())
        do(writer(t))
        self.assertEqual(list(t.one["body"].keys()), ["name"])
        self.assertEqual(t.one["body"]["name"], NEW)

    def test_old_name_is_url_encoded(self):
        """旧名里的保留字符必须处理两层，否则能把 WHERE 改成别的条件。

        M3 2026-11-10 20:26 指出：只做百分号编码不够 —— PostgREST 先 URL 解码
        再解析 filter，`%2C` 解回逗号仍会被当条件分隔符。所以还要双引号包裹。
        """
        t = Rec(rows=ok_row(name="物理实验"))
        nasty = "物理,实验(第2组):测试"
        do(writer(t), expected_old_name=nasty, new_name="物理实验")
        url = t.one["url"]
        self.assertNotIn(nasty, url, "原样出现说明没编码")
        self.assertIn("%2C", url, "逗号应被百分号编码")
        self.assertIn("%28", url, "左括号应被百分号编码")
        self.assertIn("%22", url, "值还应被双引号包裹（PostgREST 层的要求）")

    def test_quote_only_when_needed(self):
        """`_q` 只做百分号编码；仅当值含 PostgREST 保留字符时才加双引号。

        **这条断言是被真实 PostgREST 打回来的**（2026-11-10 21:17）：
        原来无条件加引号，`name=eq.%22CheckD-…%22` 解回 '"CheckD-写入测试-A"'，
        引号成了值的一部分，与库里的 'CheckD-写入测试-A' 不等 → 匹配 0 行
        → 真实改名失败（trace 9202678e3a3348fc，SNAPSHOT_CONFLICT）。
        所以「无条件包裹」这个设计假设是错的，测试当时把它固化成了「正确」。

        保留的部分：值含 , . ( ) : " 时，引号仍是必要的（M3 20:26 的指正成立）。
        """
        # 普通值（含中文）：只编码，不包引号 —— 这样才与库里的值相等
        self.assertEqual(rw._q("abc"), "abc")
        self.assertEqual(rw._q("CheckD-写入测试-A"),
                         "CheckD-%E5%86%99%E5%85%A5%E6%B5%8B%E8%AF%95-A")
        self.assertNotIn("%22", rw._q("CheckD-写入测试-A"))

        # 含保留字符：必须包引号，否则逗号会被当条件分隔符
        self.assertTrue(rw._q("a,b").startswith("%22"))
        for ch in ",.():":
            self.assertIn("%22", rw._q("x%sy" % ch), "含 %r 时应包引号" % ch)

        # 值内的双引号要转义成 \" 再编码
        self.assertEqual(rw._q('a"b'), "%22a%5C%22b%22")
        # 反斜杠先转义成 \\
        self.assertEqual(rw._q("a\\b"), "%22a%5C%5Cb%22")

    def test_user_id_never_sent_by_caller(self):
        """归属由 RLS 的 auth.uid() 判，客户端不得指定身份。"""
        t = Rec(rows=ok_row())
        do(writer(t))
        self.assertNotIn("user_id", t.one["body"])
        self.assertNotIn("user_id=eq", t.one["url"])

    def test_updated_at_never_written(self):
        """updated_at 由 set_updated_at() 触发器推，工具层写它会被覆盖，索性不写。"""
        t = Rec(rows=ok_row())
        do(writer(t))
        self.assertNotIn("updated_at", t.one["body"])

    def test_prefer_return_representation(self):
        t = Rec(rows=ok_row())
        do(writer(t))
        self.assertIn("return=representation", t.one["headers"]["Prefer"],
                      "不回行就无法做回读核验")


# --------------------------------------------------------------------------
# 三、失败语义：每一种都要落到契约码上
# --------------------------------------------------------------------------

class TestFailureMapping(unittest.TestCase):

    def test_zero_rows_is_conflict(self):
        """0 行 = 不存在 / 不属于该用户 / 名称已被改。故意不区分，避免探测。"""
        t = Rec(rows=[])
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t))
        self.assertEqual(cm.exception.code, "SNAPSHOT_CONFLICT")

    def test_multi_row_is_internal(self):
        """id 是主键，命中多行说明条件被改写了，必须中止。"""
        t = Rec(rows=[{"id": TASK_ID, "name": NEW, "user_id": "u"},
                      {"id": OTHER_ID, "name": NEW, "user_id": "u"}])
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t))
        self.assertEqual(cm.exception.code, "INTERNAL_ERROR")

    def test_401_403_forbidden(self):
        for st in (401, 403):
            with self.assertRaises(rr.RealReadError) as cm:
                do(writer(Rec(status=st, raw=b'{"message":"denied"}')))
            self.assertEqual(cm.exception.code, "FORBIDDEN", "http %d" % st)

    def test_400_is_invalid_argument_with_detail(self):
        """400 意味着我对 schema 的假设错了，必须原样带出来。"""
        raw = b'{"code":"42703","message":"column tasks.name does not exist"}'
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(Rec(status=400, raw=raw)))
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertTrue(cm.exception.details, "必须带上游原因，否则查不出是 schema 猜错")
        self.assertIn("does not exist", json.dumps(cm.exception.details, ensure_ascii=False))

    def test_5xx_dependency_unavailable(self):
        for st in (500, 502, 503):
            with self.assertRaises(rr.RealReadError) as cm:
                do(writer(Rec(status=st, raw=b"oops")))
            self.assertEqual(cm.exception.code, "DEPENDENCY_UNAVAILABLE", "http %d" % st)

    def test_readback_mismatch(self):
        """库返回的行 name 不等于新值 —— 必须报不匹配，不能当成功。"""
        t = Rec(rows=ok_row(name="别的名字"))
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t))
        self.assertEqual(cm.exception.code, "READBACK_MISMATCH")

    def test_readback_without_user_id_rejected(self):
        """RLS 下不该缺 user_id。缺了说明策略可能被改松，工具层先挡。"""
        t = Rec(rows=ok_row(uid=None))
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(t))
        self.assertEqual(cm.exception.code, "READBACK_MISMATCH")

    def test_bad_json(self):
        with self.assertRaises(rr.RealReadError) as cm:
            do(writer(Rec(status=200, raw=b"<html>not json</html>")))
        self.assertEqual(cm.exception.code, "INTERNAL_ERROR")

    def test_no_new_error_codes(self):
        """全部错误码必须是契约 21 码闭集内的既有码。"""
        import errors
        used = {"INVALID_ARGUMENT", "AUTH_REQUIRED", "FORBIDDEN",
                "SNAPSHOT_CONFLICT", "DEPENDENCY_UNAVAILABLE",
                "READBACK_MISMATCH", "INTERNAL_ERROR"}
        for code in used:
            self.assertIn(code, errors.HTTP_BY_CODE, "%s 不在闭集里" % code)


# --------------------------------------------------------------------------
# 四、成功路径
# --------------------------------------------------------------------------

class TestSuccess(unittest.TestCase):

    def test_happy_path(self):
        t = Rec(rows=ok_row())
        out = do(writer(t))
        self.assertTrue(out["changed"])
        self.assertEqual(out["old_name"], OLD)
        self.assertEqual(out["new_name"], NEW)
        self.assertEqual(out["readback"]["name"], NEW)
        self.assertIn("updated_at", out, "触发器推的时间戳要报出来，让验收方看到副作用")
        self.assertEqual(len(t.calls), 1, "成功路径应当只有一次请求（回读靠 return=representation）")

    def test_no_token_in_returned_payload(self):
        """返回值会被写进证据与邮件，绝不能把令牌带出去。"""
        t = Rec(rows=ok_row())
        out = do(writer(t))
        self.assertNotIn(JWT, json.dumps(out, ensure_ascii=False))

    def test_no_token_in_request_log(self):
        """请求头里必须有 Bearer，但只用于断言——不该出现在返回值/异常里。"""
        t = Rec(rows=ok_row())
        do(writer(t))
        self.assertEqual(t.one["headers"]["Authorization"], "Bearer " + JWT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
