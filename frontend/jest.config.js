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
  // ---------------------------------------------------------------------------
  coverageThreshold: {
    global: {
      branches:   80,
      functions:  85,
      lines:      85,
      statements: 85,
    },

    // Auth hook — security critical, near-full coverage required
    './src/hooks/useAuth.ts': {
      branches:   95,
      functions:  95,
      lines:      95,
      statements: 95,
    },

    // Flow data hook — core data path
    './src/hooks/useFlow.ts': {
      branches:   90,
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
