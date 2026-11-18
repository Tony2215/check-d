# CheckD 工具服务 · 部署交接（无凭证版）

> 生成时间：2026-11-18 13:2x（+08）｜作者：M1（A）
> **本文件不含任何口令、令牌、数据库地址或密钥值。** 只给"要哪些文件、怎么构建、怎么启动"。
> 接收方若为 agent：文件指纹（第五节）可逐条复核；不要从本文件反推任何凭证。

## 一、应用标识

| 项 | 值 |
|---|---|
| appId | `wbapp_KyTJ0Aifm7eZv27UvdH2dm` |
| 显示名 | CheckD 工具服务 |
| appType | `web` |
| 公网 | `https://checkd-agent-tools.app.workbuddy.host` |
| 分享码 | `EH5AAYWLOT`（**所有请求必须带 `?sharecode=EH5AAYWLOT`**，否则网关 403「链接不完整」，这不是服务故障） |
| 发布形态 | **覆盖式**：同一目录只能有一个 app，重发即覆盖，不能并存第二个 app 或第二个链接 |

## 二、源码与构建

| 项 | 值 |
|---|---|
| 仓库 | `Tony2215/check-d`（fork），分支 `demo/competition-11010` |
| 提交 | `abd68d4` |
| tree | `09545cfd638e6687c53043c0a0aae044b5cb9f10` |
| 远端 head | `64242c26`（tree 与本地同值；通道逐 blob 建树，sha 必然不同，**比 tree 不比 sha**） |
| 构建脚本 | `work/m1/build_deploy.py`（不在仓库内，在工作区） |
| 构建命令 | `python work/m1/build_deploy.py` → 产出 `work/deploy/checkd-tools/` |
| 产出 | 17 个运行文件 + `MANIFEST.json` + 5 个凭证文件（凭证**不入库、不交接**） |

构建脚本自带一道守卫：扫已打包的 `.py`，凡 `import X` 且源码目录存在 `X.py` 的，X 必须也在包里，否则 `FATAL` 退出。历史教训是懒加载模块漏打包，线上表现为四个工具 500。

## 三、运行参数

| 项 | 值 |
|---|---|
| 语言/运行时 | Python 3（容器镜像自带） |
| 安装命令 | **空**（当前平台侧未配置；HTTP/MCP 路径只用标准库，不需要 pip 安装） |
| 启动命令 | `BIND=0.0.0.0 CHECKD_REAL_ENABLED=1 CHECKD_REAL_WRITE=1 python3 app.py` |
| 端口 | `8790`（`app.py` 读 `PORT`，未设时默认 8790） |
| 第三方依赖 | 仅 `psycopg_pool`，且是**延迟导入**（`snapshot_store.py` L135），`CHECKD_SNAPSHOT_MODE=off` 时不需要 |
| 健康检查 | `GET /health?sharecode=EH5AAYWLOT` → `{"ok":true,"scheduler_mode":"MOCK"}` |

**关于环境变量：** 平台发布接口没有 env 字段，变量只能写进启动命令。2026-11-10 之后已改成**进程自己读部署目录下的文件**（环境变量优先、文件兜底），所以上面的启动命令里**一个口令都不出现**。

## 四、配置文件（只给名字与用途，值由部署方生成）

| 文件名 | 用途 | 缺省后果 |
|---|---|---|
| `.token` / `conf_admin.txt` | 演示口令（MOCK 路径的 `X-Demo-Token`）与管理员口令（`X-Admin-Key`） | 缺 → `/mcp` 503 或 `/diag` 401 |
| `conf_rtoken.txt` / `.rtoken` | 真实路径口令（`X-Real-Token`） | 缺 → `/real/mcp` 一律 401 |
| `.surl` | Supabase 根域（不含 `/rest/v1`） | 缺 → REAL 路径报"数据源未配置" |
| `.sanon` | Supabase publishable key | 同上 |

文件必须 **LF 行尾**。Windows 上按文本模式写回会变成 CRLF，读出的值带 `\r`，表现为"口令明明对却一直 401"——这个坑踩过一次。

## 五、文件指纹（复核用）

| 文件 | 字节 | sha256（前 16 位） |
|---|---:|---|
| app.py | 84,301 | `2141ea67e7750e7f` |
| tool_server.py | 69,020 | `dfb0aca09bc3a574` |
| snapshot_store.py | 39,993 | `867acac36e5ee8da` |
| real_read.py | 34,201 | `5bf875b98edd823a` |
| snapshot_tools.py | 18,992 | `c8b7186a963ed66c` |
| proposal_assembly.py | 16,835 | `d543b3039c54e692` |
| real_write.py | 13,845 | `482da9cd7ecc9bcd` |
| errors.py | 8,137 | `29b0804d225a2f59` |
| runtime_marker.py | 6,121 | `1ecc51c4eaebd107` |
| explanations_filter.py | 4,409 | `9b9417e477c0f575` |
| pubspec.yaml | 838 | `3da44caa34b69cd9` |
| lib/.../schedule_contract.dart | 28,510 | `c004e79e5cf936ac` |
| lib/.../schedule_engine.dart | 34,373 | `476e757cdb1331e2` |
| tools/m2-engine/bin/serve.dart | 3,359 | `d31e71f757714fe5` |
| tools/m2-engine/lib/contract_json.dart | 16,259 | `d6e858ea7f62230f` |
| tools/m2-engine/lib/handler.dart | 19,691 | `63a502f301877b1a` |
| tools/m2-engine/lib/validate.dart | 11,230 | `fce8954f9fb51b59` |

完整 64 位 sha256 在部署目录的 `MANIFEST.json` 里，逐文件一一对应。

## 六、更新步骤（原地覆盖，不换链接）

1. 拉 `demo/competition-11010`，核 tree 是否为 `09545cfd`
2. `python work/m1/build_deploy.py` 重新出包（会做依赖闭包自检）
3. 用同一个 appId `wbapp_KyTJ0Aifm7eZv27UvdH2dm`、同一个目录发一次 → **覆盖**
4. 复验三句：`/health` 200；`POST /mcp` 无凭证 401（不是 503）；`POST /real/mcp` 401（不是 404）
5. 记录发布时刻与 `boot_at`（`GET /diag`，需管理口令）

第 4 步的三个码是分层的：**503 = 口令没进去；404 = 文件没进去；401 = 都进去了、只是没带凭证**。这三种现象在外部看完全不同，别混。

## 七、这个包**不**包含

- 任何口令、密钥、数据库连接串、用户 JWT
- 数据库里的数据（真实任务/课程都在 Supabase，随账号走，不随包走）
- 用户级访问令牌（运行期由持有者注入，只驻进程内存，不落盘）

## 八、已知限制（交接方必须知道）

1. **平台会自主重启容器**，实测间隔不规则，最短 2.2 分钟。进程内存里的用户 JWT 会随之消失；演示前注入后必须立刻验证。
2. **容器空闲约 10 分钟会被回收**（推断，依据是重启总紧贴下一次请求之前）。演示前先预热一次。
3. **发布是覆盖式**，没有"灰度/回滚按钮"。回滚 = 用旧包再发一次；旧包备份在 `work/deploy/rollback-*`。
4. **没有 OAuth**。鉴权是 `?sharecode=` + 三个自定义头（`X-Demo-Token` / `X-Real-Token` / `X-Admin-Key`）。开放平台未登记、无回调地址。
