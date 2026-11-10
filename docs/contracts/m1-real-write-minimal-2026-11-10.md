# REAL-WRITE 最小工具：改动范围 · 风险 · 停止条件

> 出件：M1　2026-11-10 21:12 (+08)
> 依据：M0 2026-11-10T12:07:16Z《最小 REAL-WRITE 选型》
> 状态：**代码就绪、隔离测试 24 项全绿、默认关闭、未发布、未对生产发出任何请求**

---

## 一、先答 M0 问的四件事

| 问 | 答 |
|---|---|
| **可行性** | 可行。技术路径无阻塞，唯一硬前提是「App 上该任务没有待上传的本地修改」 |
| **隔离实测证据** | 24 项隔离测试全绿（`tests/test_real_write.py`），全套 330 项 OK；开关两态已验证 |
| **预计最小改动** | 新增 2 文件（`real_write.py` 320 行、`tests/test_real_write.py` 300 行）、改 `app.py` 3 处共约 110 行 |
| **是否需 M0 单独授权** | **是。** 生产 PATCH 需要单独授权，且需先拿到两项事实（见第六节） |

---

## 二、只读核实：真实 schema 事实（不是列白名单推断）

M0 要求「不以列白名单推断替代事实」。以下全部来自 `supabase/migrations/` 的 DDL 源码，**逐条带行号**：

`supabase/migrations/202609020001_phase_1_schema.sql`：

```sql
create table public.tasks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 100),   -- ← 长度约束
  type public.task_type not null,
  color int not null,
  status public.task_status not null default 'active',
  notes text,
  sync_version bigint not null default 1,                            -- ← 乐观锁字段
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);
```

| 事实 | 位置 | 对设计的影响 |
|---|---|---|
| `name` 是 `text not null`，`check (char_length between 1 and 100)` | 表定义 | 工具层先校验 1–100，不让库报一个英文约束名 |
| `id` 是主键 | 表定义 | 条件更新最多命中一行；命中多行说明条件被改写，必须中止 |
| `user_id` 是归属字段 | 表定义 | RLS 用它判归属；工具层**绝不**传它 |
| `sync_version bigint default 1` | 表定义 | **设计上不使用**（理由见第四节） |
| `set_updated_at()` before update 触发器挂在 `tasks` 上 | `202609020001` L233 定义、L248-256 循环挂载 | 任何 PATCH 都会推 `updated_at`，这是不可回避的副作用 |
| RLS：`for all to authenticated using/with check (auth.uid() = user_id)` | `202609020001` L299-305（`format` 动态生成） | 用户本人 JWT 可改自己的行；**行级不是列级**，单字段边界只能靠工具层白名单 |
| `task_revisions(before_data, after_data jsonb)` 存在，但无自动写入触发器 | L212-220 | 回滚载体可用，但工具层得自己插；本轮**不做** |
| 后续迁移只改过 `icon_name`（L11 `if not exists`）与 `color: int→bigint`（`202610080001` L14-19） | — | 不影响 `name` 写入 |

**必须说清的一点**：迁移文件存在 ≠ 生产库已执行。这正是 M0 2026-11-10T09:00 提醒 M2 的那条。
**生产库真实状态只能由 M0 在 Supabase 控制台确认**（见第六节两项）。

---

## 三、App 同步语义（这是本轮最关键的发现）

M3 提出「同值 PATCH 会推 `updated_at`，App 增量拉下旧值后下次上传会盖回服务端」。我核了，她前半句对，后半句**取决于 App 有没有先拉取**。代码事实：

### 3.1 上传：整行覆盖，无版本前置

`lib/core/sync/sync_upload_service.dart` L578-594，`tasks` 的上传 payload：

```dart
CloudWrite('tasks', _owned(userId, {
  'id': task.id,
  'name': task.name,              // ← 本地值，会覆盖服务端
  'type': ..., 'color': ..., 'icon_name': ..., 'tag_id': ...,
  'notes': ..., 'status': ...,
  'created_at': _iso(task.createdAt),
  'updated_at': _iso(task.updatedAt),   // ← 本地 updated_at，也会覆盖
  'deleted_at': _isoOrNull(task.deletedAt),
}), 'id');
```

- `onConflict: 'id'`，走 `upsert`（L43）
- **没有 `sync_version` 参与，没有任何 `where` 版本条件** ⇒ M3 的「无版本前置会覆盖服务端改动」成立
- 但前提是**这行确实进入了上传队列**

### 3.2 下载：按 `updated_at` 判远端是否更新

`lib/core/sync/sync_download_service.dart` L461-477：

```dart
bool _isRemoteNewer({...}) {
  if (localUpdatedAt == null) return true;
  final remoteUpdatedAt = _dateTime(remote, 'updated_at');
  if (remoteUpdatedAt.isAfter(localUpdatedAt)) return true;   // ← 采用远端
  if (remoteUpdatedAt.isBefore(localUpdatedAt)) return false;
  ... // 相同则再比 sync_version
}
```

**⇒ 服务端 PATCH 把 `updated_at` 推到 now 之后，App 下次拉取会判定「远端更新」并采用远端的 `name`。**

### 3.3 结论：`updated_at` 被推是必要机制，不是纯副作用

| 情形 | 结果 |
|---|---|
| App 无该任务的待上传修改 | 拉取 → 采用远端新名 → 本地同步 → **不会回盖** ✅ |
| App 有该任务的待上传修改（sync_queue 里有） | 先 push 本地旧值 → **回盖服务端** ❌ |

**所以硬约束是：测试前必须确认该任务在 App 侧没有未上传的改动。**
这一条我已经写进第七节的停止条件。

**这条比 M3 的判断更进一步**：她的结论在「不拉取就上传」时成立；但代码显示拉取会按 `updated_at` 采用远端，所以时间戳被推恰恰是让同步生效的手段。这是我读 `_isRemoteNewer` 才看见的。

---

## 四、设计：为什么不用 `sync_version` 做乐观锁

库里明明有 `sync_version bigint`，看起来天生适合做乐观锁。不用它，三个理由：

1. **没有消费方。** App 上传 payload 里根本没有 `sync_version`（L578-594 逐列可查），服务端写了也没人比对。
2. **响应式更新的语义对不上。** `sync_version` 语义是「本地改了几次」，而我要防的是「服务端这一行在我读取之后被别人改过」——用**旧值本身**做条件是更直接的前提校验。
3. **触发器只管 `updated_at`。** 没有任何触发器自动递增 `sync_version`，工具层若手工写它，等于自己编一个没人在读的数。

所以采用：`PATCH /rest/v1/tasks?id=eq.<uuid>&name=eq.<旧名>&deleted_at=is.null`

一个条件同时解决两件事：

- **并发覆盖**：旧名不匹配 ⇒ 匹配 0 行 ⇒ 拒绝，不覆盖别人的改动
- **重复应用**：第二次调用时名称已是新值 ⇒ 同样 0 行 ⇒ 拒绝

---

## 五、改动范围

### 5.1 新增文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `tools/m1-server/real_write.py` | 约 320 | `RealWrite.update_task_name`，只用 PATCH，只改 `name` |
| `tools/m1-server/tests/test_real_write.py` | 约 300 | 24 项隔离测试 |

### 5.2 修改 `tools/m1-server/app.py`（3 处，约 110 行）

| 位置 | 改动 | 为什么不能更小 |
|---|---|---|
| L52-64 | 新增 `real_write` 的独立 try-import | 与 `real_read` 分开失败，避免读写互相牵连报错口径 |
| L228 附近 | 新增 `REAL_WRITE_ENABLED`（默认 `False`）、`REAL_WRITE_TOOL` 常量 | 能力必须显式开启 |
| `_real_call` 开头 | 写入工具在此分岔，早于 `validate_envelope` | 写入没有 `date_range`，进读取那套校验只会报「date_range 必填」 |
| 新增 `_real_write_call` | 开关→加载→令牌→参数→请求，顺序不可换 | 顺序错了就等于留一条绕过确认的缝 |
| `_real_tool_defs` | 仅 `REAL_WRITE_ENABLED` 时追加工具定义 | 默认不出现在 `tools/list` |

### 5.3 **不改**的

- `real_read.py`（306 项测试全绿的读取路径，一行未动）
- `tools/m1-mock/*`（MOCK 七工具与八步闭环）
- 数据库：不改 DDL、不改 RLS、不加触发器、不加迁移
- App 侧 Dart 代码

---

## 六、生产验证的两个前提（需要 M0）

这两项我做不了，因为我没有生产库权限，也不该有：

1. **`tasks` 表在生产库真实存在且 `name` 可 UPDATE** —— 我只能从 migrations 读到定义，无法确认「已执行」。M0 在 Supabase 表编辑器看一眼即可。
2. **App 上目标任务没有待上传的本地修改** —— 需要在 App 里操作后等同步完成，或确认该任务最近没被本地编辑过。

另外需要 M0 拍板一件事：**选哪条专用测试任务、旧名 → 新名**。M0 原文要求「明确选择专用测试任务并确认旧/新名称」。
**建议新名带上可识别前缀**（如 `测试-改名前`），便于现场确认与回滚。

---

## 七、停止条件（满足任一即中止，不再往下做）

| # | 条件 | 中止后做什么 |
|---|---|---|
| 1 | 生产库 `tasks.name` 不存在或不可 UPDATE | 整体标 BLOCKED，写进能力矩阵，不再尝试 |
| 2 | RLS 策略与 migrations 不符（不是 `auth.uid() = user_id`） | 停下，报告实测到的策略原文，由 M0 决定是否改 policy |
| 3 | 隔离测试出现任一项失败 | 不修测试迁就代码，先定位是设计错还是实现错 |
| 4 | App 上目标任务有未上传改动 | 等同步完成再试；不得在 pending 状态下执行 |
| 5 | 平台自主重启导致 JWT 丢失 | 停止，报时间与 pid，等 M0 重新注入；不重试 |
| 6 | 任一次 PATCH 返回 0 行 | 立即停止，重新读取确认当前真实值后再判断，**不重试同一个请求** |
| 7 | 回读名称与期望不一致 | 立即停止并报告，视作数据已被改动 |

---

## 八、短时授权窗口内怎么测（M0 特别问的）

JWT 只驻进程内存（`app.py` L192），容器空闲约十分钟被回收，令牌随之消失。今天实测：心跳期间同一进程可存活 13 分钟以上（60 次 `/health` 全 200，pid/boot_at 不变）。

**所以窗口必须这么用：**

1. **注入前 1 分钟**开始每 12 秒打一次 `/health`（保活）；
2. 注入后**立刻**执行：`list_tasks` 读当前名 → `update_task_name` → 回读确认，三步压在一分钟内；
3. 全程不发布、不重启（发布必然换进程）。

**失败恢复**：

| 失败点 | 现象 | 动作 |
|---|---|---|
| 注入后进程被换 | `/diag` 的 `real_jwt_present` 变 false | 重新注入一次，不重试写入 |
| 写入成功但 App 未更新 | 服务端值已变、App 显示旧名 | 在 App 手动触发一次同步（下拉刷新）；仍未更新则查 `_isRemoteNewer` 判定 |
| 写入返回 0 行 | `SNAPSHOT_CONFLICT` | 重新 `list_tasks` 读真实当前值，确认是否已被别人改过 |
| 需要回滚 | — | 用同一工具反向调用：`expected_old_name=新名`、`new_name=旧名`。**回滚也是生产写入**，同样需要授权 |

---

## 九、副作用清单（M0 要求写清）

| 副作用 | 是否发生 | 说明 |
|---|---|---|
| `updated_at` 被推 | **必然** | `set_updated_at()` before update 触发器。这正是 App 感知变化的手段 |
| App 触发一次下载比对 | **必然** | `_isRemoteNewer` 会判定远端更新并采用 |
| `sync_version` 变化 | 否 | 无触发器递增，工具层也不写 |
| `task_revisions` 留痕 | 否 | 无自动写入触发器，本轮不插 |
| 审计日志 | 部分 | 工具层 `_EVIDENCE` 记一条脱敏条目（task_id 前 8 位、名称长度、是否确认），**不记名称原文** |
| 其他字段被动 | 否 | payload 只有 `{"name": ...}`，测试 `test_body_carries_only_name` 断言此点 |

---

## 十、当前状态与申请

| 项 | 值 |
|---|---|
| 隔离测试 | **330 项 OK**（含新增 24 项） |
| 默认开关 | `CHECKD_REAL_WRITE` 未设 ⇒ 关闭，工具不出现，调用即 `WRITE_BLOCKED` 且不发请求 |
| 对生产发出的请求 | **0 次** |
| 是否发布 | **否**。线上仍 16:03:45 版本 |

**申请**：请 M0 给出（1）生产库 `tasks.name` 可写确认、（2）专用测试任务 ID 与旧/新名称、（3）该任务在 App 侧无待上传改动确认。
三项齐备后我提交「生产单次测试申请」，届时再单独请你授权执行。

**在此之前不会对生产发出任何 PATCH。**
