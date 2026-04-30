import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('@/components/CipherLogo', () => ({
  CipherLogo: () => <svg data-testid="cipher-logo" />,
}));
jest.mock('@/components/ThemeToggle', () => ({
  ThemeToggle: () => <button data-testid="theme-toggle" />,
}));
jest.mock('@/components/dashboard/StreamStatsBar', () => ({
  StreamStatsBar: ({ stats }: { stats: unknown }) => (
    <div data-testid="stream-stats">{JSON.stringify(stats)}</div>
  ),
}));
jest.mock('@/hooks', () => ({
  useMarketStatus: jest.fn(),
}));
jest.mock('@/components/ui', () => ({
  MarketStatusChip: ({ status }: { status: string }) => (
    <span data-testid="market-status-chip">{status}</span>
  ),
}));

import { useMarketStatus } from '@/hooks';
import { AppHeader } from '@/components/layout/AppHeader';

const mockUseMarketStatus = useMarketStatus as jest.Mock;

const baseStatus = {
  status: 'open' as const,
  isLoading: false,
  isOpen: true,
  nextChange: null,
  session: 'regular',
  error: null,
  refresh: jest.fn(),
};

beforeEach(() => {
  mockUseMarketStatus.mockReturnValue(baseStatus);
});

describe('AppHeader', () => {
  it('renders brand name and logo', () => {
    render(<AppHeader email="test@cipher.com" stats={null} onLogout={jest.fn()} />);
    expect(screen.getByTestId('cipher-logo')).toBeInTheDocument();
    expect(screen.getByText('CIPHER')).toBeInTheDocument();
  });

  it('renders email when provided', () => {
    render(<AppHeader email="dhruv@cipher.com" stats={null} onLogout={jest.fn()} />);
    expect(screen.getByText('dhruv@cipher.com')).toBeInTheDocument();
  });

  it('does not crash when email is null', () => {
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.getByTestId('app-header')).toBeInTheDocument();
  });

  it('renders MarketStatusChip when status is available and not loading', () => {
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.getByTestId('market-status-chip')).toHaveTextContent('open');
  });

  it('does not render MarketStatusChip while status is loading', () => {
    mockUseMarketStatus.mockReturnValue({ ...baseStatus, status: null, isLoading: true });
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.queryByTestId('market-status-chip')).not.toBeInTheDocument();
  });

  it('does not render MarketStatusChip when status is null after load', () => {
    mockUseMarketStatus.mockReturnValue({ ...baseStatus, status: null, isLoading: false });
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.queryByTestId('market-status-chip')).not.toBeInTheDocument();
  });

  it('renders StreamStatsBar when stats are provided', () => {
    const stats = { active_symbols: 5, ticks: 100, classified: 50, signals: 3, errors: 0 };
    render(<AppHeader email={null} stats={stats} onLogout={jest.fn()} />);
    expect(screen.getByTestId('stream-stats')).toBeInTheDocument();
  });

  it('does not render StreamStatsBar when stats is null', () => {
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.queryByTestId('stream-stats')).not.toBeInTheDocument();
  });

  it('calls onLogout when Sign out button is clicked', () => {
    const onLogout = jest.fn();
    render(<AppHeader email={null} stats={null} onLogout={onLogout} />);
    fireEvent.click(screen.getByTestId('logout-btn'));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it('renders ThemeToggle', () => {
    render(<AppHeader email={null} stats={null} onLogout={jest.fn()} />);
    expect(screen.getByTestId('theme-toggle')).toBeInTheDocument();
  });
});
