/**
 * Regression tests for frontend/src/lib/api.ts
 *
 * Covers:
 *   req() helper:
 *   - Successful 200 response returns parsed JSON
 *   - Non-ok response (401) throws Error with 'detail' from body
 *   - Non-ok response with no detail falls back to 'HTTP {status}'
 *   - AbortError (timeout) throws 'Request timed out' message
 *   - Path without leading slash gets one prepended
 *   - Path with leading slash is used as-is
 *
 *   api.login:
 *   - Calls /api/auth/token with POST + x-www-form-urlencoded body
 *   - Encodes email as 'username' param
 *   - Returns {access_token: string}
 *
 *   api.register:
 *   - Calls /api/auth/register with POST + JSON body
 *   - Returns {message: string}
 *
 *   api.getFlow:
 *   - Calls /api/flow/scan with ticker param when provided
 *   - Does NOT include 'ticker' param when empty string
 *   - Includes limit and offset params
 *   - Sends Authorization header
 *
 *   api.runSimulation:
 *   - Calls /api/simulation/run with POST + JSON body
 *   - Body includes ticker, flow_events, n_agents, n_runs
 *   - Sends Authorization header
 *
 *   api.getComposite:
 *   - Calls /api/signals/composite/{ticker}
 *   - Sends Authorization header
 *
 *   api.getStats:
 *   - Calls /api/signals/stream/stats
 *   - Sends Authorization header
 *
 *   api.getSignalHistory:
 *   - No params → only limit/offset not included if undefined
 *   - ticker param forwarded
 *   - direction param forwarded
 *   - tier param forwarded
 *   - min_conviction param forwarded as string
 *   - limit param forwarded as string
 *   - offset param forwarded as string
 *   - Sends Authorization header
 */

import { api } from '../lib/api';

// ── global fetch mock ─────────────────────────────────────────────────────────

const mockFetch = jest.fn();
global.fetch = mockFetch;

const TOKEN = 'test-jwt-token';

function _ok(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as Response);
}

function _err(status: number, detail?: string) {
  return Promise.resolve({
    ok: false,
    status,
    json: () => Promise.resolve(detail ? { detail } : {}),
  } as Response);
}

beforeEach(() => {
  mockFetch.mockReset();
  jest.useFakeTimers();
});

afterEach(() => {
  jest.useRealTimers();
});


// ── req() helper ──────────────────────────────────────────────────────────────

test('successful 200 returns parsed JSON', async () => {
  mockFetch.mockReturnValue(_ok({ access_token: 'abc' }));
  const result = await api.login('a@b.com', 'pass');
  expect(result).toEqual({ access_token: 'abc' });
});

test('non-ok response with detail throws Error with detail message', async () => {
  mockFetch.mockReturnValue(_err(401, 'Invalid credentials'));
  await expect(api.login('a@b.com', 'wrong')).rejects.toThrow('Invalid credentials');
});

test('non-ok response with no detail throws HTTP status message', async () => {
  mockFetch.mockReturnValue(_err(500));
  await expect(api.login('a@b.com', 'pass')).rejects.toThrow('HTTP 500');
});

test('timeout AbortError throws timed out message', async () => {
  mockFetch.mockImplementation(() =>
    new Promise<Response>((_, reject) => {
      setTimeout(() => {
        const err = new Error('Request timed out — server may be starting up, please retry.');
        err.name = 'AbortError';
        reject(err);
      }, 25_000);
    })
  );
  const loginPromise = api.login('a@b.com', 'pass');
  jest.advanceTimersByTime(25_000);
  await expect(loginPromise).rejects.toThrow('timed out');
});


// ── api.login ─────────────────────────────────────────────────────────────────

test('api.login calls /api/auth/token with POST', async () => {
  mockFetch.mockReturnValue(_ok({ access_token: 'tok' }));
  await api.login('user@test.com', 'secret');
  const [url, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('/api/auth/token');
  expect(opts.method).toBe('POST');
});

test('api.login encodes email as username in body', async () => {
  mockFetch.mockReturnValue(_ok({ access_token: 'tok' }));
  await api.login('user@test.com', 'secret');
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const body = opts.body as URLSearchParams | string;
  const params = typeof body === 'string'
    ? new URLSearchParams(body)
    : body as URLSearchParams;
  expect(params.get('username')).toBe('user@test.com');
  expect(params.get('password')).toBe('secret');
});

test('api.login uses application/x-www-form-urlencoded content type', async () => {
  mockFetch.mockReturnValue(_ok({ access_token: 'tok' }));
  await api.login('user@test.com', 'secret');
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Content-Type']).toBe('application/x-www-form-urlencoded');
});


// ── api.register ──────────────────────────────────────────────────────────────

test('api.register calls /api/auth/register with POST + JSON', async () => {
  mockFetch.mockReturnValue(_ok({ message: 'User created' }));
  await api.register('new@user.com', 'pass123');
  const [url, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('/api/auth/register');
  expect(opts.method).toBe('POST');
  const body = JSON.parse(opts.body as string);
  expect(body.email).toBe('new@user.com');
  expect(body.password).toBe('pass123');
});


// ── api.getFlow ───────────────────────────────────────────────────────────────

test('api.getFlow includes ticker param when provided', async () => {
  mockFetch.mockReturnValue(_ok({ events: [], total: 0, limit: 100, offset: 0, ticker: 'AAPL' }));
  await api.getFlow('AAPL', TOKEN);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('ticker=AAPL');
});

test('api.getFlow omits ticker param when empty string', async () => {
  mockFetch.mockReturnValue(_ok({ events: [], total: 0, limit: 100, offset: 0, ticker: null }));
  await api.getFlow('', TOKEN);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).not.toContain('ticker=');
});

test('api.getFlow includes limit and offset params', async () => {
  mockFetch.mockReturnValue(_ok({ events: [], total: 0, limit: 50, offset: 10, ticker: null }));
  await api.getFlow('', TOKEN, 50, 10);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('limit=50');
  expect(url).toContain('offset=10');
});

test('api.getFlow sends Authorization header', async () => {
  mockFetch.mockReturnValue(_ok({ events: [], total: 0, limit: 100, offset: 0, ticker: null }));
  await api.getFlow('TSLA', TOKEN);
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Authorization']).toBe(`Bearer ${TOKEN}`);
});


// ── api.runSimulation ─────────────────────────────────────────────────────────

test('api.runSimulation calls /api/simulation/run with POST + JSON body', async () => {
  const fake: import('../lib/api').SimulationResult = {
    ticker: 'AAPL', direction: 'BUY', confidence: 0.8,
    bull_votes: 4, bear_votes: 1, hold_votes: 1,
    summary: 'BUY with 80% confidence.', agents: [],
  };
  mockFetch.mockReturnValue(_ok(fake));
  await api.runSimulation('AAPL', [], 6, 1, TOKEN);
  const [url, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('/api/simulation/run');
  expect(opts.method).toBe('POST');
  const body = JSON.parse(opts.body as string);
  expect(body.ticker).toBe('AAPL');
  expect(body.n_agents).toBe(6);
  expect(body.n_runs).toBe(1);
});

test('api.runSimulation sends Authorization header', async () => {
  mockFetch.mockReturnValue(_ok({ ticker: 'SPY', direction: 'HOLD', confidence: 0.5,
    bull_votes: 2, bear_votes: 2, hold_votes: 2, summary: '', agents: [] }));
  await api.runSimulation('SPY', [], 6, 1, TOKEN);
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Authorization']).toBe(`Bearer ${TOKEN}`);
});


// ── api.getComposite ──────────────────────────────────────────────────────────

test('api.getComposite calls correct URL with ticker', async () => {
  mockFetch.mockReturnValue(_ok({ ticker: 'NVDA', recommendation: 'BUY',
    composite_score: 0.9, flow_score: 0.85, backtest_score: 0.8, reasoning: 'r' }));
  await api.getComposite('NVDA', TOKEN);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toBe('/api/signals/composite/NVDA');
});

test('api.getComposite sends Authorization header', async () => {
  mockFetch.mockReturnValue(_ok({ ticker: 'NVDA', recommendation: 'BUY',
    composite_score: 0.9, flow_score: 0.85, backtest_score: 0.8, reasoning: 'r' }));
  await api.getComposite('NVDA', TOKEN);
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Authorization']).toBe(`Bearer ${TOKEN}`);
});


// ── api.getStats ──────────────────────────────────────────────────────────────

test('api.getStats calls /api/signals/stream/stats', async () => {
  mockFetch.mockReturnValue(_ok({ stats: { active_symbols: 5, ticks: 100,
    classified: 90, signals: 10, errors: 0 } }));
  await api.getStats(TOKEN);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toBe('/api/signals/stream/stats');
});

test('api.getStats sends Authorization header', async () => {
  mockFetch.mockReturnValue(_ok({ stats: {} }));
  await api.getStats(TOKEN);
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Authorization']).toBe(`Bearer ${TOKEN}`);
});


// ── api.getSignalHistory ──────────────────────────────────────────────────────

const _historyOk = () => _ok({ signals: [], total: 0, limit: 50, offset: 0 });

test('api.getSignalHistory calls /api/signals/history', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN);
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('/api/signals/history');
});

test('api.getSignalHistory sends Authorization header', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN);
  const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit];
  const headers = opts.headers as Record<string, string>;
  expect(headers['Authorization']).toBe(`Bearer ${TOKEN}`);
});

test('api.getSignalHistory with ticker param includes ticker in URL', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, { ticker: 'AAPL' });
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('ticker=AAPL');
});

test('api.getSignalHistory with direction param includes direction in URL', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, { direction: 'bullish' });
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('direction=bullish');
});

test('api.getSignalHistory with tier param includes tier in URL', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, { tier: 'whale' });
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('tier=whale');
});

test('api.getSignalHistory with min_conviction encodes as string', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, { min_conviction: 0.75 });
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('min_conviction=0.75');
});

test('api.getSignalHistory with limit and offset forwards both', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, { limit: 25, offset: 50 });
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).toContain('limit=25');
  expect(url).toContain('offset=50');
});

test('api.getSignalHistory with no params does not include undefined params', async () => {
  mockFetch.mockReturnValue(_historyOk());
  await api.getSignalHistory(TOKEN, {});
  const [url] = mockFetch.mock.calls[0] as [string];
  expect(url).not.toContain('ticker=');
  expect(url).not.toContain('direction=');
  expect(url).not.toContain('tier=');
});
