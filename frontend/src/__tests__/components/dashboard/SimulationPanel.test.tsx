/**
 * SimulationPanel — coverage (P7)
 *
 * Covers:
 *   - Loading state renders progress percentage
 *   - Error state renders error message
 *   - Empty state rendered when result=null
 *   - Verdict direction rendered
 *   - Summary text rendered
 *   - Agent votes section present
 *   - Agent reasoning rendered when agents provided
 *   - Confidence percentage rendered in ring
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { SimulationPanel } from '@/components/dashboard/SimulationPanel';
import type { SimulationResult } from '@/lib/api';

const makeResult = (overrides: Partial<SimulationResult> = {}): SimulationResult => ({
  ticker:     'AAPL',
  direction:  'BUY',
  confidence: 0.75,
  summary:    'Strong bullish consensus.',
  bull_votes: 7,
  bear_votes: 2,
  hold_votes: 1,
  agents:     [],
  ...overrides,
});

test('renders progress percentage during loading', () => {
  render(<SimulationPanel result={null} loading={true} error={null} progress={45} />);
  expect(screen.getByText('45%')).toBeInTheDocument();
});

test('renders running simulation text during loading', () => {
  render(<SimulationPanel result={null} loading={true} error={null} progress={0} />);
  expect(screen.getByText(/running ai swarm simulation/i)).toBeInTheDocument();
});

test('renders error message', () => {
  render(<SimulationPanel result={null} loading={false} error="Simulation timed out" progress={0} />);
  expect(screen.getByText(/simulation timed out/i)).toBeInTheDocument();
});

test('renders empty state when result is null', () => {
  render(<SimulationPanel result={null} loading={false} error={null} progress={0} />);
  expect(screen.getByText(/no simulation results yet/i)).toBeInTheDocument();
});

test('renders verdict direction', () => {
  render(<SimulationPanel result={makeResult({ direction: 'SELL' })} loading={false} error={null} progress={0} />);
  expect(screen.getAllByText('SELL').length).toBeGreaterThan(0);
});

test('renders summary text', () => {
  render(<SimulationPanel result={makeResult({ summary: 'Bearish pressure mounting.' })} loading={false} error={null} progress={0} />);
  expect(screen.getByText('Bearish pressure mounting.')).toBeInTheDocument();
});

test('renders agent vote labels BUY SELL HOLD', () => {
  render(<SimulationPanel result={makeResult()} loading={false} error={null} progress={0} />);
  expect(screen.getByText('Agent Votes')).toBeInTheDocument();
  expect(screen.getByText('HOLD')).toBeInTheDocument();
});

test('renders agent reasoning when agents provided', () => {
  const result = makeResult({
    agents: [{ role: 'TREND', direction: 'BUY', reasoning: 'Uptrend firmly established.' }],
  });
  render(<SimulationPanel result={result} loading={false} error={null} progress={0} />);
  expect(screen.getByText('Uptrend firmly established.')).toBeInTheDocument();
  expect(screen.getByText('TREND')).toBeInTheDocument();
});

test('renders confidence percentage in ring', () => {
  render(<SimulationPanel result={makeResult({ confidence: 0.75 })} loading={false} error={null} progress={0} />);
  expect(screen.getByText('75')).toBeInTheDocument();
});
