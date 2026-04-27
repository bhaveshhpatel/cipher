/**
 * /api/apex-gate — Next.js proxy for the Apex aggression gate config.
 *
 * GET   → proxies to backend GET  /api/apex/gate-config
 * PATCH → proxies to backend PATCH /api/apex/gate-config
 *
 * Forwards the Authorization header from the client unchanged.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8000";

export async function GET(req: NextRequest): Promise<NextResponse> {
  const auth = req.headers.get("authorization") ?? "";
  try {
    const upstream = await fetch(`${BACKEND}/api/apex/gate-config`, {
      headers: { Authorization: auth },
    });
    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch {
    return NextResponse.json({ error: "upstream_unavailable" }, { status: 502 });
  }
}

export async function PATCH(req: NextRequest): Promise<NextResponse> {
  const auth = req.headers.get("authorization") ?? "";
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  try {
    const upstream = await fetch(`${BACKEND}/api/apex/gate-config`, {
      method:  "PATCH",
      headers: { Authorization: auth, "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch {
    return NextResponse.json({ error: "upstream_unavailable" }, { status: 502 });
  }
}
