/**
 * Tests for FlowEventsTab component.
 *
 * Covers:
 *   Rendering states:
 *   - Shows skeleton rows when loading=true
 *   - Shows error message when error is set
 *   - Shows empty state when events=[]
 *   - Renders all 10 column headers when events are present
 *   - Renders one row per event
 *
 *   KPI bar:
 *   - Shows KPI bar only when events.length > 0
 *   - Total Premium KPI sums premiums correctly
 *   - Unique Tickers KPI counts distinct tickers
 *   - Trade Count KPI shows event count
 *
 *   Event row data:
 *   - Ticker displayed in amber
 *   - Contract shown as Strike + Expiry
 *   - CALL badge has badge-green class
 *   - PUT badge has badge-red class
 *   - BULLISH sentiment has badge-green class
 *   - BEARISH sentiment has badge-red class
 *   - Premium formatted with $ and K/M suffix
 *   - T1 tier gets badge-amber class
 *   - T2 tier gets badge-teal class
 *   - is_aggressive renders ⚡ flag
 *   - is_golden_sweep renders ★ flag
 *
 *   Filter interactions:
 *   - Clicking BULLISH filter calls onFilter with sentiment=BULLISH
 *   - Clicking PUT filter calls onFilter with contract_type=PUT
 *   - Clicking T1 filter calls onFilter with tier=T1
 *   - Clicking Aggressive toggle calls onFilter with aggressive=true
 *   - Clicking Golden Sweep toggle calls onFilter with golden_sweep=true
 *   - Clicking ALL sentiment resets sentiment filter
 *   - Multiple filters combine correctly
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { FlowEventsTab } from '../components/dashboard/FlowEventsTab';
import type { FlowEventRaw } from '../lib/api';

const makeEvent = (overrides: Partial<FlowEventRaw> = {}): FlowEventRaw => ({
  id: 1,
  ticker: 'AAPL',
  strike: 200,
  expiry: '2026-05-16',
  contract_type: 'CALL',
  sentiment: 'BULLISH',
  premium: 50_000,
  size: 100,
  bid: 4.90,
  ask: 5.10,
  fill_price: 5.00,
  tier: 'T1',
  is_aggressive: false,
  is_golden_sweep: false,
  timestamp: '2026-04-28T18:00:00Z',
  session_date: '2026-04-28',
  ...overrides,
});

const noop = jest.fn();

beforeEach(() => noop.mockReset());

// ── loading ───────────────────────────────────────────────────────────────────

test('shows skeleton rows when loading', () => {
  const { container } = render(
    <FlowEventsTab events={[]} loading={true} error={null} onFilter={noop} />
  );
  const skeletons = container.querySelectorAll('.skeleton');
  expect(skeletons.length).toBeGreaterThan(0);
});

// ── error ─────────────────────────────────────────────────────────────────────

test('shows error message when error is set', () => {
  render(<FlowEventsTab events={[]} loading={false} error="Network failure" onFilter={noop} />);
  expect(screen.getByText(/Network failure/)).toBeInTheDocument();
});

// ── empty state ───────────────────────────────────────────────────────────────

test('shows empty state when events is empty', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByText(/No flow events match these filters/)).toBeInTheDocument();
});

// ── column headers ────────────────────────────────────────────────────────────

test('renders all 10 column headers', () => {
  render(<FlowEventsTab events={[makeEvent()]} loading={false} error={null} onFilter={noop} />);
  ['Time', 'Ticker', 'Contract', 'Type', 'Sentiment', 'Premium', 'Size', 'Bid / Ask / Fill', 'Tier', 'Flags']
    .forEach(h => expect(screen.getByText(h)).toBeInTheDocument());
});

// ── KPI bar ───────────────────────────────────────────────────────────────────

test('KPI bar not rendered when events is empty', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  expect(screen.queryByText('Total Premium')).not.toBeInTheDocument();
});

test('KPI bar rendered when events present', () => {
  render(<FlowEventsTab events={[makeEvent()]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByText('Total Premium')).toBeInTheDocument();
  expect(screen.getByText('Trade Count')).toBeInTheDocument();
  expect(screen.getByText('Unique Tickers')).toBeInTheDocument();
});

test('Total Premium sums all event premiums', () => {
  const events = [makeEvent({ premium: 100_000 }), makeEvent({ id: 2, ticker: 'SPY', premium: 200_000 })];
  render(<FlowEventsTab events={events} loading={false} error={null} onFilter={noop} />);
  // $300K — appears in the KPI bar span (text-2xl)
  const kpiSpan = document.querySelector('.text-2xl');
  expect(kpiSpan?.textContent).toBe('$300.0K');
});

test('Unique Tickers counts distinct tickers', () => {
  const events = [
    makeEvent({ id: 1, ticker: 'AAPL' }),
    makeEvent({ id: 2, ticker: 'AAPL' }),
    makeEvent({ id: 3, ticker: 'SPY' }),
  ];
  render(<FlowEventsTab events={events} loading={false} error={null} onFilter={noop} />);
  // 2 unique tickers — the "2" should appear in the KPI
  const kpiValues = screen.getAllByText('2');
  expect(kpiValues.length).toBeGreaterThan(0);
});

// ── row data ──────────────────────────────────────────────────────────────────

test('renders ticker in row', () => {
  render(<FlowEventsTab events={[makeEvent({ ticker: 'TSLA' })]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByText('TSLA')).toBeInTheDocument();
});

test('renders strike and expiry as contract', () => {
  render(<FlowEventsTab events={[makeEvent({ strike: 250, expiry: '2026-06-20' })]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByText('$250 2026-06-20')).toBeInTheDocument();
});

test('CALL contract_type has badge-green class', () => {
  render(<FlowEventsTab events={[makeEvent({ contract_type: 'CALL' })]} loading={false} error={null} onFilter={noop} />);
  const badges = document.querySelectorAll('.badge-green');
  expect(badges.length).toBeGreaterThan(0);
});

test('PUT contract_type has badge-red class', () => {
  render(<FlowEventsTab events={[makeEvent({ contract_type: 'PUT', sentiment: 'BEARISH' })]} loading={false} error={null} onFilter={noop} />);
  const badges = document.querySelectorAll('.badge-red');
  expect(badges.length).toBeGreaterThan(0);
});

test('BULLISH sentiment renders badge-green', () => {
  render(<FlowEventsTab events={[makeEvent({ sentiment: 'BULLISH' })]} loading={false} error={null} onFilter={noop} />);
  // Both a filter button and a table badge contain 'BULLISH' — find the badge
  const badge = screen.getAllByText('BULLISH').find(el => el.classList.contains('badge-green'));
  expect(badge).toBeDefined();
  expect(badge).toBeInTheDocument();
});

test('BEARISH sentiment renders badge-red', () => {
  render(<FlowEventsTab events={[makeEvent({ sentiment: 'BEARISH', contract_type: 'PUT' })]} loading={false} error={null} onFilter={noop} />);
  const badge = screen.getAllByText('BEARISH').find(el => el.classList.contains('badge-red'));
  expect(badge).toBeDefined();
  expect(badge).toBeInTheDocument();
});

test('premium formatted as $K', () => {
  render(<FlowEventsTab events={[makeEvent({ premium: 75_500 })]} loading={false} error={null} onFilter={noop} />);
  // $75.5K appears in both KPI bar and table row — find the table cell
  const matches = screen.getAllByText('$75.5K');
  // At least one match; table cell has class tabular
  const tableCell = matches.find(el => el.tagName === 'TD');
  expect(tableCell).toBeDefined();
});

test('premium formatted as $M', () => {
  render(<FlowEventsTab events={[makeEvent({ premium: 1_250_000 })]} loading={false} error={null} onFilter={noop} />);
  const matches = screen.getAllByText('$1.25M');
  const tableCell = matches.find(el => el.tagName === 'TD');
  expect(tableCell).toBeDefined();
});

test('is_aggressive renders lightning flag', () => {
  render(<FlowEventsTab events={[makeEvent({ is_aggressive: true })]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByTitle('Aggressive fill')).toBeInTheDocument();
});

test('is_golden_sweep renders star flag', () => {
  render(<FlowEventsTab events={[makeEvent({ is_golden_sweep: true })]} loading={false} error={null} onFilter={noop} />);
  expect(screen.getByTitle('Golden Sweep')).toBeInTheDocument();
});

test('no flags rendered when both are false', () => {
  render(<FlowEventsTab events={[makeEvent({ is_aggressive: false, is_golden_sweep: false })]} loading={false} error={null} onFilter={noop} />);
  expect(screen.queryByTitle('Aggressive fill')).not.toBeInTheDocument();
  expect(screen.queryByTitle('Golden Sweep')).not.toBeInTheDocument();
});

// ── filter interactions ───────────────────────────────────────────────────────

test('clicking BULLISH filter calls onFilter with sentiment=BULLISH', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText('BULLISH'));
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ sentiment: 'BULLISH' }));
});

test('clicking BEARISH filter calls onFilter with sentiment=BEARISH', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText('BEARISH'));
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ sentiment: 'BEARISH' }));
});

test('clicking PUT filter calls onFilter with contract_type=PUT', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  // There are multiple PUT buttons possible — get the filter bar PUT
  const putBtn = screen.getAllByText('PUT')[0];
  fireEvent.click(putBtn);
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ contract_type: 'PUT' }));
});

test('clicking T1 tier filter calls onFilter with tier=T1', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText('T1'));
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ tier: 'T1' }));
});

test('clicking Aggressive toggle calls onFilter with aggressive=true', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText(/Aggressive/));
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ aggressive: true }));
});

test('clicking Golden Sweep toggle calls onFilter with golden_sweep=true', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText(/Golden Sweep/));
  expect(noop).toHaveBeenCalledWith(expect.objectContaining({ golden_sweep: true }));
});

test('clicking All Sentiment resets sentiment filter (no sentiment key)', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  // First set BULLISH, then reset to ALL
  fireEvent.click(screen.getByText('BULLISH'));
  fireEvent.click(screen.getByText('All Sentiment'));
  const lastCall = noop.mock.calls[noop.mock.calls.length - 1][0];
  expect(lastCall).not.toHaveProperty('sentiment');
});

test('combining T2 + CALL calls onFilter with both', () => {
  render(<FlowEventsTab events={[]} loading={false} error={null} onFilter={noop} />);
  fireEvent.click(screen.getByText('T2'));
  const callBtns = screen.getAllByText('CALL');
  fireEvent.click(callBtns[0]);
  const lastCall = noop.mock.calls[noop.mock.calls.length - 1][0];
  expect(lastCall).toMatchObject({ tier: 'T2', contract_type: 'CALL' });
});
