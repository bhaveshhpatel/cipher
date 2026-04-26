/** @type {import('jest').Config} */
const config = {
  // jsdom environment for React component + hook tests
  testEnvironment: 'jsdom',

  // TypeScript transform via ts-jest
  transform: {
    '^.+\\.(ts|tsx)$': ['ts-jest', {
      tsconfig: {
        jsx: 'react-jsx',
      },
    }],
  },

  // Module resolution — mirror Next.js path aliases defined in tsconfig.json
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
    // Static asset stubs
    '\\.(css|less|scss|sass)$':            '<rootDir>/__mocks__/styleMock.ts',
    '\\.(jpg|jpeg|png|gif|svg|ico|webp)$': '<rootDir>/__mocks__/fileMock.ts',
  },

  // Global test setup (e.g. jest-dom matchers)
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],

  // Pattern for test files
  testMatch: [
    '**/__tests__/**/*.(ts|tsx)',
    '**/?(*.)+(spec|test).(ts|tsx)',
  ],

  // ---------------------------------------------------------------------------
  // Coverage collection — src only, exclude non-logic files
  // ---------------------------------------------------------------------------
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/index.ts',           // barrel files — no logic
    '!src/app/layout.tsx',        // Next.js shell — no testable logic
    '!src/app/globals.css',
    '!src/types/**',
    '!src/**/__mocks__/**',
  ],

  // Coverage output formats — lcov for artifact upload, json-summary for CI
  coverageReporters: ['text', 'lcov', 'json-summary'],

  // ---------------------------------------------------------------------------
  // Coverage thresholds
  //
  // Global thresholds are intentionally LOW because large untested files
  // (admin/page.tsx, most dashboard components) pull overall coverage below 30%.
  // Per-file thresholds are kept tight for every file that HAS tests.
  // Raise global thresholds incrementally as component tests are added.
  // ---------------------------------------------------------------------------
  coverageThreshold: {
    global: {
      branches:   10,
      functions:  15,
      lines:      15,
      statements: 15,
    },

    // Auth hook — security critical, near-full coverage required
    './src/hooks/useAuth.ts': {
      branches:   65,
      functions:  65,
      lines:      80,
      statements: 80,
    },

    // Flow data hook — core data path
    './src/hooks/useFlow.ts': {
      branches:   75,
      functions:  90,
      lines:      90,
      statements: 90,
    },

    // API client — all request/response paths must be exercised
    './src/lib/api.ts': {
      branches:   80,
      functions:  80,
      lines:      80,
      statements: 80,
    },
  },

  // Ignore Next.js build output and node_modules
  testPathIgnorePatterns: [
    '<rootDir>/.next/',
    '<rootDir>/node_modules/',
  ],
}

module.exports = config
