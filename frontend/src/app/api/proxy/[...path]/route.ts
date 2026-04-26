/**
 * Next.js App Router proxy – forwards /api/* requests to the Railway backend.
 *
 * Environment variables:
 *   BACKEND_URL  – Railway backend origin, e.g. https://cipher-production-6cd8.up.railway.app
 *                  Falls back to NEXT_PUBLIC_API_URL for local-dev compatibility.
 *
 * Behaviour:
 *   - Strips the `host` header before forwarding (prevents SNI mismatch on Railway).
 *   - Returns HTTP 503 + JSON { detail } when the backend is unreachable.
 *   - Forwards all other headers (Authorization, Content-Type, etc.) unchanged.
 *   - Preserves query-string from the original request.
 *   - Supports GET, POST, PUT, PATCH, DELETE via a single shared handler.
 */

import { NextRequest, NextResponse } from "next/server";

const BACKEND =
  (
    process.env.BACKEND_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000"
  ).replace(/\/+$/, ""); // strip trailing slash

type RouteContext = { params: Promise<{ path: string[] }> };

/** Build the upstream URL from the catch-all path segments + original query string. */
function buildUpstreamUrl(req: NextRequest, pathSegments: string[]): string {
  const joined = pathSegments.join("/");
  const search = req.nextUrl.search ?? "";
  return `${BACKEND}/api/${joined}${search}`;
}

/** Copy all request headers except `host` into a plain object for fetch(). */
function forwardHeaders(req: NextRequest): Record<string, string> {
  const out: Record<string, string> = {};
  // NextRequest.headers is a real Headers instance in the Node runtime.
  // Use Object.fromEntries for environments where .forEach may not be present.
  Object.entries(Object.fromEntries(req.headers)).forEach(([key, value]) => {
    if (key.toLowerCase() !== "host") {
      out[key] = value;
    }
  });
  return out;
}

async function handler(
  req: NextRequest,
  ctx: RouteContext
): Promise<NextResponse> {
  const { path: pathSegments } = await ctx.params;
  const upstreamUrl = buildUpstreamUrl(req, pathSegments);
  const headers = forwardHeaders(req);

  // Read body for methods that carry one
  const body =
    req.method === "GET" || req.method === "HEAD"
      ? undefined
      : await req.text();

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method: req.method,
      headers,
      body,
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown network error";
    return NextResponse.json(
      { detail: `Backend unreachable: ${message}` },
      { status: 503 }
    );
  }

  const responseText = await upstream.text();
  const contentType =
    upstream.headers.get("content-type") ?? "application/json";

  return new NextResponse(responseText, {
    status: upstream.status,
    headers: { "content-type": contentType },
  });
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
