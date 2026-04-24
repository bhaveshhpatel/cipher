/**
 * Next.js App Router proxy — forwards /api/* to the Railway backend.
 *
 * Fixes:
 * - HTTP 501: caused by passing req.body (ReadableStream) directly into
 *   fetch() with duplex:"half" which isn't supported on all Vercel runtimes.
 *   Now reads body as text first, then forwards as string — safe and universal.
 * - Works for all content types: JSON, form-urlencoded, multipart.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.BACKEND_URL ||           // server-only var — set in Vercel env vars
  process.env.NEXT_PUBLIC_API_URL ||   // fallback for local dev
  "http://localhost:8000"
).replace(/\/+$/, "");

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  const pathStr = path.join("/");
  const search  = req.nextUrl.search ?? "";
  const url     = `${BACKEND}/api/${pathStr}${search}`;

  // Build forwarded headers — drop host to avoid Railway rejecting the request
  const fwdHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (key.toLowerCase() !== "host") fwdHeaders[key] = value;
  });

  // Read body upfront as text so we avoid ReadableStream / duplex issues
  let body: string | undefined;
  if (!["GET", "HEAD"].includes(req.method)) {
    body = await req.text();
  }

  try {
    const upstream = await fetch(url, {
      method:  req.method,
      headers: fwdHeaders,
      body,
    });

    const responseBody = await upstream.text();
    const contentType  = upstream.headers.get("content-type") ?? "application/json";

    return new NextResponse(responseBody, {
      status:  upstream.status,
      headers: {
        "content-type":  contentType,
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

// Required for Next.js App Router dynamic API routes
export const dynamic = "force-dynamic";
