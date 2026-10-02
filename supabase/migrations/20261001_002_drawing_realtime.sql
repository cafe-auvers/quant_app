-- Opt-in only after RLS integration tests pass in the dedicated dev project.
do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'web_drawings'
    ) then
        alter publication supabase_realtime add table public.web_drawings;
    end if;
end $$;
