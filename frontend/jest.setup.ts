import '@testing-library/jest-dom';

// Must run first so jest-fetch-mock injects its fetch/Response globals
// before any polyfill logic reads them.
// eslint-disable-next-line @typescript-eslint/no-require-imports
require('jest-fetch-mock').enableMocks();

/**
 * Polyfill Response.json static method.
 *
 * The WHATWG static Response.json() is absent in:
 *   - Node environments (proxy.test.ts uses @jest-environment node)
 *   - older jsdom builds
 *
 * NextResponse.json() delegates to it internally, causing
 * "Response.json is not a function" in the proxy 503 test.
 *
 * Guard with typeof so this is a no-op in environments where
 * Response itself is undefined (avoids ReferenceError).
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
if (typeof globalThis.Response !== 'undefined' && !(globalThis.Response as any).json) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis.Response as any).json = (data: unknown, init?: ResponseInit) =>
    new globalThis.Response(JSON.stringify(data), {
      status: 200,
      ...init,
      headers: { 'Content-Type': 'application/json' },
    });
}
