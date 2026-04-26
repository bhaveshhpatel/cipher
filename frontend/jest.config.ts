import type { Config } from "jest";

const config: Config = {
  preset: "ts-jest",
  testEnvironment: "jsdom",
  setupFilesAfterFramework: undefined,
  // Load @testing-library/jest-dom matchers after the test framework
  setupFilesAfterEach: undefined,
  globals: {
    "ts-jest": {
      tsconfig: "<rootDir>/tsconfig.json",
    },
  },
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  testMatch: [
    "**/__tests__/**/*.test.ts",
    "**/__tests__/**/*.test.tsx",
  ],
  collectCoverageFrom: [
    "src/hooks/**/*.ts",
    "src/lib/**/*.ts",
    "!src/**/*.d.ts",
  ],
};

export default config;
