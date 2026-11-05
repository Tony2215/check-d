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
| POST | `/tools/<name>` | 业务口令 | 直接调用；body 是工具入参对象 |
| POST | `/mcp` | 业务口令 | MCP JSON-RPC（无状态），供平台挂成工具 |
| GET | `/evidence` | 管理口令 | 工具调用记录：工具名/参数/结果/耗时 |
| GET | `/state` | 管理口令 | 演示数据规模与写入计数 |
| POST | `/admin/reset` | 管理口令 | 回到初始 fixture（重新加载模块） |
| POST | `/demo/confirm` | 管理口令 | **演示确认**，见下节 |

鉴权头：优先 `X-Demo-Token: <口令>`，也接受 `Authorization: Bearer <口令>`。

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
