import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('@/components/layout/AppHeader', () => ({
  AppHeader: ({ onLogout }: { onLogout: () => void }) => (
    <div data-testid="app-header">
      <button data-testid="mock-logout" onClick={onLogout}>logout</button>
    </div>
  ),
}));
jest.mock('@/components/layout/SidebarNav', () => ({
  SidebarNav: ({ activeTab, collapsed, onToggle }: { activeTab: string; collapsed: boolean; onToggle: () => void }) => (
    <div data-testid="sidebar-nav" data-active={activeTab} data-collapsed={String(collapsed)}>
      <button data-testid="mock-toggle" onClick={onToggle}>toggle</button>
    </div>
  ),
}));
jest.mock('@/components/layout/MobileTabBar', () => ({
  MobileTabBar: ({ activeTab }: { activeTab: string }) => (
    <div data-testid="mobile-tab-bar" data-active={activeTab} />
  ),
}));

import { DashboardLayout } from '@/components/layout/DashboardLayout';
import type { DashboardTab } from '@/types';

const defaultProps = {
  email:       'test@cipher.com',
  stats:       null,
  onLogout:    jest.fn(),
  activeTab:   'flow_events' as DashboardTab,
  onTabChange: jest.fn(),
  signalCount: 0,
};

beforeEach(() => jest.clearAllMocks());

describe('DashboardLayout', () => {
  it('renders all layout regions', () => {
    render(<DashboardLayout {...defaultProps}><div>content</div></DashboardLayout>);
    expect(screen.getByTestId('dashboard-layout')).toBeInTheDocument();
    expect(screen.getByTestId('app-header')).toBeInTheDocument();
    expect(screen.getByTestId('sidebar-nav')).toBeInTheDocument();
    expect(screen.getByTestId('mobile-tab-bar')).toBeInTheDocument();
    expect(screen.getByTestId('layout-main')).toBeInTheDocument();
  });

  it('renders children inside layout-main', () => {
    render(
      <DashboardLayout {...defaultProps}>
        <div data-testid="child-content">hello</div>
      </DashboardLayout>
    );
    expect(screen.getByTestId('layout-main')).toContainElement(screen.getByTestId('child-content'));
  });

  it('passes activeTab to SidebarNav', () => {
    render(<DashboardLayout {...defaultProps} activeTab="signals"><div /></DashboardLayout>);
    expect(screen.getByTestId('sidebar-nav')).toHaveAttribute('data-active', 'signals');
  });

  it('passes activeTab to MobileTabBar', () => {
    render(<DashboardLayout {...defaultProps} activeTab="composite"><div /></DashboardLayout>);
    expect(screen.getByTestId('mobile-tab-bar')).toHaveAttribute('data-active', 'composite');
  });

  it('sidebar starts expanded (collapsed=false)', () => {
    render(<DashboardLayout {...defaultProps}><div /></DashboardLayout>);
    expect(screen.getByTestId('sidebar-nav')).toHaveAttribute('data-collapsed', 'false');
  });

  it('toggles sidebar to collapsed on toggle click', () => {
    render(<DashboardLayout {...defaultProps}><div /></DashboardLayout>);
    fireEvent.click(screen.getByTestId('mock-toggle'));
    expect(screen.getByTestId('sidebar-nav')).toHaveAttribute('data-collapsed', 'true');
  });

  it('toggles sidebar back to expanded on second toggle click', () => {
    render(<DashboardLayout {...defaultProps}><div /></DashboardLayout>);
    fireEvent.click(screen.getByTestId('mock-toggle'));
    fireEvent.click(screen.getByTestId('mock-toggle'));
    expect(screen.getByTestId('sidebar-nav')).toHaveAttribute('data-collapsed', 'false');
  });

  it('calls onLogout when header triggers logout', () => {
    const onLogout = jest.fn();
    render(<DashboardLayout {...defaultProps} onLogout={onLogout}><div /></DashboardLayout>);
    fireEvent.click(screen.getByTestId('mock-logout'));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it('renders multiple children correctly', () => {
    render(
      <DashboardLayout {...defaultProps}>
        <div data-testid="child-a">A</div>
        <div data-testid="child-b">B</div>
      </DashboardLayout>
    );
    expect(screen.getByTestId('child-a')).toBeInTheDocument();
    expect(screen.getByTestId('child-b')).toBeInTheDocument();
  });
});
