/**
 * SignalFeed tests
 *
 * Covers:
 *   - EmptyState: connecting variant (connected=false, token=null)
 *   - EmptyState: waiting variant (connected=true, no signals)
 *   - Signal cards render with ticker, direction, alert_level
 *   - STRONG_SIGNAL label formatted as "STRONG SIGNAL"
 *   - Accelerating badge only shown when is_accelerating=true
 *   - Multiple signals render all cards
 *   - trade_count shown when > 0
 *   - contract_type badge rendered when present
 *
 * Strategy: pass token=null to skip the DB poll useEffect entirely.
 * api.getSignalHistory is mocked to guard against accidental calls.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';

jest.mock('@/lib/api', () => ({
  api: {
    getSignalHistory: jest.fn().mockResolvedValue({ signals: [] }),
  },
}));

import { SignalFeed } from '@/components/dashboard/SignalFeed';
import type { WsSignal } from '@/hooks/useSignalStream';

const makeSignal = (overrides: Partial<WsSignal> = {}): WsSignal => ({
  ticker:           'AAPL',
  direction:        'BUY',
  alert_level:      'CONVICTION',
  conviction_score: 0.85,
  total_premium:    500_000,
  trade_count:      12,
  is_accelerating:  false,
  timestamp:        new Date('2026-04-30T10:00:00Z').toISOString(),
  ...overrides,
} as WsSignal);

beforeEach(() => jest.clearAllMocks());

test('renders connecting empty state when not connected and no token', () => {
  render(<SignalFeed signals={[]} connected={false} token={null} />);
  expect(screen.getByText(/Connecting to stream/)).toBeInTheDocument();
});

test('renders waiting empty state when connected but no signals', () => {
  render(<SignalFeed signals={[]} connected={true} token={null} />);
  expect(screen.getByText(/Waiting for live signals/)).toBeInTheDocument();
});

test('renders signal card with ticker and direction', () => {
  render(<SignalFeed signals={[makeSignal()]} connected={true} token={null} />);
  expect(screen.getByText('AAPL')).toBeInTheDocument();
  expect(screen.getByText('BUY')).toBeInTheDocument();
});

test('renders CONVICTION alert level label', () => {
  render(<SignalFeed signals={[makeSignal({ alert_level: 'CONVICTION' })]} connected={true} token={null} />);
  expect(screen.getByText('CONVICTION')).toBeInTheDocument();
});

test('renders STRONG_SIGNAL as "STRONG SIGNAL" (underscore replaced)', () => {
  render(<SignalFeed signals={[makeSignal({ alert_level: 'STRONG_SIGNAL' })]} connected={true} token={null} />);
  expect(screen.getByText('STRONG SIGNAL')).toBeInTheDocument();
});

test('renders ALERT level label', () => {
  render(<SignalFeed signals={[makeSignal({ alert_level: 'ALERT' })]} connected={true} token={null} />);
  expect(screen.getByText('ALERT')).toBeInTheDocument();
});

test('renders accelerating badge when is_accelerating=true', () => {
  render(<SignalFeed signals={[makeSignal({ is_accelerating: true })]} connected={true} token={null} />);
  expect(screen.getByText('⚡ Accel')).toBeInTheDocument();
});

test('does not render accelerating badge when is_accelerating=false', () => {
  render(<SignalFeed signals={[makeSignal({ is_accelerating: false })]} connected={true} token={null} />);
  expect(screen.queryByText('⚡ Accel')).not.toBeInTheDocument();
});

test('renders trade_count when > 0', () => {
  render(<SignalFeed signals={[makeSignal({ trade_count: 9 })]} connected={true} token={null} />);
  expect(screen.getByText('9 trades')).toBeInTheDocument();
});

test('renders CALL contract_type badge when present', () => {
  render(<SignalFeed signals={[makeSignal({ contract_type: 'CALL' })]} connected={true} token={null} />);
  expect(screen.getByText('CALL')).toBeInTheDocument();
});

test('renders multiple signal cards', () => {
  const signals = [
    makeSignal({ ticker: 'AAPL', direction: 'BUY'  }),
    makeSignal({ ticker: 'SPY',  direction: 'SELL' }),
    makeSignal({ ticker: 'TSLA', direction: 'HOLD' }),
  ];
  render(<SignalFeed signals={signals} connected={true} token={null} />);
  expect(screen.getByText('AAPL')).toBeInTheDocument();
  expect(screen.getByText('SPY')).toBeInTheDocument();
  expect(screen.getByText('TSLA')).toBeInTheDocument();
});
