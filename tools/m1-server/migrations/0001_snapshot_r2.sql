-- 0001_snapshot_r2.sql —— 独立 DEMO 快照库 · 修订版 R2
--
-- 修订依据（不是我自己想的，逐条对应）：
--   M2 2026-11-07 02:54:35Z / 03:07:56Z 三项 P0（user_id 复合 FK / date_range 无格式约束 / 连接池）
--   M0 2026-11-07 03:07:56Z 工单 §二 P0-1~3、§三 P1-1~3、§四 1
-- 目标库：全新独立 DEMO，Postgres 14+，不触碰任何既有/PRODUCTION 库
-- 执行属性：**幂等**，可重复执行；检测到旧版 schema 时显式失败而非静默跳过
--
-- 相对 R1（work/m1/outbox/2026-11-07_工作包A_...md 内嵌 DDL）的变更：
--   P0-1  snapshot 增 unique(snapshot_id,user_id)；proposal / applied_change 改复合 FK
--   P0-2  date_range text 拆为 date_from / date_to 两列（date 类型），据此建 latest 唯一索引
--   P1-1  全量 if not exists + 前置 guard，重复执行不报错
--   P1-2  base_version 改生成列，唯一构造入口，不漂移
--   P1-3  snapshot TTL 与 proposal TTL 两列分存分查（未合并）

-- ============================================================
-- 0. 前置 guard：拒绝在旧版 schema 上"假装成功"
-- ============================================================
do $$
begin
  if exists (
    select 1 from information_schema.columns
     where table_schema = 'checkd_demo'
       and table_name   = 'snapshot'
       and column_name  = 'date_range'
  ) then
    raise exception
      '检测到 R1 schema（checkd_demo.snapshot.date_range 存在）。R2 改动主键与唯一索引，不支持原地升级；请先 drop schema checkd_demo cascade（仅限独立 DEMO 库）后重跑本脚本。';
  end if;
end $$;

create schema if not exists checkd_demo;

-- ============================================================
-- 1. 发号表（P0-2：date_from / date_to 两列）
--    一行管一个 (user_id, date_from, date_to)，只用来发号
-- ============================================================
create table if not exists checkd_demo.snapshot_seq (
  user_id      text        not null,
  date_from    date        not null,
  date_to      date        not null,
  next_version integer     not null default 1,
  updated_at   timestamptz not null default now(),
  primary key (user_id, date_from, date_to),
  constraint snapshot_seq_range_ck   check (date_from <= date_to),
  constraint snapshot_seq_version_ck check (next_version > 0)
);

-- ============================================================
-- 2. 快照本体：只追加，不就地改 payload
-- ============================================================
create table if not exists checkd_demo.snapshot (
  snapshot_id    uuid        primary key default gen_random_uuid(),
  user_id        text        not null,
  date_from      date        not null,
  date_to        date        not null,
  version        integer     not null,
  payload        jsonb       not null,   -- 服务端自行读取，不采信请求体
  payload_digest text        not null,   -- sha256(规范化 json)，只做完整性，不当版本号
  payload_bytes  integer     not null,
  created_at     timestamptz not null default now(),
  expires_at     timestamptz not null,   -- snapshot TTL（与 proposal TTL 分开存，P1-3）
  superseded_at  timestamptz,            -- null = 该 (user,date_from,date_to) 的 latest
  constraint snapshot_version_pos check (version > 0),
  constraint snapshot_range_ck    check (date_from <= date_to),
  constraint snapshot_bytes_ck    check (payload_bytes >= 0),
  constraint snapshot_version_uk  unique (user_id, date_from, date_to, version),
  -- P0-1 关键：给 proposal / applied_change 的复合 FK 一个可指向的键。
  -- 单列 FK 允许 A 用户 proposal 指向 B 用户 snapshot，库不会拒；这一条才堵住。
  constraint snapshot_id_user_uk  unique (snapshot_id, user_id)
);

-- 同 (user_id, date_from, date_to) 任何时刻最多一个 latest
create unique index if not exists snapshot_one_latest
  on checkd_demo.snapshot (user_id, date_from, date_to)
  where superseded_at is null;

create index if not exists snapshot_lookup
  on checkd_demo.snapshot (user_id, date_from, date_to, version desc);

create index if not exists snapshot_expiry
  on checkd_demo.snapshot (expires_at) where superseded_at is null;

-- ============================================================
-- 3. Proposal：base_version 由生成列产出（P1-2），TTL 独立（P1-3）
-- ============================================================
create table if not exists checkd_demo.proposal (
  proposal_id      uuid        primary key default gen_random_uuid(),
  user_id          text        not null,
  snapshot_id      uuid        not null,
  snapshot_version integer     not null,
  -- P1-2：生成列 = 唯一构造入口。代码里再拼一次字符串就与库不一致，
  -- 所以不允许应用层写这个字段（写入即报错），杜绝"两处拼接慢慢漂移"。
  base_version     text        generated always as
                     (snapshot_id::text || ':' || snapshot_version::text) stored,
  status           text        not null default 'pending',
  created_at       timestamptz not null default now(),
  expires_at       timestamptz not null,   -- proposal TTL，与 snapshot.expires_at 是两个字段两条语义
  applied_at       timestamptz,
  constraint proposal_status_ck check (status in ('pending','applied','rejected','expired')),
  constraint proposal_version_pos check (snapshot_version > 0),
  -- P0-1：复合 FK。proposal.user_id 必须与自己引用的 snapshot.user_id 相同
  constraint proposal_snapshot_fk foreign key (snapshot_id, user_id)
    references checkd_demo.snapshot (snapshot_id, user_id),
  -- 供 applied_change 复合 FK 指向
  constraint proposal_id_user_uk unique (proposal_id, user_id)
);

create index if not exists proposal_user
  on checkd_demo.proposal (user_id, created_at desc);
create index if not exists proposal_pending
  on checkd_demo.proposal (expires_at) where status = 'pending';

-- ============================================================
-- 4. 应用结果：apply 成功才写，与 proposal 状态同事务
-- ============================================================
create table if not exists checkd_demo.applied_change (
  apply_id     uuid        primary key default gen_random_uuid(),
  proposal_id  uuid        not null,
  user_id      text        not null,
  snapshot_id  uuid        not null,
  applied_at   timestamptz not null default now(),
  result       jsonb       not null,
  -- 幂等最后一道闸：并发两个 apply 都过了前置检查，第二个在插入时冲突 → 回滚 → ALREADY_APPLIED
  constraint applied_change_proposal_uk unique (proposal_id),
  -- P0-1：applied_change 按同一归属原则约束，proposal 与 snapshot 都必须同属一个 user
  constraint applied_change_proposal_fk foreign key (proposal_id, user_id)
    references checkd_demo.proposal (proposal_id, user_id),
  constraint applied_change_snapshot_fk foreign key (snapshot_id, user_id)
    references checkd_demo.snapshot (snapshot_id, user_id)
);

-- ============================================================
-- 5. 发号：单语句，禁止 select max+1
--    首次插入 next_version=2 → 返回 1；并发第二次阻塞在该行 update → 3 → 返回 2
-- ============================================================
-- insert into checkd_demo.snapshot_seq (user_id, date_from, date_to, next_version)
-- values ($1, $2, $3, 2)
-- on conflict (user_id, date_from, date_to)
-- do update set next_version = checkd_demo.snapshot_seq.next_version + 1,
--               updated_at   = now()
-- returning next_version - 1 as version;

-- ============================================================
-- 6. 取新号后的同事务两步（锁序固定：seq 行 → 旧 latest 行 → 插新行）
-- ============================================================
-- update checkd_demo.snapshot set superseded_at = now()
--  where user_id = $1 and date_from = $2 and date_to = $3 and superseded_at is null;
--
-- insert into checkd_demo.snapshot
--   (user_id, date_from, date_to, version, payload, payload_digest, payload_bytes, expires_at)
-- values ($1, $2, $3, $4, $5::jsonb, $6, $7, now() + make_interval(secs => $8))
-- returning snapshot_id;
