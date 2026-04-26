/**
 * Next.js API proxy — forwards all /api/proxy/* requests to the Railway backend.
 *
 * Why this exists:
 *   Browser → /api/proxy/flow/scan  →  this handler  →  Railway /api/flow/scan
 *
 * The proxy strips the /api/proxy prefix, forwards headers (minus host/encoding),
 * and returns the upstream response verbatim.
 *
 * NOTE on headers forwarding:
 *   `upstream.headers` from node-fetch / undici is a Headers instance with .forEach().
 *   In jsdom / test environments the mock returns a plain object with only .get().
 *   We normalise by iterating Object.entries on a plain-object copy built before
 *   making the request, so tests and production both work.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8000";

type RouteCtx = { params: Promise<{ path: string[] }> };

async function handler(req: NextRequest, ctx: RouteCtx): Promise<NextResponse> {
  const { path } = await ctx.params;
  const segment  = Array.isArray(path) ? path.join("/") : path;

  // Rebuild upstream URL preserving query string
  const incomingUrl = new URL(req.url);
  const upstream    = `${BACKEND}/api/${segment}${incomingUrl.search}`;

  // Forward all request headers except `host`
  const forwardHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (key.toLowerCase() !== "host") {
      forwardHeaders[key] = value;
    }
  });

  // Read body for non-GET/HEAD methods
  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
  }

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(upstream, {
      method:  req.method,
      headers: forwardHeaders,
      body,
    });
  } catch (err) {
    console.error("[proxy] upstream fetch failed:", err);
    return NextResponse.json(
      { detail: "Backend unreachable. Please try again." },
      { status: 503 },
    );
  }

  const responseText = await upstreamRes.text();

  // Copy upstream response headers to our response.
  // upstream.headers may be a real Headers instance (production/Node) or a
  // plain object with only .get() (jsdom test mock). Normalise via a helper
  // that works for both.
  const resHeaders = new Headers();
  const rawHeaders = upstreamRes.headers;

  if (typeof (rawHeaders as Headers).forEach === "function") {
    // Real Headers instance (Node.js / undici)
    (rawHeaders as Headers).forEach((value, key) => {
      if (!["`content-encoding`", "transfer-encoding"].includes(key.toLowerCase())) {
        resHeaders.set(key, value);
      }
    });
  } else {
    // Plain object mock (jsdom / test)
    const ct = (rawHeaders as { get: (k: string) => string | null }).get("content-type");
    if (ct) resHeaders.set("content-type", ct);
  }

  return new NextResponse(responseText, {
    status:  upstreamRes.status,
    headers: resHeaders,
  });
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
