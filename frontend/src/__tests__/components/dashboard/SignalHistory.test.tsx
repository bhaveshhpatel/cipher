/**
 * SignalHistory tests
 *
 * Strategy: mock useSignalHistory hook entirely. All rendering is deterministic
 * from the mocked return value — no network, no timers needed.
 *
 * Covers:
 *   - Loading spinner shown when loading=true and items=[]
 *   - Empty state shown when not loading and items=[]
 *   - Error banner shown when error is set
 *   - Table rows render ticker and recommendation for each item
 *   - Pagination controls shown when totalPages > 1
 *   - Pagination hidden when totalPages <= 1
 *   - Filter inputs rendered (ticker, direction, tier, min score)
 *   - Apply and Clear buttons rendered
 *   - Refresh countdown label rendered
 */
import React from 'react';
import { render, screen } from '@testing-library/react';

jest.mock('@/hooks/useSignalHistory', () => ({
  useSignalHistory: jest.fn(),
}));

import { SignalHistory } from '@/components/dashboard/SignalHistory';
import { useSignalHistory } from '@/hooks/useSignalHistory';
import type { SignalHistoryItem } from '@/lib/api';

const mockUseSignalHistory = useSignalHistory as jest.Mock;

const makeItem = (overrides: Partial<SignalHistoryItem> = {}): SignalHistoryItem => ({
  id:              '1',
  ticker:          'AAPL',
  recommendation:  'BUY',
  composite_score: 0.82,
  flow_score:      0.74,
  backtest_score:  0.66,
  created_at:      '2026-04-30T10:00:00Z',
  influence_tier:  'WHALE',
  total_premium:   500_000,
  is_accelerating: false,
  direction:       'bullish',
  ...overrides,
} as SignalHistoryItem);

const baseMock = {
  items:    [],
  total:    0,
  loading:  false,
  error:    null,
  page:     1,
  pageSize: 20,
  fetch:    jest.fn(),
  setPage:  jest.fn(),
};

beforeEach(() => {
  jest.clearAllMocks();
  mockUseSignalHistory.mockReturnValue({ ...baseMock, fetch: jest.fn(), setPage: jest.fn() });
});

test('renders loading spinner when loading and no items', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, loading: true, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/Loading signal history/)).toBeInTheDocument();
});

test('renders empty state when not loading and no items', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/No signals yet/)).toBeInTheDocument();
});

test('renders error banner when error is set', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, error: 'Failed to load signals', fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('Failed to load signals')).toBeInTheDocument();
});

test('renders table row with ticker and recommendation', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, items: [makeItem()], total: 1, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('AAPL')).toBeInTheDocument();
  expect(screen.getByText('BUY')).toBeInTheDocument();
});

test('renders multiple rows', () => {
  const items = [
    makeItem({ id: '1', ticker: 'AAPL', recommendation: 'BUY'  }),
    makeItem({ id: '2', ticker: 'SPY',  recommendation: 'SELL' }),
    makeItem({ id: '3', ticker: 'NVDA', recommendation: 'HOLD' }),
  ];
  mockUseSignalHistory.mockReturnValue({ ...baseMock, items, total: 3, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('AAPL')).toBeInTheDocument();
  expect(screen.getByText('SPY')).toBeInTheDocument();
  expect(screen.getByText('NVDA')).toBeInTheDocument();
});

test('pagination controls shown when totalPages > 1', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, items: [makeItem()], total: 60, page: 1, pageSize: 20, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/Page 1 \/ 3/)).toBeInTheDocument();
  expect(screen.getByText('← Prev')).toBeInTheDocument();
  expect(screen.getByText('Next →')).toBeInTheDocument();
});

test('pagination controls hidden when all items fit on one page', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, items: [makeItem()], total: 1, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.queryByText('← Prev')).not.toBeInTheDocument();
});

test('renders filter inputs and action buttons', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByPlaceholderText('All tickers')).toBeInTheDocument();
  expect(screen.getByText('Apply')).toBeInTheDocument();
  expect(screen.getByText('Clear')).toBeInTheDocument();
});

test('renders refresh countdown label', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/refresh in/)).toBeInTheDocument();
});

test('total signal count shown when total > 0', () => {
  mockUseSignalHistory.mockReturnValue({ ...baseMock, items: [makeItem()], total: 42, fetch: jest.fn(), setPage: jest.fn() });
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/42 signals/)).toBeInTheDocument();
});
