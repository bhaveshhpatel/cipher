/**
 * CompositeCard — coverage (P7)
 *
 * Covers:
 *   - Skeleton rendered when loading=true
 *   - Empty state shown when signal=null
 *   - Factor scores heading includes ticker
 *   - Composite score rendered as integer percentage
 *   - BUY / SELL recommendation rendered
 *   - Reasoning text rendered
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { CompositeCard } from '@/components/dashboard/CompositeCard';
import type { CompositeSignal } from '@/lib/api';

const makeSignal = (overrides: Partial<CompositeSignal> = {}): CompositeSignal => ({
  ticker:          'AAPL',
  recommendation:  'BUY',
  composite_score: 0.78,
  flow_score:      0.82,
  backtest_score:  0.65,
  reasoning:       'Strong bullish flow with confirmed backtest.',
  ...overrides,
});

test('renders skeleton elements when loading', () => {
  const { container } = render(<CompositeCard signal={null} loading={true} ticker="AAPL" />);
  expect(container.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
});

test('renders empty state with ticker name when signal is null', () => {
  render(<CompositeCard signal={null} loading={false} ticker="MSFT" />);
  expect(screen.getByText(/no composite signal for MSFT/i)).toBeInTheDocument();
});

test('renders factor scores heading with ticker', () => {
  render(<CompositeCard signal={makeSignal()} loading={false} ticker="AAPL" />);
  expect(screen.getByText(/factor scores · aapl/i)).toBeInTheDocument();
});

test('renders composite score as integer percentage', () => {
  render(<CompositeCard signal={makeSignal({ composite_score: 0.78 })} loading={false} ticker="AAPL" />);
  expect(screen.getAllByText('78').length).toBeGreaterThan(0);
});

test('renders BUY recommendation', () => {
  render(<CompositeCard signal={makeSignal({ recommendation: 'BUY' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('BUY')).toBeInTheDocument();
});

test('renders SELL recommendation', () => {
  render(<CompositeCard signal={makeSignal({ recommendation: 'SELL' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('SELL')).toBeInTheDocument();
});

test('renders reasoning text', () => {
  render(<CompositeCard signal={makeSignal({ reasoning: 'Whale flow detected.' })} loading={false} ticker="AAPL" />);
  expect(screen.getByText('Whale flow detected.')).toBeInTheDocument();
});
