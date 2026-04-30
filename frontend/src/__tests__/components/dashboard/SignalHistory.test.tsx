/**
 * SignalHistory — coverage (P7)
 *
 * Covers:
 *   - Filter bar renders (ticker input, direction select, tier select, min score select)
 *   - Loading spinner shown when loading=true and items=[]
 *   - Empty state shown when loading=false and items=[]
 *   - Table rows render items with ticker + recommendation
 *   - Error banner shown when error present
 *   - Pagination rendered when total > pageSize
 *   - Prev button disabled on page 1
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { SignalHistory } from '@/components/dashboard/SignalHistory';
import type { SignalHistoryItem } from '@/lib/api';

jest.mock('@/hooks/useSignalHistory');
import { useSignalHistory } from '@/hooks/useSignalHistory';
const mockUseSignalHistory = useSignalHistory as jest.MockedFunction<typeof useSignalHistory>;

const defaultHook = (): ReturnType<typeof useSignalHistory> => ({
  items:    [],
  total:    0,
  loading:  false,
  error:    null,
  page:     1,
  pageSize: 20,
  fetch:    jest.fn(),
  setPage:  jest.fn(),
});

const makeItem = (overrides: Partial<SignalHistoryItem> = {}): SignalHistoryItem => ({
  id:              '1',
  ticker:          'AAPL',
  recommendation:  'BUY',
  composite_score: 0.80,
  flow_score:      0.75,
  backtest_score:  0.70,
  influence_tier:  'WHALE',
  total_premium:   500_000,
  is_accelerating: false,
  created_at:      '2026-04-30T10:00:00Z',
  direction:       'bullish',
  ...overrides,
});

beforeEach(() => {
  jest.useFakeTimers();
  mockUseSignalHistory.mockReturnValue(defaultHook());
});

afterEach(() => {
  jest.useRealTimers();
  jest.clearAllMocks();
});

test('renders ticker input filter', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByPlaceholderText('All tickers')).toBeInTheDocument();
});

test('renders direction select filter', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByText('Direction')).toBeInTheDocument();
});

test('renders tier select filter', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByText('Tier')).toBeInTheDocument();
});

test('renders loading spinner when loading=true and items=[]', () => {
  mockUseSignalHistory.mockReturnValue({ ...defaultHook(), loading: true });
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/loading signal history/i)).toBeInTheDocument();
});

test('renders empty state when loading=false and items=[]', () => {
  render(<SignalHistory token={null} />);
  expect(screen.getByText(/no signals yet/i)).toBeInTheDocument();
});

test('renders item ticker and recommendation in table', () => {
  mockUseSignalHistory.mockReturnValue({
    ...defaultHook(),
    items: [makeItem({ ticker: 'TSLA', recommendation: 'SELL' })],
    total: 1,
  });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('TSLA')).toBeInTheDocument();
  expect(screen.getByText('SELL')).toBeInTheDocument();
});

test('renders error banner when error is present', () => {
  mockUseSignalHistory.mockReturnValue({ ...defaultHook(), error: 'Network error' });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('Network error')).toBeInTheDocument();
});

test('renders pagination when total > pageSize', () => {
  mockUseSignalHistory.mockReturnValue({
    ...defaultHook(),
    items: [makeItem()],
    total: 40,
    page:  1,
  });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('← Prev')).toBeInTheDocument();
  expect(screen.getByText('Next →')).toBeInTheDocument();
});

test('Prev button is disabled on page 1', () => {
  mockUseSignalHistory.mockReturnValue({
    ...defaultHook(),
    items: [makeItem()],
    total: 40,
    page:  1,
  });
  render(<SignalHistory token={null} />);
  expect(screen.getByText('← Prev')).toBeDisabled();
});
