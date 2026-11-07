#!/usr/bin/env python
"""checkd 快照库预检 —— 拿到 DSN 后一条命令出结果。

用法：
    CHECKD_DEMO_DSN="postgresql://..." python work/m1/preflight_snapshot_db.py
    CHECKD_DEMO_DSN="..." python work/m1/preflight_snapshot_db.py --apply   # 顺带跑 migration

设计原则（对应 M0 D 条「证明不了就标 BLOCKED」）：
  1. 每条检查都给出「查到了什么」，不是只给 PASS —— 失败时能直接定位。
  2. 只读检查与写入检查分开。写入检查最后跑，且自己清理自己造的数据。
  3. 任何一条 FAIL 都不影响后面的检查继续跑，最后一次性汇总 —— 别在第一条就退出，
     否则要来回好几轮才知道全貌。
  4. 不打印 DSN 原文（含口令），只打印 host 与库名。

不做什么：
  - 不建库、不 drop 任何东西（清库是不可逆动作，不在预检里做）。
  - 不假通过。拿不到库就 exit 2，输出 BLOCKED，而不是 exit 0。
"""

import argparse
import json
import os
import re
import sys
import uuid

MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "db", "0001_snapshot_r3.sql")

RESULTS = []


def rec(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print("  %-42s %s  %s" % (name, "PASS" if ok else "FAIL", str(detail)[:120]))


def safe_dsn(dsn):
    """只暴露 host / dbname，绝不打印口令。"""
    m = re.match(r"^[a-z+]+://([^:]+):([^@]*)@([^/]+)/([^?\s]+)", dsn)
    if not m:
        return {"host": "(无法解析)", "dbname": "(无法解析)"}
    return {"host": m.group(3), "dbname": m.group(4), "user": m.group(1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="先执行 migration，再检查")
    args = ap.parse_args()

    dsn = os.environ.get("CHECKD_DEMO_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("BLOCKED: 未提供 CHECKD_DEMO_DSN。没有库就不出任何 PASS。")
        return 2

    try:
        import psycopg
    except ImportError:
        print("BLOCKED: 缺 psycopg3（pip install 'psycopg[binary]'）。")
        return 2

    info = safe_dsn(dsn)
    print("目标库: host=%s dbname=%s user=%s" % (info["host"], info["dbname"], info.get("user", "")))

    try:
        # prepare_threshold=None：6543 PgBouncer transaction mode 下不开预编译缓存
        conn = psycopg.connect(dsn, connect_timeout=8, prepare_threshold=None,
                               application_name="checkd-preflight",
                               options="-c statement_timeout=8000 -c lock_timeout=5000")
    except Exception as e:                                   # noqa: BLE001
        print("BLOCKED: 连不上库：%s: %s" % (type(e).__name__, e))
        return 2

    try:
        with conn:
            conn.autocommit = True

            # ---------- 0 连接层 ----------
            row = conn.execute("select current_database(), current_user, version()").fetchone()
            rec("连接 / 库名", True, "%s as %s" % (row[0], row[1]))
            ver = row[2].split()[1] if len(row[2].split()) > 1 else "?"
            rec("服务端版本 >= 14", tuple(int(x) for x in re.findall(r"\d+", ver)[:2]) >= (14, 0), ver)
            pid1 = conn.execute("select pg_backend_pid()").fetchone()[0]
            pid2 = conn.execute("select pg_backend_pid()").fetchone()[0]
            rec("连接后端稳定（非每语句换后端）", pid1 == pid2, "pid=%s/%s" % (pid1, pid2))

            # ---------- 1 工单要求的迁移可执行 ----------
            if args.apply:
                sql = open(MIGRATION, encoding="utf-8").read()
                try:
                    conn.execute(sql)
                    rec("migration R3 首次执行", True, "%d B" % len(sql.encode("utf-8")))
                except Exception as e:                       # noqa: BLE001
                    rec("migration R3 首次执行", False, "%s: %s" % (type(e).__name__, e))
                # 幂等：再跑一遍必须不报错
                try:
                    conn.execute(sql)
                    rec("migration R3 重复执行（幂等）", True, "")
                except Exception as e:                       # noqa: BLE001
                    rec("migration R3 重复执行（幂等）", False, "%s: %s" % (type(e).__name__, e))

            # ---------- 2 结构存在性 ----------
            tabs = [r[0] for r in conn.execute(
                "select table_name from information_schema.tables "
                " where table_schema='checkd_demo' order by 1").fetchall()]
            want_tabs = ["applied_change", "proposal", "snapshot", "snapshot_seq"]
            rec("4 张表齐全", tabs == want_tabs, ",".join(tabs))

            cons = [r[0] for r in conn.execute(
                "select conname from pg_constraint c join pg_namespace n on n.oid=c.connamespace "
                " where n.nspname='checkd_demo' order by 1").fetchall()]
            want = ["applied_change_proposal_fk", "applied_change_proposal_snapshot_fk",
                    "applied_change_proposal_uk", "applied_change_snapshot_fk",
                    "proposal_id_snapshot_uk", "proposal_id_user_uk",
                    "proposal_snapshot_fk", "proposal_ttl_ck",
                    "snapshot_id_user_uk", "snapshot_ttl_ck", "snapshot_version_uk"]
            missing = [c for c in want if c not in cons]
            rec("关键约束 11 条", not missing, "缺: %s" % missing if missing else "%d 条" % len(cons))

            idx = [r[0] for r in conn.execute(
                "select indexname from pg_indexes where schemaname='checkd_demo'").fetchall()]
            rec("latest 唯一索引存在", "snapshot_one_latest" in idx, ",".join(sorted(idx)))
            rec("冗余索引 snapshot_lookup 已删", "snapshot_lookup" not in idx, "")

            gen = conn.execute(
                "select is_generated, generation_expression from information_schema.columns "
                " where table_schema='checkd_demo' and table_name='proposal' "
                " and column_name='base_version'").fetchone()
            rec("base_version 是生成列（P1-2）",
                bool(gen) and gen[0] == "ALWAYS", (gen[1] if gen else "列不存在"))

            # ---------- 3 行为：唯一索引真的挡住第二个 latest ----------
            uid = "preflight-" + uuid.uuid4().hex[:8]
            try:
                with conn.transaction():
                    conn.execute("insert into checkd_demo.snapshot "
                                 "(user_id,date_from,date_to,version,payload,payload_digest,"
                                 " payload_bytes,expires_at) "
                                 "values (%s,'2026-11-08','2026-11-10',1,'{}'::jsonb,'x',2,"
                                 " now()+interval '1 hour')", (uid,))
                    conn.execute("insert into checkd_demo.snapshot "
                                 "(user_id,date_from,date_to,version,payload,payload_digest,"
                                 " payload_bytes,expires_at) "
                                 "values (%s,'2026-11-08','2026-11-10',2,'{}'::jsonb,'x',2,"
                                 " now()+interval '1 hour')", (uid,))
                rec("同区间两个 latest 被拒（唯一索引生效）", False, "两条都插进去了，约束没生效")
            except Exception as e:                           # noqa: BLE001
                ok = "snapshot_one_latest" in str(e) or "duplicate key" in str(e).lower()
                rec("同区间两个 latest 被拒（唯一索引生效）", ok, type(e).__name__)

            # ---------- 4 行为：跨用户复合 FK 真的挡住 ----------
            try:
                with conn.transaction():
                    r = conn.execute(
                        "insert into checkd_demo.snapshot "
                        "(user_id,date_from,date_to,version,payload,payload_digest,payload_bytes,"
                        " expires_at) values (%s,'2026-11-08','2026-11-10',3,'{}'::jsonb,'x',2,"
                        " now()+interval '1 hour') returning snapshot_id", (uid,)).fetchone()
                    sid = r[0]
                    # A 用户名的 proposal 去指 B 用户名的 snapshot → 必须被 23503 拒
                    conn.execute("insert into checkd_demo.proposal "
                                 "(user_id,snapshot_id,snapshot_version,expires_at) "
                                 "values (%s,%s,3, now()+interval '10 min')",
                                 ("other-user-" + uuid.uuid4().hex[:6], sid))
                rec("跨用户 proposal 指向他人 snapshot 被拒", False, "插进去了，P0-1 未生效")
            except Exception as e:                           # noqa: BLE001
                ok = "23503" in str(e) or "foreign key" in str(e).lower()
                rec("跨用户 proposal 指向他人 snapshot 被拒", ok, type(e).__name__)

            # ---------- 5 行为：applied_change 指向不一致被拒（R3 新增 P1）----------
            try:
                with conn.transaction():
                    s1 = conn.execute(
                        "insert into checkd_demo.snapshot (user_id,date_from,date_to,version,"
                        " payload,payload_digest,payload_bytes,expires_at) values "
                        "(%s,'2026-11-08','2026-11-10',4,'{}'::jsonb,'x',2,"
                        " now()+interval '1 hour') returning snapshot_id", (uid,)).fetchone()[0]
                    s2 = conn.execute(
                        "insert into checkd_demo.snapshot (user_id,date_from,date_to,version,"
                        " payload,payload_digest,payload_bytes,expires_at) values "
                        "(%s,'2026-11-08','2026-11-10',5,'{}'::jsonb,'x',2,"
                        " now()+interval '1 hour') returning snapshot_id", (uid,)).fetchone()[0]
                    p = conn.execute(
                        "insert into checkd_demo.proposal "
                        "(user_id,snapshot_id,snapshot_version,expires_at) "
                        "values (%s,%s,4, now()+interval '10 min') returning proposal_id",
                        (uid, s1)).fetchone()[0]
                    # 提案基于 s1，apply 却记 s2 → R3 的复合 FK 必须拒
                    conn.execute("insert into checkd_demo.applied_change "
                                 "(proposal_id,user_id,snapshot_id,result) "
                                 "values (%s,%s,%s,'{}'::jsonb)", (p, uid, s2))
                rec("apply 记录与提案快照不一致被拒（R3 P1）", False, "插进去了，R3 未生效")
            except Exception as e:                           # noqa: BLE001
                ok = "23503" in str(e) or "foreign key" in str(e).lower()
                rec("apply 记录与提案快照不一致被拒（R3 P1）", ok, type(e).__name__)

            # ---------- 6 行为：TTL 早于创建被拒（P2-1）----------
            try:
                with conn.transaction():
                    conn.execute(
                        "insert into checkd_demo.snapshot (user_id,date_from,date_to,version,"
                        " payload,payload_digest,payload_bytes,expires_at) values "
                        "(%s,'2026-11-08','2026-11-10',9,'{}'::jsonb,'x',2, now()-interval '1 min')",
                        (uid,))
                rec("TTL 早于 created_at 被拒（P2-1）", False, "插进去了，约束没生效")
            except Exception as e:                           # noqa: BLE001
                ok = "snapshot_ttl_ck" in str(e) or "check constraint" in str(e).lower()
                rec("TTL 早于 created_at 被拒（P2-1）", ok, type(e).__name__)

            # ---------- 7 清理自己造的数据 ----------
            with conn.transaction():
                conn.execute("delete from checkd_demo.applied_change where user_id like 'preflight-%'")
                conn.execute("delete from checkd_demo.proposal where user_id like 'preflight-%'")
                conn.execute("delete from checkd_demo.snapshot where user_id like 'preflight-%'")
                left = conn.execute(
                    "select (select count(*) from checkd_demo.snapshot where user_id like 'preflight-%')"
                    "     + (select count(*) from checkd_demo.proposal where user_id like 'preflight-%')"
                ).fetchone()[0]
            rec("预检数据已清理", left == 0, "残留 %s 行" % left)

            # ---------- 8 权限（P2-4）----------
            for role in ("anon", "authenticated"):
                exists = conn.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone()
                if not exists:
                    continue
                has = conn.execute(
                    "select has_schema_privilege(%s,'checkd_demo','usage')", (role,)).fetchone()[0]
                rec("%s 无 schema usage（P2-4）" % role, not has, "has_usage=%s" % has)

    finally:
        try:
            conn.close()
        except Exception:                                    # noqa: BLE001
            pass

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print("\n合计 %d/%d PASS" % (passed, total))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "preflight_snapshot_db_result.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "target": info, "passed": passed, "total": total,
            "results": RESULTS}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("结果已写入 %s" % out)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
