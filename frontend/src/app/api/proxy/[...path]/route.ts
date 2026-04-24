/**
 * Next.js App Router proxy — forwards /api/* to the Railway backend.
 *
 * Env var priority (both are checked so either works):
 *   BACKEND_URL          — server-only var, recommended for production (Vercel)
 *   NEXT_PUBLIC_API_URL  — also works; used by local dev and as fallback
 *
 * IMPORTANT: On Vercel, set BACKEND_URL = https://your-app.up.railway.app
 * in the Vercel dashboard under Settings → Environment Variables.
 */
import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
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

  if (!BACKEND || BACKEND === "http://localhost:8000" && process.env.NODE_ENV === "production") {
    console.error("[proxy] No BACKEND_URL or NEXT_PUBLIC_API_URL configured for production");
    return NextResponse.json(
      { detail: "Backend not configured — set BACKEND_URL in Vercel environment variables." },
      { status: 503 }
    );
  }

  const fwdHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (key.toLowerCase() !== "host") fwdHeaders[key] = value;
  });

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

export const dynamic = "force-dynamic";
