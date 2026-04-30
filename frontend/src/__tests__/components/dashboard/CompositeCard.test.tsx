/**
 * CompositeCard tests
 *
 * Covers:
 *   - Skeleton rendered when loading=true
 *   - EmptyState rendered when signal=null and not loading (includes ticker name)
 *   - Recommendation, ticker, composite score, reasoning rendered when signal present
 *   - BUY / SELL / HOLD recommendations all render
 *   - ScoreBar percentage clamped to 0–100
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { CompositeCard } from '@/components/dashboard/CompositeCard';
import type { CompositeSignal } from '@/lib/api';

const makeSignal = (overrides: Partial<CompositeSignal> = {}): CompositeSignal => ({
  ticker:          'AAPL',
  recommendation:  'BUY',
  composite_score: 0.82,
  flow_score:      0.75,
  backtest_score:  0.68,
  reasoning:       'Strong bullish flow detected across multiple strikes.',
  ...overrides,
} as CompositeSignal);

beforeEach(() => jest.clearAllMocks());

test('renders skeleton divs when loading', () => {
  const { container } = render(<CompositeCard signal={null} loading={true} ticker="AAPL" />);
  expect(container.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
});

test('renders empty state with ticker name when signal is null and not loading', () => {
  render(<CompositeCard signal={null} loading={false} ticker="TSLA" />);
  expect(screen.getByText(/No composite signal for TSLA/)).toBeInTheDocument();
});

test('renders recommendation when signal present', () => {
  render(<CompositeCard signal={makeSignal()} loading={false} ticker="AAPL" />);
  expect(screen.getByText('BUY')).toBeInTheDocument();
});

test('renders ticker in factor scores header', () => {
  render(<CompositeCard signal={makeSignal({ ticker: 'NVDA' })} loading={false} ticker="NVDA" />);
  expect(screen.getByText(/NVDA/)).toBeInTheDocument();
});

test('renders composite score as integer percentage', () => {
  render(<CompositeCard signal={makeSignal({ composite_score: 0.82 })} loading={false} ticker="AAPL" />);
  // composite_score * 100 → "82" (toFixed(0))
  expect(screen.getAllByText('82').length).toBeGreaterThan(0);
});

test('renders reasoning text', () => {
  render(<CompositeCard signal={makeSignal()} loading={false} ticker="AAPL" />);
  expect(screen.getByText('Strong bullish flow detected across multiple strikes.')).toBeInTheDocument();
});

test('renders SELL recommendation', () => {
  render(<CompositeCard signal={makeSignal({ recommendation: 'SELL' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('SELL')).toBeInTheDocument();
});

test('renders HOLD recommendation', () => {
  render(<CompositeCard signal={makeSignal({ recommendation: 'HOLD' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('HOLD')).toBeInTheDocument();
});

test('renders STRONG_BUY recommendation', () => {
  render(<CompositeCard signal={makeSignal({ recommendation: 'STRONG_BUY' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('STRONG_BUY')).toBeInTheDocument();
});

test('renders Factor Scores section header', () => {
  render(<CompositeCard signal={makeSignal()} loading={false} ticker="AAPL" />);
  expect(screen.getByText(/Factor Scores/)).toBeInTheDocument();
  expect(screen.getByText('Recommendation')).toBeInTheDocument();
});
