/**
 * Next.js App Router — transparent reverse proxy to Railway backend.
 *
 * Production rewrite rules (next.config.mjs) send:
 *   /api/* → /api/proxy/:path* where [...path] = segments AFTER /api/
 *
 * Example:
 *   Browser:       GET /api/health/stream
 *   After rewrite: GET /api/proxy/health/stream
 *   [...path]:     ["health", "stream"]
 *   Upstream:      BACKEND_URL/api/health/stream  ✓
 *
 * The proxy PREPENDS /api/ to reconstruct the full upstream path.
 * OPTIONS preflights are handled locally so they never reach Railway.
 */

import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"
).replace(/\/+$/, "");

// Headers that must not be forwarded to the upstream service.
const STRIP_HEADERS = new Set([
  "host",
  "origin",
  "referer",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-vercel-forwarded-for",
  "x-vercel-id",
]);

type Context = { params: Promise<{ path: string[] }> };

async function handler(req: NextRequest, context: Context): Promise<NextResponse> {
  const { path } = await context.params;

  // Reconstruct the full upstream path by prepending /api/.
  // The rewrite strips /api/ from the path before passing to [...path],
  // so path=["health","stream"] must become /api/health/stream upstream.
  const upstreamPath = "/api/" + path.join("/");
  const search = req.nextUrl.search ?? "";
  const upstreamURL = `${BACKEND}${upstreamPath}${search}`;

  // Copy headers, stripping ones that would confuse Railway's CORS/routing.
  const forwardHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (!STRIP_HEADERS.has(key.toLowerCase())) {
      forwardHeaders[key] = value;
    }
  });

  // Read body for methods that carry one.
  let body: ArrayBuffer | null = null;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(upstreamURL, {
      method:  req.method,
      headers: forwardHeaders,
      body:    body ?? undefined,
      // @ts-expect-error — Node 18 fetch supports duplex
      duplex:  body ? "half" : undefined,
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[proxy] Backend unreachable: ${msg} — ${upstreamURL}`);
    return NextResponse.json(
      { detail: `Backend unreachable: ${msg}` },
      { status: 503 },
    );
  }

  // Stream response body back to the browser.
  const responseBody = await upstream.arrayBuffer();
  const responseHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    // Let Next.js manage these — don't copy them from upstream.
    if (
      key.toLowerCase() !== "content-encoding" &&
      key.toLowerCase() !== "transfer-encoding"
    ) {
      responseHeaders.set(key, value);
    }
  });

  return new NextResponse(responseBody, {
    status:  upstream.status,
    headers: responseHeaders,
  });
}

// Handle CORS preflights locally — never forward OPTIONS to Railway.
export async function OPTIONS(
  _req: NextRequest,
  _context: Context,
): Promise<NextResponse> {
  return new NextResponse(null, {
    status: 200,
    headers: {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Authorization,Content-Type,Accept",
      "Access-Control-Max-Age":       "86400",
    },
  });
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
