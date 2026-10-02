-- Keep drawing updates subject to the same allowlist used by reads/inserts.
-- Replacing the policy makes allowlist removal effective for existing owners.

drop policy if exists "owner drawing update" on public.web_drawings;

create policy "owner drawing update"
on public.web_drawings for update to authenticated
using (
    author_id = auth.uid()
    and exists (
        select 1 from public.web_access_allowlist a
        where a.user_id = auth.uid()
    )
)
with check (
    author_id = auth.uid()
    and exists (
        select 1 from public.web_access_allowlist a
        where a.user_id = auth.uid()
    )
);
