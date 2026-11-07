# 比赛版工具服务 · 运行说明（M1）

本目录有**两样东西**，别混：

| 文件 | 是什么 | 状态 |
|---|---|---|
| `errors.py` + `tests/test_errors.py` | 契约 §4.1 的 21 码 HTTP 映射与失败信封，34 条单测 | 已交付（PR #10） |
| `app.py` | 把 `tools/m1-mock/tool_server.py` 的 7 个工具暴露成 HTTP / MCP 服务 | 本次比赛版新增 |

工具实现只有一份，在 `tools/m1-mock/tool_server.py`。`app.py` 只做传输层：
鉴权、JSON、HTTP 状态、证据记录。**不要在 app.py 里加第二套业务逻辑。**

---

## 一、本机运行（只绑回环）

```bash
cd tools/m1-server
CHECKD_DEMO_TOKEN=<随机口令> CHECKD_ADMIN_KEY=<随机管理口令> PORT=8790 python app.py
```

默认 `BIND=127.0.0.1`，**只有本机能连**。这是刻意设计：默认值就是安全边界，
要让本机之外可访问必须显式给 `BIND`，且会打警告。

## 二、部署到平台沙箱

服务跑在平台沙箱，用户电脑不开任何端口：

```bash
python work/m1/build_deploy.py            # 组装 work/deploy/checkd-tools
```

部署时启动命令：

```
BIND=0.0.0.0 CHECKD_DEMO_TOKEN=<口令> CHECKD_ADMIN_KEY=<管理口令> PYTHONUNBUFFERED=1 python3 app.py
```

沙箱内绑 `0.0.0.0` 是平台要求（反向代理回源）。**沙箱外不要这么跑。**

### 2.1 发布前必须重建并核对（2026-11-08 定稿）

部署目录是**构建产物，不是真源**。仓库改了没重建就发布 = 带旧代码上线；
而且 `/health` 的精简输出（4 键）**分辨不出上线件是哪个 commit**，事后没法从公网反推。
2026-11-08 02:05 实测过：部署目录里 `snapshot_store.py` 落后两个提交（37,506 B，缺 TTL 两次修改），
其余 11 个文件与 HEAD 逐字节相同——只有重建才会对上。

每次发布前：

```bash
python work/m1/build_deploy.py               # 脚本先 rmtree 再重拼，可反复跑
cat work/deploy/checkd-tools/MANIFEST.json   # 逐文件 bytes / sha256
```

核对判据（任一条不满足就别发布）——**比 LF 归一后的 sha256，不比字节数**：

```bash
cd work/repo/check-d
for f in tools/m1-server/app.py tools/m1-server/errors.py \
         tools/m1-server/snapshot_store.py tools/m1-server/explanations_filter.py \
         tools/m1-mock/tool_server.py; do
  echo "$f wt=$(tr -d '\r' < $f | sha256sum | cut -c1-16) head=$(git cat-file blob HEAD:$f | sha256sum | cut -c1-16)"
done
```

- 每行两侧 16 位前缀必须相同；MANIFEST 的 `sha256` 应等于工作树文件的 sha256。
- **为什么用 `tr -d '\r'`**：Windows 工作树的 `errors.py` 是 CRLF（8,137 B），HEAD blob 是 LF（7,943 B），
  行数同为 194、LF 归一后 sha256 完全相同。直接比字节数会误判成「改过」。
- 参照值：`snapshot_store.py` 在 11-07 之后应为 **39,993 B**。若 MANIFEST 里还是 **37,506 B**，
  说明目录还是 `40e801f` 时代的旧件，TTL 两次修改都不在里面。

## 三、环境变量

| 变量 | 作用 | 不设的后果 |
|---|---|---|
| `PORT` | 监听端口 | 默认 8790 |
| `BIND` | 绑定地址 | 默认 `127.0.0.1`；部署时必须显式 `0.0.0.0` |
| `CHECKD_DEMO_TOKEN` | 业务口令 | **未设置则全部业务端点返回 503**（fail-closed） |
| `CHECKD_ADMIN_KEY` | 管理口令 | 未设置则 reset / evidence / state / demo confirm 整体禁用 |
| `CHECKD_DEMO_USER` | 口令通过后使用的演示身份 | 默认 `demo-token-alice` |

口令通过后，内部固定以演示用户身份调用工具。mock 自带的
`demo-token-alice/bob` 不出现在任何对外文案里。

## 四、端点

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/health` | 无 | 存活检查；只给配置指纹（不可逆），不给数据 |
| GET | `/tools` | 业务口令 | 7 个工具的 schema，供 Agent 配置 |
| POST | `/tools/<name>` | 业务口令 | 直接调用；body 是**四键请求信封**（见下） |
| POST | `/mcp` | 业务口令 | MCP JSON-RPC（无状态），供平台挂成工具 |
| GET | `/evidence` | 管理口令 | 工具调用记录：工具名/参数/结果/耗时 |
| GET | `/state` | 管理口令 | 演示数据规模与写入计数 |
| POST | `/admin/reset` | 管理口令 | 回到初始 fixture（重新加载模块） |
| POST | `/demo/confirm` | 管理口令 | **演示确认**，见下节 |

鉴权头：优先 `X-Demo-Token: <口令>`，也接受 `Authorization: Bearer <口令>`。

**`POST /tools/<name>` 的 body 必须是四键请求信封，不是裸的入参对象**（2026-11-05 修正，M3 独立复验指出的唯一 FAIL）：

```json
{
  "api_version": "checkd.agent.v1",
  "request_id": "req_0001",
  "timezone": "Asia/Shanghai",
  "input": { }
}
```

工具自己的参数全部放在 `input` 里。漏了信封会直接 400 `INVALID_ARGUMENT`。
`api_version` 的合法值只有 `checkd.agent.v1` 一个（写成 `1.0` 会报 `unsupported`）。
`GET /tools` 返回的 schema 是按这个形状写的，照 schema 填不会错。

**为什么优先 X-Demo-Token**：实测平台反代会注入/覆盖 `Authorization`，
只认这个头会永远 401。两个来源都收，逐个比对。

## 五、`/demo/confirm` 的定性（必须照这个说）

这是**演示确认**，不是真实审批。

- 真实审批链（G5）要求独立审批服务、私钥签发、撤销机制、外部确认页 —— **全部 BLOCKED**。
- 本端点只把一条待审记录置为 approved，让纵向闭环能在隔离演示数据上真的跑完。
- 响应里固定带 `"demo": true`，任何转述都必须保留这个标记。
- **不得**把它写成"审批通过"或"生产可用"。

## 六、接进 WorkBuddy（MCP）

`%USERPROFILE%\.workbuddy\mcp.json` 加一条：

```json
{
  "mcpServers": {
    "checkd-agent-tools": {
      "type": "http",
      "url": "https://checkd-agent-tools.app.workbuddy.host/mcp?sharecode=EH5AAYWLOT",
      "headers": {
        "X-Demo-Token": "<业务口令>"
      }
    }
  }
}
```

然后：连接器 → 自定义连接器 → MCP 服务管理 → 找到该条目 → **信任** → 新建任务对话。

信任是界面操作，必须由账号所有者在客户端点一次。这一步脚本做不了。

## 七、边界

- 无数据库。数据全在进程内存，重启即回到初始 fixture。
- 不支持 Dart，M2 引擎**无法**部署到这里；`generate_schedule` 当前用的是 mock 调度器。
- 单一实现、单一进程、串行可接受；无限流、无审计日志、无 WAF。
- 演示数据仅隔离环境，不写 Production / Dev。

## 八、快照库的两个身份（必须分开说，别混）

`tools/m1-server` 接快照库时涉及**两个不同的数据库身份**，它们的权限完全不同，
验收结论里混着写就是错的说法。

| 身份 | 谁在用 | 权限 | 能干什么 |
|---|---|---|---|
| `postgres`（库所有者） | M0 在 Supabase SQL Editor 执行 migration | 超级权限，`revoke` 对它无效 | DDL、建表、改约束 |
| `checkd_demo_app`（nologin 角色） | 应用运行时连接 | 只 `usage` + 四张表的 DML，**无** `create`/`delete`/`truncate` | 读写业务表，不能改结构 |

三条容易说错的：

1. **用 postgres 的 DSN 跑应用，等于最小权限没生效。** migration 里的 `revoke`/
   `grant` 对一个库所有者角色不构成限制，它本来就能做任何事。这种情况下权限段是
   **文档性约束**，不是运行时约束 —— 验收结论不得写「已按最小权限运行」。
2. **preflight 报「应用角色有权限」≠「应用真的用该角色连」。** 前者只证明权限位存在，
   后者要拿 `checkd_demo_app` 的 DSN 真连一次才能证明。两件事分开写。
3. **要真验最小权限**，得给 `checkd_demo_app` 开 login + 密码，用它的 DSN 跑一遍
   preflight（`CHECKD_DEMO_DSN` 换成该角色）。这一步需要 M0 单独授权，
   因为要新增一个有 login 的角色。

## 九、preflight 的只读边界（2026-11-07 事故后定稿）

`preflight_snapshot_db.py` **默认只读**：连接层 `default_transaction_read_only=on`、
语句守卫只放行 select/with/show/explain、连上后真去 `create temp table` 验证写被拒
（SQLSTATE 25006）。

写入型行为测试（唯一索引 / 复合 FK / TTL 实拍）走另一条命令，且必须显式授权：

```
python tools/m1-server/preflight_snapshot_db.py --behavior-tests --i-have-m0-approval
python tools/m1-server/preflight_snapshot_db.py --apply          --i-have-m0-approval
```

缺 `--i-have-m0-approval` 一律 `REFUSED` + exit 3。**不要**用「文件名/参数名看起来是只读」
当证据 —— 2026-11-07 就出过一版默认路径会 INSERT/DELETE 的脚本，注释写着只读。

### 9.1 唯一命令（macOS / Linux，M0 本机）

```bash
cd <仓库根> && CHECKD_DEMO_DSN="$CHECKD_DEMO_DSN" \
  python3 tools/m1-server/preflight_snapshot_db.py
```

- 结果路径**固定且与 cwd 无关**：先找 `<仓库>/../work/m1/`（本机工作区布局），
  干净 clone 下落到 `<仓库根>/preflight_snapshot_db_result.json`；脚本开头会打印
  `结果文件: <绝对路径>`，跑完照它取文件。要别的路径加 `--out /abs/path.json`。
- 退出码：`0` 无 FAIL／`1` 有 FAIL／`2` 拿不到库或缺 psycopg3／
  `3` REFUSED（写路径缺授权）／`4` BLOCKED（只读证明失败，后续检查一律未执行）。
- 只读证明失败（连接非只读，或探针临时表居然建成功）**立即 exit 4**，
  不再跑 schema/权限检查，也不会去「清理」探针表（只回滚事务）。

### 9.2 干净 Mac 首次安装与运行（2026-11-08 修正版）

适用：机器上没装过 psycopg3、也没建过虚拟环境。**逐行照抄，别改顺序。**

```bash
git clone -b demo/competition-11010 https://github.com/Tony2215/check-d.git
cd check-d
git rev-parse HEAD                    # 期望 1a426717… 之后的最新 head
python3 -m venv .venv
. .venv/bin/activate                  # 之后所有命令都在这个 venv 里
python -m pip install -U pip 'psycopg[binary]'
env -u DATABASE_URL CHECKD_DEMO_DSN='postgresql://用户:口令@主机:5432/库名' \
  python tools/m1-server/preflight_snapshot_db.py
cat ./preflight_snapshot_db_result.json
```

三处与 2026-11-08 之前的旧命令不同，都是 11-08 复核后改的：

1. **用 venv，不用 `pip install --user`**。macOS 上 python.org / Homebrew 的 3.11+
   是 PEP 668 externally-managed 环境，`--user` 会被 `error: externally-managed-environment`
   直接拒掉。venv 是唯一不需要动系统 Python 的做法。
2. **命令前加 `env -u DATABASE_URL`**。脚本取 DSN 的顺序是
   `CHECKD_DEMO_DSN` → `DATABASE_URL`（见 `preflight_snapshot_db.py` 的 `main()`）。
   `CHECKD_DEMO_DSN` 非空时不会读 `DATABASE_URL`，所以这是**纵深防御**、不是修 bug：
   万一 DSN 那串打错或为空，脚本会默默回退到你 shell 里别的 `DATABASE_URL`（可能是正式库）。
   剥掉它，就不会连错库。**别指望脚本自己发现连错了。**
3. **`psycopg[binary]` 不锁版本**。现网 3.3.8 已提供 cp315（Python 3.15）macOS 轮子，
   3.9–3.15 都装得上；锁版本反而会在新解释器上找不到匹配轮子。

跑完看三样：退出码（`0` 才算无 FAIL）、`结果文件:` 打印的绝对路径、JSON 里只出现
host/dbname/user 三项（**不含 DSN**）。退出码 2 = 没拿到库或缺 psycopg3，不是代码失败。

