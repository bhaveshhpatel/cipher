import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { SidebarNav } from '@/components/layout/SidebarNav';
import { DASHBOARD_TABS } from '@/types';
import type { DashboardTab } from '@/types';

const defaultProps = {
  activeTab:   'flow_events' as DashboardTab,
  onTabChange: jest.fn(),
  signalCount: 0,
  collapsed:   false,
  onToggle:    jest.fn(),
};

beforeEach(() => jest.clearAllMocks());

describe('SidebarNav', () => {
  it('renders all dashboard tabs', () => {
    render(<SidebarNav {...defaultProps} />);
    DASHBOARD_TABS.forEach(tab => {
      expect(screen.getByTestId(`sidebar-tab-${tab}`)).toBeInTheDocument();
    });
  });

  it('marks the active tab with aria-current=page', () => {
    render(<SidebarNav {...defaultProps} activeTab="signals" />);
    expect(screen.getByTestId('sidebar-tab-signals')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('sidebar-tab-flow_events')).not.toHaveAttribute('aria-current');
  });

  it('calls onTabChange with the clicked tab', () => {
    const onTabChange = jest.fn();
    render(<SidebarNav {...defaultProps} onTabChange={onTabChange} />);
    fireEvent.click(screen.getByTestId('sidebar-tab-composite'));
    expect(onTabChange).toHaveBeenCalledWith('composite');
  });

  it('shows signal badge when signalCount > 0 on signals tab', () => {
    render(<SidebarNav {...defaultProps} signalCount={5} />);
    expect(screen.getByTestId('signal-badge-signals')).toHaveTextContent('5');
  });

  it('caps signal badge display at 99+', () => {
    render(<SidebarNav {...defaultProps} signalCount={150} />);
    expect(screen.getByTestId('signal-badge-signals')).toHaveTextContent('99+');
  });

  it('does not render signal badge when signalCount is 0', () => {
    render(<SidebarNav {...defaultProps} signalCount={0} />);
    expect(screen.queryByTestId('signal-badge-signals')).not.toBeInTheDocument();
  });

  it('calls onToggle when the collapse button is clicked', () => {
    const onToggle = jest.fn();
    render(<SidebarNav {...defaultProps} onToggle={onToggle} />);
    fireEvent.click(screen.getByTestId('sidebar-toggle'));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('shows Collapse sidebar aria-label when expanded', () => {
    render(<SidebarNav {...defaultProps} collapsed={false} />);
    expect(screen.getByTestId('sidebar-toggle')).toHaveAttribute('aria-label', 'Collapse sidebar');
  });

  it('shows Expand sidebar aria-label when collapsed', () => {
    render(<SidebarNav {...defaultProps} collapsed={true} />);
    expect(screen.getByTestId('sidebar-toggle')).toHaveAttribute('aria-label', 'Expand sidebar');
  });

  it('hides tab labels when collapsed', () => {
    render(<SidebarNav {...defaultProps} collapsed={true} />);
    // Labels are rendered inside spans that are hidden when collapsed
    // The button text nodes for labels should not be visible as tab text
    // We verify the signal badge is hidden too (signalCount=0 so it wouldn\'t show anyway)
    expect(screen.queryByText('Flow Events')).not.toBeInTheDocument();
  });

  it('shows tab labels when expanded', () => {
    render(<SidebarNav {...defaultProps} collapsed={false} />);
    expect(screen.getByText('Flow Events')).toBeInTheDocument();
  });

  it('has a nav with accessible label', () => {
    render(<SidebarNav {...defaultProps} />);
    expect(screen.getByRole('navigation', { name: 'Dashboard navigation' })).toBeInTheDocument();
  });
});
