import '@testing-library/jest-dom';

/**
 * Polyfill Response.json static method.
 *
 * jsdom does not implement Response.json (a WHATWG Fetch spec addition).
 * NextResponse.json() calls it internally, so without this polyfill
 * the proxy route handler test fails with "Response.json is not a function".
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
if (!(Response as any).json) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (Response as any).json = (data: unknown, init?: ResponseInit) =>
    new Response(JSON.stringify(data), {
      status: 200,
      ...init,
      headers: { 'Content-Type': 'application/json' },
    });
}

// eslint-disable-next-line @typescript-eslint/no-require-imports
require('jest-fetch-mock').enableMocks();
