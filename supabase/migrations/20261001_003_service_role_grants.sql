-- Modern Supabase secret keys assume the service_role database role.
-- RLS bypass does not replace ordinary table privileges, so grant only the
-- operations needed by the server-side publisher and verification tooling.

grant select on public.web_access_allowlist to service_role;
grant select, insert, update, delete on public.web_scanner_projection to service_role;
grant select on public.web_chart_manifests to service_role;
grant select on public.web_drawings to service_role;
