-- ============================================================
-- ESG 碳画像系统 · Supabase 建表 SQL
-- 执行方式：Supabase Dashboard → SQL Editor → New query → 粘贴 → Run
-- 说明：profiles 关联 auth.users；RLS 数据库层强制用户数据隔离
-- 日期：2026-08-18
-- ============================================================

-- 1. 用户资料扩展表（关联 Supabase 内置 auth.users）
create table if not exists public.profiles (
  id uuid references auth.users on delete cascade primary key,
  username text unique not null,          -- 中英文昵称
  created_at timestamptz default now()
);

-- 2. 分析历史表
create table if not exists public.history (
  id uuid default gen_random_uuid() primary key,
  user_id uuid references auth.users on delete cascade not null,
  company_name text not null,             -- 企业名称
  industry text default '',               -- 所属行业
  report_year text default '',            -- 报告年份
  total_score real default 0,             -- 总分
  grade text default '',                  -- 评分等级
  result_json jsonb not null,             -- 完整增强 JSON（42指标+评分+建议）
  created_at timestamptz default now()
);
create index if not exists idx_history_user on public.history(user_id, created_at desc);

-- 3. 行级安全（RLS）——数据库层强制数据隔离
alter table public.profiles enable row level security;
alter table public.history enable row level security;

-- profiles：用户只能读自己的资料
drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own" on public.profiles
  for select using (auth.uid() = id);

-- profiles：用户只能插入自己的资料（注册时自动创建）
drop policy if exists "profiles_insert_own" on public.profiles;
create policy "profiles_insert_own" on public.profiles
  for insert with check (auth.uid() = id);

-- history：用户只能读/写/删自己的历史记录
drop policy if exists "history_manage_own" on public.history;
create policy "history_manage_own" on public.history
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- 4. profiles 表 UPDATE policy（v=75 用户卡片·修改昵称用）
create policy "profiles_update_own" on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);
