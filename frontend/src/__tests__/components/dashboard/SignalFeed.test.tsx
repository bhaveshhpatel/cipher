/**
 * SignalFeed — coverage (P7)
 *
 * Covers:
 *   - Empty state variants (connecting / waiting for live)
 *   - Ticker + direction rendered per signal card
 *   - Alert level text rendered (underscore replaced with space)
 *   - CALL / PUT contract_type badge
 *   - ⚡ Accel badge shown/hidden based on is_accelerating
 *   - trade_count rendered when > 0
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { SignalFeed } from '@/components/dashboard/SignalFeed';
import type { WsSignal } from '@/hooks/useSignalStream';

jest.mock('@/lib/api', () => ({
  api: { getSignalHistory: jest.fn().mockResolvedValue({ signals: [] }) },
}));

const makeSignal = (overrides: Partial<WsSignal> = {}): WsSignal => ({
  ticker:           'AAPL',
  direction:        'BUY',
  alert_level:      'CONVICTION',
  conviction_score: 0.85,
  total_premium:    500_000,
  trade_count:      12,
  is_accelerating:  false,
  timestamp:        '2026-04-30T10:00:00Z',
  ...overrides,
});

beforeEach(() => jest.clearAllMocks());

test('shows connecting text when not connected and no signals', () => {
  render(<SignalFeed signals={[]} connected={false} token={null} />);
  expect(screen.getByText(/connecting to stream/i)).toBeInTheDocument();
});

test('shows waiting for live signals text when connected and no signals', () => {
  render(<SignalFeed signals={[]} connected={true} token={null} />);
  expect(screen.getByText(/waiting for live signals/i)).toBeInTheDocument();
});

test('renders ticker for each signal card', () => {
  const signals = [makeSignal({ ticker: 'TSLA' }), makeSignal({ ticker: 'SPY' })];
  render(<SignalFeed signals={signals} connected={true} token={null} />);
  expect(screen.getByText('TSLA')).toBeInTheDocument();
  expect(screen.getByText('SPY')).toBeInTheDocument();
});

test('renders direction label for each signal', () => {
  render(<SignalFeed signals={[makeSignal({ direction: 'SELL' })]} connected={true} token={null} />);
  expect(screen.getByText('SELL')).toBeInTheDocument();
});

test('renders alert level with underscore replaced by space', () => {
  render(<SignalFeed signals={[makeSignal({ alert_level: 'STRONG_SIGNAL' })]} connected={true} token={null} />);
  expect(screen.getByText('STRONG SIGNAL')).toBeInTheDocument();
});

test('renders CALL badge when contract_type is CALL', () => {
  render(<SignalFeed signals={[makeSignal({ contract_type: 'CALL' })]} connected={true} token={null} />);
  expect(screen.getByText('CALL')).toBeInTheDocument();
});

test('renders PUT badge when contract_type is PUT', () => {
  render(<SignalFeed signals={[makeSignal({ contract_type: 'PUT' })]} connected={true} token={null} />);
  expect(screen.getByText('PUT')).toBeInTheDocument();
});

test('renders Accel badge when is_accelerating is true', () => {
  render(<SignalFeed signals={[makeSignal({ is_accelerating: true })]} connected={true} token={null} />);
  expect(screen.getByText('⚡ Accel')).toBeInTheDocument();
});

test('does not render Accel badge when is_accelerating is false', () => {
  render(<SignalFeed signals={[makeSignal({ is_accelerating: false })]} connected={true} token={null} />);
  expect(screen.queryByText('⚡ Accel')).not.toBeInTheDocument();
});

test('renders trade count when > 0', () => {
  render(<SignalFeed signals={[makeSignal({ trade_count: 8 })]} connected={true} token={null} />);
  expect(screen.getByText('8 trades')).toBeInTheDocument();
});
