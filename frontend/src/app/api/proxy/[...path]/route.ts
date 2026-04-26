import { NextRequest, NextResponse } from "next/server";

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

const BACKEND_URL =
  process.env.BACKEND_URL ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "http://localhost:8000";

function buildBackendUrl(pathSegments: string[], search: string): string {
  const joined = pathSegments.join("/");
  return search
    ? `${BACKEND_URL}/api/${joined}?${search}`
    : `${BACKEND_URL}/api/${joined}`;
}

function forwardHeaders(req: NextRequest): Record<string, string> {
  const headers: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (key.toLowerCase() === "host") return; // strip host
    headers[key] = value;
  });
  return headers;
}

async function proxyRequest(
  req: NextRequest,
  context: RouteContext
): Promise<NextResponse> {
  try {
    const { path } = await context.params;
    const { search } = new URL(req.url);
    const searchStr = search.startsWith("?") ? search.slice(1) : search;
    const backendUrl = buildBackendUrl(path, searchStr);
    const headers = forwardHeaders(req);

    const hasBody = req.method !== "GET" && req.method !== "HEAD";
    const body = hasBody ? await req.text() : undefined;

    const backendRes = await fetch(backendUrl, {
      method: req.method,
      headers,
      body,
    });

    const text = await backendRes.text();
    const contentType = backendRes.headers.get("content-type") ?? "application/json";

    return new NextResponse(text, {
      status: backendRes.status,
      headers: { "content-type": contentType },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { detail: `Backend unreachable: ${message}` },
      { status: 503 }
    );
  }
}

export async function GET(req: NextRequest, context: RouteContext) {
  return proxyRequest(req, context);
}

export async function POST(req: NextRequest, context: RouteContext) {
  return proxyRequest(req, context);
}

export async function PUT(req: NextRequest, context: RouteContext) {
  return proxyRequest(req, context);
}

export async function PATCH(req: NextRequest, context: RouteContext) {
  return proxyRequest(req, context);
}

export async function DELETE(req: NextRequest, context: RouteContext) {
  return proxyRequest(req, context);
}
