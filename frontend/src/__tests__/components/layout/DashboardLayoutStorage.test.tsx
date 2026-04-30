/**
 * P6 — DashboardLayout localStorage persistence tests.
 *
 * Covers:
 *   - Sidebar starts collapsed when localStorage has cipher:sidebar-collapsed=true
 *   - Sidebar starts expanded when localStorage is empty
 *   - Toggle writes cipher:sidebar-collapsed=true to localStorage
 *   - Double-toggle writes cipher:sidebar-collapsed=false to localStorage
 *
 * Strategy: mock SidebarNav to capture the `collapsed` prop directly
 * without depending on aria-label strings.
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { DashboardLayout } from '../../../components/layout/DashboardLayout';

// ── mocks ─────────────────────────────────────────────────────────────────────

jest.mock('../../../components/layout/AppHeader', () => ({
  AppHeader: () => <header data-testid="app-header" />,
}));

jest.mock('../../../components/layout/MobileTabBar', () => ({
  MobileTabBar: () => <nav data-testid="mobile-tab-bar" />,
}));

jest.mock('../../../components/layout/SidebarNav', () => ({
  SidebarNav: ({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) => (
    <button
      data-testid="sidebar-toggle"
      data-collapsed={String(collapsed)}
      onClick={onToggle}
    >
      {collapsed ? 'Expand' : 'Collapse'}
    </button>
  ),
}));

// ── helpers ───────────────────────────────────────────────────────────────────

const baseProps = {
  email:       'admin@cipher.io',
  stats:       null,
  onLogout:    jest.fn(),
  activeTab:   'flow_events' as const,
  onTabChange: jest.fn(),
  signalCount: 0,
  children:    <div data-testid="content" />,
};

beforeEach(() => {
  localStorage.clear();
  jest.clearAllMocks();
});

// ── tests ─────────────────────────────────────────────────────────────────────

test('sidebar starts expanded when localStorage is empty', () => {
  render(<DashboardLayout {...baseProps} />);
  expect(screen.getByTestId('sidebar-toggle')).toHaveAttribute('data-collapsed', 'false');
});

test('sidebar starts collapsed when localStorage has cipher:sidebar-collapsed=true', () => {
  localStorage.setItem('cipher:sidebar-collapsed', 'true');
  render(<DashboardLayout {...baseProps} />);
  expect(screen.getByTestId('sidebar-toggle')).toHaveAttribute('data-collapsed', 'true');
});

test('toggle writes cipher:sidebar-collapsed=true to localStorage', () => {
  render(<DashboardLayout {...baseProps} />);
  fireEvent.click(screen.getByTestId('sidebar-toggle'));
  expect(localStorage.getItem('cipher:sidebar-collapsed')).toBe('true');
});

test('double-toggle writes cipher:sidebar-collapsed=false to localStorage', () => {
  render(<DashboardLayout {...baseProps} />);
  fireEvent.click(screen.getByTestId('sidebar-toggle')); // collapse
  fireEvent.click(screen.getByTestId('sidebar-toggle')); // expand
  expect(localStorage.getItem('cipher:sidebar-collapsed')).toBe('false');
});
