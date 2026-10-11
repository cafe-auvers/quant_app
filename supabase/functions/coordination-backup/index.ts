// A bounded recovery backup. This function never changes trading state.
const MAX_RAW_BYTES = 8 * 1024 * 1024;
const MAX_COMPRESSED_BYTES = 1024 * 1024;
const BUCKET = "coordination-backups";

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

Deno.serve(async (request: Request) => {
  const url = Deno.env.get("SUPABASE_URL") ?? "";
  const authorization = request.headers.get("Authorization") ?? "";
  const key = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  // Keep gateway JWT verification enabled. The export RPC grants EXECUTE only
  // to service_role; pass the caller's credential through without elevating it.
  if (!key) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  if (request.method !== "POST") return new Response(null, { status: 405 });
  const headers = { apikey: key, Authorization: `Bearer ${key}` };
  const started = Date.now();
  try {
    const exported = await fetch(`${url}/rest/v1/rpc/quant_export_coordination_backup`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: "{}",
    });
    if (exported.status === 401 || exported.status === 403) {
      return Response.json({ error: "Unauthorized" }, { status: 401 });
    }
    if (!exported.ok) throw new Error(`Export HTTP ${exported.status}`);
    const payloadText = await exported.json();
    if (typeof payloadText !== "string") throw new Error("Invalid snapshot envelope");
    const raw = new TextEncoder().encode(payloadText);
    if (raw.byteLength > MAX_RAW_BYTES) throw new Error("Raw backup exceeds free budget");
    const payload = JSON.parse(payloadText);
    if (Object.keys(payload.tables ?? {}).length !== 20) throw new Error("Unexpected table set");
    const createdAt = new Date().toISOString();
    const archive = JSON.stringify({
      format: "quant-cloud-coordination-backup-v1", created_at: createdAt,
      payload_text: payloadText, payload_sha256: await sha256(raw),
    });
    const compressed = new Uint8Array(await new Response(
      new Blob([archive]).stream().pipeThrough(new CompressionStream("gzip")),
    ).arrayBuffer());
    if (compressed.byteLength > MAX_COMPRESSED_BYTES) throw new Error("Compressed backup exceeds free budget");
    // Thirty-one slots bound storage without accumulating daily revisions.
    const slot = createdAt.slice(8, 10);
    const objectName = `coordination/slot-${slot}.json.gz`;
    const objectUrl = `${url}/storage/v1/object/${BUCKET}/${objectName}`;
    const checksum = await sha256(compressed);
    const uploaded = await fetch(objectUrl, {
      method: "POST", headers: { ...headers, "Content-Type": "application/gzip", "x-upsert": "true" },
      body: compressed,
    });
    if (!uploaded.ok) throw new Error(`Upload HTTP ${uploaded.status}`);
    const readback = await fetch(`${url}/storage/v1/object/authenticated/${BUCKET}/${objectName}?v=${checksum}`, {
      headers: { ...headers, "Cache-Control": "no-cache" },
    });
    if (!readback.ok) throw new Error(`Verification HTTP ${readback.status}`);
    if (await sha256(new Uint8Array(await readback.arrayBuffer())) !== checksum) {
      throw new Error("Stored backup checksum mismatch");
    }
    const rows = Object.fromEntries(Object.entries(payload.tables).map(([name, values]) => [name, (values as unknown[]).length]));
    const result = {
      object_name: objectName, created_at: createdAt, sha256: checksum,
      compressed_bytes: compressed.byteLength, raw_bytes: raw.byteLength,
      tables: 20, rows, elapsed_ms: Date.now() - started,
    };
    const recorded = await fetch(`${url}/rest/v1/rpc/quant_record_coordination_backup`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ p_result: result }),
    });
    if (!recorded.ok) throw new Error(`Run record HTTP ${recorded.status}`);
    return Response.json({ verified: true, ...result });
  } catch (error) {
    // No payload, account data, or credentials are written to logs.
    return Response.json({ verified: false, error: error instanceof Error ? error.message : "Backup failed" }, { status: 500 });
  }
});
