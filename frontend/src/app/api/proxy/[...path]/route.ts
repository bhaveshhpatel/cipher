/**
 * Next.js API proxy — forwards all /api/* requests to the Railway backend.
 *
 * WHY THIS EXISTS:
 * Vercel serves the frontend over HTTPS from a *.vercel.app domain.
 * Browsers block mixed-content (HTTPS page → HTTP backend) and enforce
 * strict CORS on cross-origin fetches. Rather than fight both, we proxy
 * every /api/* call through Next.js itself so:
 *   - The browser always talks to the same origin (no CORS at all)
 *   - HTTP vs HTTPS is handled server-side, not in the browser
 *   - NEXT_PUBLIC_API_URL is only needed server-side now
 *
 * The frontend lib/api.ts calls /api/... (relative URLs), which Next.js
 * routes here, which forwards to Railway.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.BACKEND_URL ||
  "http://localhost:8000"
).replace(/\/+$/, "");

async function handler(req: NextRequest, { params }: { params: { path: string[] } }) {
  const path   = params.path.join("/");
  const search = req.nextUrl.search ?? "";
  const url    = `${BACKEND}/api/${path}${search}`;

  // Forward all headers except host (causes Railway to reject)
  const headers = new Headers(req.headers);
  headers.delete("host");

  try {
    const upstream = await fetch(url, {
      method:  req.method,
      headers,
      body:    ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
      // @ts-expect-error — Node 18+ fetch supports duplex
      duplex:  "half",
    });

    const body = await upstream.arrayBuffer();

    return new NextResponse(body, {
      status:  upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-store",
      },
    });
  } catch (err) {
    console.error(`[proxy] ${req.method} ${url} →`, err);
    return NextResponse.json(
      { detail: "Backend unreachable — server may be starting up, please retry in a few seconds." },
      { status: 503 }
    );
  }
}

export const GET     = handler;
export const POST    = handler;
export const PUT     = handler;
export const PATCH   = handler;
export const DELETE  = handler;
export const OPTIONS = handler;
