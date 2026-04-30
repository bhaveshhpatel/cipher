/**
 * StreamStatsBar — coverage (P7)
 *
 * Covers:
 *   - All stat items render with correct label and value
 *   - Classify rate calculated from ticks/classified
 *   - Classify rate shows em-dash when ticks=0
 *   - Errors item absent when errors=0
 *   - Errors item present when errors>0
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { StreamStatsBar } from '@/components/dashboard/StreamStatsBar';
import type { StreamStats } from '@/lib/api';

const makeStats = (overrides: Partial<StreamStats> = {}): StreamStats => ({
  active_symbols: 50,
  ticks:          200,
  classified:     100,
  signals:        10,
  errors:         0,
  ...overrides,
});

test('renders Active Symbols label and value', () => {
  render(<StreamStatsBar stats={makeStats({ active_symbols: 50 })} />);
  expect(screen.getByText('Active Symbols')).toBeInTheDocument();
  expect(screen.getByText('50')).toBeInTheDocument();
});

test('renders Ticks label and value', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 200 })} />);
  expect(screen.getByText('Ticks')).toBeInTheDocument();
  expect(screen.getByText('200')).toBeInTheDocument();
});

test('renders Classified label and value', () => {
  render(<StreamStatsBar stats={makeStats({ classified: 100 })} />);
  expect(screen.getByText('Classified')).toBeInTheDocument();
  expect(screen.getByText('100')).toBeInTheDocument();
});

test('calculates classify rate as (classified/ticks)*100 with one decimal', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 200, classified: 100 })} />);
  expect(screen.getByText('50.0%')).toBeInTheDocument();
});

test('shows em-dash for classify rate when ticks is 0', () => {
  render(<StreamStatsBar stats={makeStats({ ticks: 0, classified: 0 })} />);
  expect(screen.getByText('—')).toBeInTheDocument();
});

test('renders Signals label and value', () => {
  render(<StreamStatsBar stats={makeStats({ signals: 10 })} />);
  expect(screen.getByText('Signals')).toBeInTheDocument();
  expect(screen.getByText('10')).toBeInTheDocument();
});

test('does not render Errors item when errors is 0', () => {
  render(<StreamStatsBar stats={makeStats({ errors: 0 })} />);
  expect(screen.queryByText('Errors')).not.toBeInTheDocument();
});

test('renders Errors label and value when errors > 0', () => {
  render(<StreamStatsBar stats={makeStats({ errors: 3 })} />);
  expect(screen.getByText('Errors')).toBeInTheDocument();
  expect(screen.getByText('3')).toBeInTheDocument();
});
