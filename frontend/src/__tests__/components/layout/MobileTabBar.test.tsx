import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MobileTabBar } from '@/components/layout/MobileTabBar';
import { DASHBOARD_TABS } from '@/types';
import type { DashboardTab } from '@/types';

const defaultProps = {
  activeTab:   'flow_events' as DashboardTab,
  onTabChange: jest.fn(),
  signalCount: 0,
};

beforeEach(() => jest.clearAllMocks());

describe('MobileTabBar', () => {
  it('renders all dashboard tabs', () => {
    render(<MobileTabBar {...defaultProps} />);
    DASHBOARD_TABS.forEach(tab => {
      expect(screen.getByTestId(`mobile-tab-${tab}`)).toBeInTheDocument();
    });
  });

  it('marks the active tab with aria-current=page', () => {
    render(<MobileTabBar {...defaultProps} activeTab="history" />);
    expect(screen.getByTestId('mobile-tab-history')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('mobile-tab-flow_events')).not.toHaveAttribute('aria-current');
  });

  it('calls onTabChange with the clicked tab', () => {
    const onTabChange = jest.fn();
    render(<MobileTabBar {...defaultProps} onTabChange={onTabChange} />);
    fireEvent.click(screen.getByTestId('mobile-tab-simulation'));
    expect(onTabChange).toHaveBeenCalledWith('simulation');
  });

  it('shows signal badge when signalCount > 0', () => {
    render(<MobileTabBar {...defaultProps} signalCount={8} />);
    expect(screen.getByTestId('mobile-signal-badge')).toHaveTextContent('8');
  });

  it('caps signal badge at 99+', () => {
    render(<MobileTabBar {...defaultProps} signalCount={200} />);
    expect(screen.getByTestId('mobile-signal-badge')).toHaveTextContent('99+');
  });

  it('does not show signal badge when signalCount is 0', () => {
    render(<MobileTabBar {...defaultProps} signalCount={0} />);
    expect(screen.queryByTestId('mobile-signal-badge')).not.toBeInTheDocument();
  });

  it('has nav role with accessible label', () => {
    render(<MobileTabBar {...defaultProps} />);
    expect(screen.getByRole('navigation', { name: 'Dashboard navigation' })).toBeInTheDocument();
  });

  it('does not show signal badge on non-signals tab even with count > 0', () => {
    render(<MobileTabBar {...defaultProps} signalCount={10} activeTab="composite" />);
    // badge only appears on signals tab
    const signalsTab = screen.getByTestId('mobile-tab-signals');
    expect(signalsTab).toBeInTheDocument();
    // badge is present (signals tab has count=10)
    expect(screen.getByTestId('mobile-signal-badge')).toBeInTheDocument();
    // but composite tab itself doesn\'t have a badge
    const compositeTab = screen.getByTestId('mobile-tab-composite');
    expect(compositeTab).not.toHaveTextContent('10');
  });
});
