/**
 * StreamStatsBar tests
 *
 * Covers:
 *   - All stat items render (active_symbols, ticks, classified, signals)
 *   - classify rate computed correctly (ticks > 0)
 *   - classify rate shows dash when ticks === 0
 *   - Errors item only shown when errors > 0
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { StreamStatsBar } from '@/components/dashboard/StreamStatsBar';
import type { StreamStats } from '@/lib/api';

const makeStats = (overrides: Partial<StreamStats> = {}): StreamStats => ({
  active_symbols: 5,
  ticks:          100,
  classified:     80,
  signals:        3,
  errors:         0,
  ...overrides,
} as StreamStats);

beforeEach(() => jest.clearAllMocks());

test('renders active_symbols, ticks, classified, signals', () => {
  render(<StreamStatsBar stats={makeStats()} />);
  expect(screen.getByText('5')).toBeInTheDocument();
  expect(screen.getByText('100')).toBeInTheDocument();
  expect(screen.getByText('80')).toBeInTheDocument();
  expect(screen.getByText('3')).toBeInTheDocument();
});

test('renders all stat labels', () => {
  render(<StreamStatsBar stats={makeStats()} />);
  expect(screen.getByText('Active Symbols')).toBeInTheDocument();
  expect(screen.getByText('Ticks')).toBeInTheDocument();
  expect(screen.getByText('Classified')).toBeInTheDocument();
  expect(screen.getByText('Classify Rate')).toBeInTheDocument();
  expect(screen.getByText('Signals')).toBeInTheDocument();
});

test('classify rate computed correctly when ticks > 0', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 100, classified: 80 })} />);
  expect(screen.getByText('80.0%')).toBeInTheDocument();
});

test('classify rate is 100% when all ticks classified', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 50, classified: 50 })} />);
  expect(screen.getByText('100.0%')).toBeInTheDocument();
});

test('classify rate shows dash when ticks is 0', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 0, classified: 0 })} />);
  expect(screen.getByText('—')).toBeInTheDocument();
});

test('errors item not rendered when errors === 0', () => {
  render(<StreamStatsBar stats={makeStats({ errors: 0 })} />);
  expect(screen.queryByText('Errors')).not.toBeInTheDocument();
});

test('errors item rendered when errors > 0', () => {
  render(<StreamStatsBar stats={makeStats({ errors: 4 })} />);
  expect(screen.getByText('Errors')).toBeInTheDocument();
  expect(screen.getByText('4')).toBeInTheDocument();
});
