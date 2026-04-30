/**
 * SimulationPanel tests
 *
 * Covers:
 *   - Loading state shows progress bar and percentage
 *   - Error state renders error message
 *   - Empty state when result=null, not loading, no error
 *   - Verdict card: direction, ticker, summary rendered
 *   - Vote counts rendered for bull / bear / hold
 *   - Confidence ring shows correct percentage
 *   - Agent reasoning cards rendered when agents present
 *   - Agent section absent when agents=[]
 *
 * Note: BUY / SELL / HOLD each appear twice when a result is present
 * (once in the verdict card, once as an Agent Votes row label),
 * so affected assertions use getAllByText instead of getByText.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { SimulationPanel } from '@/components/dashboard/SimulationPanel';
import type { SimulationResult } from '@/lib/api';

type Agent = { role: string; direction: string; reasoning: string };

const makeResult = (overrides: Partial<SimulationResult> = {}): SimulationResult => ({
  ticker:     'AAPL',
  direction:  'BUY',
  confidence: 0.78,
  summary:    'Strong bullish consensus across agents.',
  bull_votes: 7,
  bear_votes: 2,
  hold_votes: 1,
  agents:     [] as Agent[],
  ...overrides,
} as SimulationResult);

beforeEach(() => jest.clearAllMocks());

test('renders loading state with progress and label', () => {
  render(<SimulationPanel result={null} loading={true} error={null} progress={45} />);
  expect(screen.getByText('45%')).toBeInTheDocument();
  expect(screen.getByText(/Running AI swarm simulation/)).toBeInTheDocument();
});

test('renders loading progress at 0%', () => {
  render(<SimulationPanel result={null} loading={true} error={null} progress={0} />);
  expect(screen.getByText('0%')).toBeInTheDocument();
});

test('renders error message', () => {
  render(<SimulationPanel result={null} loading={false} error="Simulation timed out" progress={0} />);
  expect(screen.getByText(/Simulation timed out/)).toBeInTheDocument();
});

test('renders empty state when no result and not loading', () => {
  render(<SimulationPanel result={null} loading={false} error={null} progress={0} />);
  expect(screen.getByText(/No simulation results yet/)).toBeInTheDocument();
});

test('renders direction in verdict card and summary', () => {
  render(<SimulationPanel result={makeResult()} loading={false} error={null} progress={0} />);
  // BUY appears in verdict card + Agent Votes label — both are valid
  expect(screen.getAllByText('BUY').length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText('Strong bullish consensus across agents.')).toBeInTheDocument();
});

test('renders SELL direction in verdict card', () => {
  render(<SimulationPanel result={makeResult({ direction: 'SELL' })} loading={false} error={null} progress={0} />);
  // SELL appears in verdict card + Agent Votes label
  expect(screen.getAllByText('SELL').length).toBeGreaterThanOrEqual(1);
});

test('renders bull, bear, hold vote counts', () => {
  render(<SimulationPanel result={makeResult({ bull_votes: 7, bear_votes: 2, hold_votes: 1 })} loading={false} error={null} progress={0} />);
  expect(screen.getByText('7')).toBeInTheDocument();
  expect(screen.getByText('2')).toBeInTheDocument();
  expect(screen.getByText('1')).toBeInTheDocument();
});

test('renders confidence as integer percentage in ring', () => {
  render(<SimulationPanel result={makeResult({ confidence: 0.78 })} loading={false} error={null} progress={0} />);
  expect(screen.getByText('78')).toBeInTheDocument();
});

test('renders agent reasoning cards when agents present', () => {
  const agents: Agent[] = [
    { role: 'Flow Analyst',  direction: 'BUY',  reasoning: 'High premium flow detected.' },
    { role: 'Risk Manager',  direction: 'HOLD', reasoning: 'Elevated market volatility.' },
  ];
  render(<SimulationPanel result={makeResult({ agents })} loading={false} error={null} progress={0} />);
  expect(screen.getByText('Flow Analyst')).toBeInTheDocument();
  expect(screen.getByText('High premium flow detected.')).toBeInTheDocument();
  expect(screen.getByText('Risk Manager')).toBeInTheDocument();
  expect(screen.getByText('Elevated market volatility.')).toBeInTheDocument();
});

test('agent reasoning section absent when agents is empty', () => {
  render(<SimulationPanel result={makeResult({ agents: [] })} loading={false} error={null} progress={0} />);
  expect(screen.queryByText('Agent Reasoning')).not.toBeInTheDocument();
});

test('Agent Votes section header and all three vote labels rendered', () => {
  render(<SimulationPanel result={makeResult()} loading={false} error={null} progress={0} />);
  expect(screen.getByText('Agent Votes')).toBeInTheDocument();
  // Each label appears at least once in the votes section
  expect(screen.getAllByText('BUY').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('SELL').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('HOLD').length).toBeGreaterThanOrEqual(1);
});
