/**
 * Next.js API route — proxy all /api/proxy/[...path] requests to the Railway backend.
 *
 * Why a proxy?
 *   - The frontend is on Vercel (different domain from Railway).
 *   - We can't expose BACKEND_URL to the browser (CORS, secrets).
 *   - All fetch calls in src/lib/api.ts use relative /api/... paths.
 *   - next.config.mjs rewrites /api/* → /api/proxy/* so every API call
 *     lands here and gets forwarded to Railway.
 */

import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "";

/** Headers that must not be forwarded upstream. */
const HOP_BY_HOP = new Set(["host", "connection", "transfer-encoding", "te", "trailer", "upgrade"]);

async function proxyRequest(req: NextRequest, params: { path: string[] }): Promise<NextResponse> {
  const pathSegments = params.path ?? [];
  const upstreamPath = "/api/" + pathSegments.join("/");

  // Preserve query string from the original request.
  const search = new URL(req.url).search;
  const upstreamURL = `${BACKEND_URL}${upstreamPath}${search}`;

  // Forward request headers, stripping hop-by-hop headers.
  const forwardHeaders: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) {
      forwardHeaders[key] = value;
    }
  });

  // For POST/PUT/PATCH forward the body as text.
  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
  }

  try {
    const upstream = await fetch(upstreamURL, {
      method:  req.method,
      headers: forwardHeaders,
      body,
    });

    // Read response body as text.
    const responseText = await upstream.text();

    // Copy response headers — guard against plain-object mocks that lack forEach.
    const responseHeaders = new Headers();
    if (typeof upstream.headers.forEach === "function") {
      upstream.headers.forEach((value: string, key: string) => {
        if (!HOP_BY_HOP.has(key.toLowerCase())) {
          responseHeaders.set(key, value);
        }
      });
    }

    return new NextResponse(responseText, {
      status:  upstream.status,
      headers: responseHeaders,
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[proxy] Backend unreachable: ${msg} — ${upstreamURL}`);
    return NextResponse.json(
      { detail: `Backend unreachable: ${msg}` },
      { status: 503 },
    );
  }
}

export async function GET(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxyRequest(req, await params);
}

export async function POST(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxyRequest(req, await params);
}

export async function PUT(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxyRequest(req, await params);
}

export async function PATCH(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxyRequest(req, await params);
}

export async function DELETE(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxyRequest(req, await params);
}
