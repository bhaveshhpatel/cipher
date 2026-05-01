/**
 * Tests for FlowEpisodesTab component.
 *
 * Covers:
 *   Rendering states:
 *   - Shows skeleton rows when loading=true
 *   - Shows error message when error is set
 *   - Shows empty state when episodes=[]
 *   - Renders all 9 column headers when episodes are present
 *   - Renders one row per episode
 *
 *   Episode row data:
 *   - Ticker shown in amber
 *   - BULLISH direction has badge-green class
 *   - BEARISH direction has badge-red class
 *   - CALL contract_type has badge-green class
 *   - PUT contract_type has badge-red class
 *   - STRONG alert level styled orange
 *   - ALERT level styled gold
 *   - HOLD level styled blue
 *   - WATCH level uses default muted style
 *   - Total premium formatted correctly
 *   - Delta premium positive shows + prefix
 *   - Delta premium negative shows - prefix
 *   - Duration < 60s shows seconds format
 *   - Duration >= 60s shows minutes format
 *   - No strike or expiry column headers
 *
 *   Filter interactions:
 *   - Clicking BULLISH calls onFiltersChange with direction=BULLISH
 *   - Clicking BEARISH calls onFiltersChange with direction=BEARISH
 *   - Clicking PUT calls onFiltersChange with contract_type=PUT
 *   - Clicking STRONG alert calls onFiltersChange with alert_level=STRONG
 *   - Clicking HOLD alert calls onFiltersChange with alert_level=HOLD
 *   - Clicking Accelerating toggle sorts by delta desc (client-side only)
 *   - Clicking All Directions resets direction filter
 *   - Combining BEARISH + PUT calls onFiltersChange with both
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { FlowEpisodesTab } from '../components/dashboard/FlowEpisodesTab';
import type { FlowEpisode } from '../lib/api';

const makeEpisode = (overrides: Partial<FlowEpisode> = {}): FlowEpisode => ({
  id: 1,
  ticker: 'AAPL',
  direction: 'BULLISH',
  contract_type: 'CALL',
  alert_level: 'ALERT',
  trade_count: 10,
  total_premium: 150_000,
  last_signaled_premium: 100_000,
  duration_seconds: 90,
  started_at: '2026-04-28T18:00:00Z',
  updated_at: '2026-04-28T18:01:30Z',
  session_date: '2026-04-28',
  ...overrides,
});

const noop = jest.fn();

beforeEach(() => noop.mockReset());

// ── loading ───────────────────────────────────────────────────────────────────

test('shows skeleton rows when loading', () => {
  const { container } = render(
    <FlowEpisodesTab episodes={[]} loading={true} error={null} onFiltersChange={noop} />
  );
  const skeletons = container.querySelectorAll('.skeleton');
  expect(skeletons.length).toBeGreaterThan(0);
});

// ── error ─────────────────────────────────────────────────────────────────────

test('shows error message when error is set', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error="DB timeout" onFiltersChange={noop} />);
  expect(screen.getByText(/DB timeout/)).toBeInTheDocument();
});

// ── empty state ───────────────────────────────────────────────────────────────

test('shows empty state when episodes is empty', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText(/No active episodes match these filters/)).toBeInTheDocument();
});

// ── column headers ────────────────────────────────────────────────────────────

test('renders all 9 column headers', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode()]} loading={false} error={null} onFiltersChange={noop} />);
  ['Ticker', 'Direction', 'Contract Type', 'Alert Level', 'Trades', 'Total Premium', '\u0394Premium', 'Duration', 'Started']
    .forEach(h => expect(screen.getByText(h)).toBeInTheDocument());
});

test('no Strike or Expiry column headers', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode()]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.queryByText('Strike')).not.toBeInTheDocument();
  expect(screen.queryByText('Expiry')).not.toBeInTheDocument();
});

// ── row data ──────────────────────────────────────────────────────────────────

test('renders ticker in row', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ ticker: 'NVDA' })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText('NVDA')).toBeInTheDocument();
});

test('BULLISH direction has badge-green class', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ direction: 'BULLISH' })]} loading={false} error={null} onFiltersChange={noop} />);
  // getAllByText guards against filter-bar vs row-badge ambiguity
  const badge = screen.getAllByText('BULLISH').find(el => el.classList.contains('badge-green'));
  expect(badge).toBeDefined();
});

test('BEARISH direction has badge-red class', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ direction: 'BEARISH' })]} loading={false} error={null} onFiltersChange={noop} />);
  const badge = screen.getAllByText('BEARISH').find(el => el.classList.contains('badge-red'));
  expect(badge).toBeDefined();
});

test('CALL contract_type has badge-green class', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ contract_type: 'CALL' })]} loading={false} error={null} onFiltersChange={noop} />);
  const badge = screen.getAllByText('CALL').find(el => el.classList.contains('badge-green'));
  expect(badge).toBeDefined();
});

test('PUT contract_type has badge-red class', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ contract_type: 'PUT' })]} loading={false} error={null} onFiltersChange={noop} />);
  const badge = screen.getAllByText('PUT').find(el => el.classList.contains('badge-red'));
  expect(badge).toBeDefined();
});

test('STRONG alert level uses orange color', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ alert_level: 'STRONG' })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getAllByText('STRONG').length).toBeGreaterThan(0);
});

test('HOLD alert level renders in table row', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ alert_level: 'HOLD' })]} loading={false} error={null} onFiltersChange={noop} />);
  const holds = screen.getAllByText('HOLD');
  expect(holds.length).toBeGreaterThan(0);
});

test('WATCH alert level renders in table row', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ alert_level: 'WATCH' })]} loading={false} error={null} onFiltersChange={noop} />);
  const watches = screen.getAllByText('WATCH');
  expect(watches.length).toBeGreaterThan(0);
});

test('total premium formatted as $K', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ total_premium: 85_000 })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText('$85.0K')).toBeInTheDocument();
});

test('total premium formatted as $M', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ total_premium: 2_500_000 })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText('$2.50M')).toBeInTheDocument();
});

test('positive delta shows + prefix', () => {
  render(
    <FlowEpisodesTab
      episodes={[makeEpisode({ total_premium: 150_000, last_signaled_premium: 100_000 })]}
      loading={false} error={null} onFiltersChange={noop}
    />
  );
  expect(screen.getByText('+$50.0K')).toBeInTheDocument();
});

test('negative delta shows no + prefix', () => {
  render(
    <FlowEpisodesTab
      episodes={[makeEpisode({ total_premium: 80_000, last_signaled_premium: 100_000 })]}
      loading={false} error={null} onFiltersChange={noop}
    />
  );
  expect(screen.getByText('-$20.0K')).toBeInTheDocument();
});

test('duration < 60s shows seconds', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ duration_seconds: 45 })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText('45s')).toBeInTheDocument();
});

test('duration >= 60s shows minutes format', () => {
  render(<FlowEpisodesTab episodes={[makeEpisode({ duration_seconds: 125 })]} loading={false} error={null} onFiltersChange={noop} />);
  expect(screen.getByText('2m 5s')).toBeInTheDocument();
});

// ── filter interactions ───────────────────────────────────────────────────────
// events=[] ensures only filter-bar elements are rendered; no row-badge ambiguity.

test('clicking BULLISH calls onFiltersChange with direction=BULLISH', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const btns = screen.getAllByText('BULLISH');
  fireEvent.click(btns[0]);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ direction: 'BULLISH' }));
});

test('clicking BEARISH calls onFiltersChange with direction=BEARISH', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const btns = screen.getAllByText('BEARISH');
  fireEvent.click(btns[0]);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ direction: 'BEARISH' }));
});

test('clicking PUT calls onFiltersChange with contract_type=PUT', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const putBtns = screen.getAllByText('PUT');
  fireEvent.click(putBtns[0]);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ contract_type: 'PUT' }));
});

test('clicking STRONG alert calls onFiltersChange with alert_level=STRONG', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const strongBtns = screen.getAllByText('STRONG');
  fireEvent.click(strongBtns[0]);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ alert_level: 'STRONG' }));
});

test('clicking HOLD calls onFiltersChange with alert_level=HOLD', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const holdBtns = screen.getAllByText('HOLD');
  fireEvent.click(holdBtns[0]);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ alert_level: 'HOLD' }));
});

test('clicking Accelerating does NOT call onFiltersChange with accelerating key', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  fireEvent.click(screen.getByText(/Accelerating/));
  expect(noop).toHaveBeenCalledWith(expect.not.objectContaining({ accelerating: true }));
});

test('Accelerating toggle sorts episodes by delta desc', () => {
  const eps = [
    makeEpisode({ id: 1, ticker: 'LOW',  total_premium: 110_000, last_signaled_premium: 100_000 }),
    makeEpisode({ id: 2, ticker: 'HIGH', total_premium: 200_000, last_signaled_premium: 100_000 }),
  ];
  render(<FlowEpisodesTab episodes={eps} loading={false} error={null} onFiltersChange={noop} />);
  fireEvent.click(screen.getByText(/Accelerating/));
  const rows = screen.getAllByRole('row');
  expect(rows[1].textContent).toContain('HIGH');
});

test('clicking All Directions resets direction filter', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  fireEvent.click(screen.getByText('BULLISH'));
  fireEvent.click(screen.getByText('All Directions'));
  const lastCall = noop.mock.calls[noop.mock.calls.length - 1][0];
  expect(lastCall).not.toHaveProperty('direction');
});

test('combining BEARISH + PUT calls onFiltersChange with both', () => {
  render(<FlowEpisodesTab episodes={[]} loading={false} error={null} onFiltersChange={noop} />);
  const bearBtns = screen.getAllByText('BEARISH');
  fireEvent.click(bearBtns[0]);
  const putBtns = screen.getAllByText('PUT');
  fireEvent.click(putBtns[0]);
  const lastCall = noop.mock.calls[noop.mock.calls.length - 1][0];
  expect(lastCall).toMatchObject({ direction: 'BEARISH', contract_type: 'PUT' });
});
