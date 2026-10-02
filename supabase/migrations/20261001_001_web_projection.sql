-- Quant Web optional projection/drawing schema.
-- This migration deliberately contains no production TradeCards, Watchlist,
-- Buylist, Buy Today, orders, reservations, ownership, or execution ledgers.

create extension if not exists pgcrypto;

create table if not exists public.web_access_allowlist (
    user_id uuid primary key references auth.users(id) on delete cascade,
    created_at timestamptz not null default now()
);

create table if not exists public.web_scanner_projection (
    symbol text primary key,
    snapshot_date date not null,
    setup_name text not null,
    rank integer,
    metrics jsonb not null default '{}'::jsonb,
    source text not null,
    updated_at timestamptz not null default now()
);

create table if not exists public.web_chart_manifests (
    symbol text not null,
    timeframe text not null check (timeframe in ('1D', '1H')),
    object_name text not null,
    checksum_sha256 text not null check (length(checksum_sha256) = 64),
    compressed_bytes bigint not null check (compressed_bytes >= 0),
    source text not null,
    adjustment_mode text not null,
    session_policy text not null,
    publication_state text not null check (publication_state in ('UPDATING', 'READY', 'FAILED')),
    revision bigint not null default 1,
    updated_at timestamptz not null default now(),
    primary key (symbol, timeframe)
);

create table if not exists public.web_drawings (
    drawing_id uuid primary key default gen_random_uuid(),
    symbol text not null,
    start_ts timestamptz not null,
    start_price numeric not null check (start_price > 0),
    end_ts timestamptz not null,
    end_price numeric not null check (end_price > 0),
    timeframe text not null check (timeframe in ('1D', '1H')),
    revision bigint not null default 1 check (revision > 0),
    author_id uuid not null default auth.uid() references auth.users(id),
    deleted boolean not null default false,
    updated_at timestamptz not null default now()
);

alter table public.web_access_allowlist enable row level security;
alter table public.web_scanner_projection enable row level security;
alter table public.web_chart_manifests enable row level security;
alter table public.web_drawings enable row level security;

revoke all on public.web_access_allowlist from anon, authenticated;
revoke all on public.web_scanner_projection from anon;
revoke all on public.web_chart_manifests from anon;
revoke all on public.web_drawings from anon;

grant select on public.web_scanner_projection to authenticated;
grant select on public.web_chart_manifests to authenticated;
grant select, insert, update on public.web_drawings to authenticated;

create policy "allowlisted scanner read"
on public.web_scanner_projection for select to authenticated
using (exists (select 1 from public.web_access_allowlist a where a.user_id = auth.uid()));

create policy "allowlisted manifest read"
on public.web_chart_manifests for select to authenticated
using (exists (select 1 from public.web_access_allowlist a where a.user_id = auth.uid()));

create policy "owner drawing read"
on public.web_drawings for select to authenticated
using (
    author_id = auth.uid()
    and exists (select 1 from public.web_access_allowlist a where a.user_id = auth.uid())
);

create policy "owner drawing insert"
on public.web_drawings for insert to authenticated
with check (
    author_id = auth.uid()
    and exists (select 1 from public.web_access_allowlist a where a.user_id = auth.uid())
);

create policy "owner drawing update"
on public.web_drawings for update to authenticated
using (author_id = auth.uid())
with check (author_id = auth.uid());

insert into storage.buckets (id, name, public)
values ('chart-cache', 'chart-cache', false)
on conflict (id) do update set public = false;

create policy "allowlisted private chart read"
on storage.objects for select to authenticated
using (
    bucket_id = 'chart-cache'
    and exists (select 1 from public.web_access_allowlist a where a.user_id = auth.uid())
);

create or replace function public.web_begin_chart_publication(
    p_symbol text,
    p_timeframe text,
    p_object_name text,
    p_source text,
    p_adjustment_mode text,
    p_session_policy text
) returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
    next_revision bigint;
begin
    if p_timeframe not in ('1D', '1H') then
        raise exception 'invalid timeframe';
    end if;
    insert into public.web_chart_manifests (
        symbol, timeframe, object_name, checksum_sha256, compressed_bytes,
        source, adjustment_mode, session_policy, publication_state, revision
    ) values (
        upper(p_symbol), p_timeframe, p_object_name, repeat('0', 64), 0,
        p_source, p_adjustment_mode, p_session_policy, 'UPDATING', 1
    )
    on conflict (symbol, timeframe) do update set
        object_name = excluded.object_name,
        source = excluded.source,
        adjustment_mode = excluded.adjustment_mode,
        session_policy = excluded.session_policy,
        publication_state = 'UPDATING',
        revision = public.web_chart_manifests.revision + 1,
        updated_at = now()
    returning revision into next_revision;
    return next_revision;
end;
$$;

create or replace function public.web_finish_chart_publication(
    p_symbol text,
    p_timeframe text,
    p_revision bigint,
    p_state text,
    p_checksum text,
    p_compressed_bytes bigint
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_state not in ('READY', 'FAILED')
       or length(p_checksum) <> 64
       or p_compressed_bytes < 0 then
        raise exception 'invalid publication result';
    end if;
    update public.web_chart_manifests set
        checksum_sha256 = p_checksum,
        compressed_bytes = p_compressed_bytes,
        publication_state = p_state,
        updated_at = now()
    where symbol = upper(p_symbol)
      and timeframe = p_timeframe
      and revision = p_revision
      and publication_state = 'UPDATING';
    return found;
end;
$$;

revoke all on function public.web_begin_chart_publication(text, text, text, text, text, text) from public, anon, authenticated;
revoke all on function public.web_finish_chart_publication(text, text, bigint, text, text, bigint) from public, anon, authenticated;
grant execute on function public.web_begin_chart_publication(text, text, text, text, text, text) to service_role;
grant execute on function public.web_finish_chart_publication(text, text, bigint, text, text, bigint) to service_role;

-- Publisher uploads use a server-side secret/service credential, which
-- bypasses RLS. No browser INSERT/UPDATE policy is intentionally present.
