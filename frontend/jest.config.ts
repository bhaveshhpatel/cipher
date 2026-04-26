import type { Config } from 'jest'

const config: Config = {
  // Use jsdom for React component tests
  testEnvironment: 'jsdom',

  // TypeScript transform via ts-jest
  transform: {
    '^.+\\.(ts|tsx)$': ['ts-jest', {
      tsconfig: {
        jsx: 'react-jsx',
      },
    }],
  },

  // Module resolution — mirror Next.js path aliases
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
    // Static asset stubs
    '\\.(css|less|scss|sass)$': '<rootDir>/__mocks__/styleMock.ts',
    '\\.(jpg|jpeg|png|gif|svg|ico|webp)$': '<rootDir>/__mocks__/fileMock.ts',
  },

  // Setup files run after jest is initialised
  setupFilesAfterFramework: [],
  setupFilesAfterFramework: [],

  // Pattern for test files
  testMatch: [
    '**/__tests__/**/*.(ts|tsx)',
    '**/?(*.)+(spec|test).(ts|tsx)',
  ],

  // Coverage collection
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/index.ts',          // barrel files — no logic
    '!src/app/layout.tsx',       // Next.js shell — no testable logic
    '!src/app/globals.css',
    '!src/types/**',
    '!src/**/__mocks__/**',
  ],

  // ---------------------------------------------------------------------------
  // Phase 5: Coverage thresholds — CI will fail if any metric drops below these
  // ---------------------------------------------------------------------------
  coverageThreshold: {
    global: {
      branches:   70,
      functions:  75,
      lines:      75,
      statements: 75,
    },
    // Critical auth hook — must stay at near-100%
    './src/hooks/useAuth.ts': {
      branches:   90,
      functions:  90,
      lines:      90,
      statements: 90,
    },
    // Flow data hook
    './src/hooks/useFlow.ts': {
      branches:   85,
      functions:  85,
      lines:      85,
      statements: 85,
    },
  },

  // Ignore Next.js build output
  testPathIgnorePatterns: [
    '<rootDir>/.next/',
    '<rootDir>/node_modules/',
  ],
}

export default config
