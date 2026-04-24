/**
 * Next.js App Router catch-all proxy.
 *
 * In production (Vercel), next.config.mjs rewrites:
 *   /api/:path*  →  /api/proxy/:path*
 *
 * This route then forwards the request to the Railway backend.
 * Without this file the entire dashboard is broken — every API call
 * hits a 404 because /api/proxy/[...path] had no route handler.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"
).replace(/\/+$/, "");

async function handler(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const pathStr = path.join("/");
  const search  = req.nextUrl.search;
  const url     = `${BACKEND}/api/${pathStr}${search}`;

  // Forward all headers except ones that would confuse the upstream
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!["host", "connection", "transfer-encoding"].includes(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  const isBodyMethod = !["GET", "HEAD"].includes(req.method.toUpperCase());
  const body = isBodyMethod ? await req.arrayBuffer() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method:  req.method,
      headers,
      body:    body ? Buffer.from(body) : undefined,
    });
  } catch (err) {
    console.error("[proxy] upstream fetch failed:", err);
    return NextResponse.json(
      { detail: "Backend unreachable. Please try again." },
      { status: 502 }
    );
  }

  const resHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    // Don't forward encoding headers — Next.js handles this
    if (!["content-encoding", "transfer-encoding"].includes(key.toLowerCase())) {
      resHeaders.set(key, value);
    }
  });

  return new NextResponse(upstream.body, {
    status:  upstream.status,
    headers: resHeaders,
  });
}

export const GET     = handler;
export const POST    = handler;
export const PUT     = handler;
export const DELETE  = handler;
export const PATCH   = handler;
export const OPTIONS = handler;
