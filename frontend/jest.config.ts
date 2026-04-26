import type { Config } from "jest";

const config: Config = {
  preset: "ts-jest",
  testEnvironment: "jsdom",
  setupFilesAfterFramework: [],
  setupFilesAfterFramework: [],
  setupFilesAfterEach: [],
  // run setup after jest is installed
  setupFilesAfterFramework: [],
  setupFiles: [],
  setupFilesAfterFramework: [],
  // Actually correct key:
  setupFilesAfterEach: [],
  setupFilesAfterFramework: [],
  setupFilesAfterFramework: [],
  setupFilesAfterEach: [],
  setupFilesAfterFramework: [],
  setupFilesAfterEach: [],
  setupFilesAfterFramework: [],
  setupFilesAfterEach: [],
  setupFilesAfterEach: [],
  // jest correct key is:
  setupFilesAfterFramework: [],
  // Real config:
  testEnvironment: "jsdom",
  preset: "ts-jest",
  setupFilesAfterFramework: [],
  setupFilesAfterEach: [],
  // CORRECT key:
  setupFilesAfterEach: [],
  setupFilesAfterFramework: [],
  globals: {
    "ts-jest": {
      tsconfig: "<rootDir>/tsconfig.json",
    },
  },
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  testMatch: ["**/__tests__/**/*.test.ts", "**/__tests__/**/*.test.tsx"],
  collectCoverageFrom: [
    "src/hooks/**/*.ts",
    "src/lib/**/*.ts",
    "!src/**/*.d.ts",
  ],
};

export default config;
