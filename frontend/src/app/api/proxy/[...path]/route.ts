/**
 * Next.js catch-all API proxy → Railway backend
 *
 * All requests to /api/* from the frontend are forwarded here.
 * Running server-side on Vercel so there is NO cross-origin constraint
 * between this proxy and Railway — we strip origin/host so Railway's
 * CORS middleware never rejects the request.
 *
 * CORS preflights (OPTIONS) are handled here directly and never
 * forwarded to Railway.
 */
import { NextRequest, NextResponse } from 'next/server';

const BACKEND =
  process.env.BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  'http://localhost:8000';

// Headers we never forward to Railway
const STRIP_REQUEST_HEADERS = new Set([
  'host',
  'origin',
  'referer',
  'x-forwarded-host',
]);

// CORS headers returned on every preflight + real response
const CORS_HEADERS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
  'Access-Control-Allow-Headers': 'Authorization,Content-Type,X-Requested-With',
  'Access-Control-Max-Age':       '86400',
};

// ---------------------------------------------------------------------------
// OPTIONS — answer preflight here, never hit Railway
// ---------------------------------------------------------------------------
export async function OPTIONS() {
  return new NextResponse(null, { status: 200, headers: CORS_HEADERS });
}

// ---------------------------------------------------------------------------
// Shared proxy handler
// ---------------------------------------------------------------------------
async function handler(req: NextRequest): Promise<NextResponse> {
  // Reconstruct target URL: strip the /api/proxy prefix
  const url = new URL(req.url);
  const stripped = url.pathname.replace(/^\/api\/proxy/, '');
  const target = `${BACKEND}${stripped}${url.search}`;

  // Build forwarded headers — drop the ones Railway must not see
  const forwardedHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (!STRIP_REQUEST_HEADERS.has(key.toLowerCase())) {
      forwardedHeaders[key] = value;
    }
  });

  try {
    const upstream = await fetch(target, {
      method:  req.method,
      headers: forwardedHeaders,
      body:    ['GET', 'HEAD'].includes(req.method) ? undefined : req.body,
      // @ts-expect-error — Node 18 fetch supports duplex for streaming bodies
      duplex: 'half',
    });

    // Stream response body back, attach CORS headers so browser is happy
    const responseHeaders = new Headers(CORS_HEADERS);
    upstream.headers.forEach((value, key) => {
      // Don't forward upstream CORS headers — ours override them
      if (!key.toLowerCase().startsWith('access-control-')) {
        responseHeaders.set(key, value);
      }
    });

    return new NextResponse(upstream.body, {
      status:  upstream.status,
      headers: responseHeaders,
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { detail: `Backend unreachable: ${msg}` },
      { status: 503, headers: CORS_HEADERS },
    );
  }
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
