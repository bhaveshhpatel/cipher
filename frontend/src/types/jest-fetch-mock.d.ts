/**
 * Ambient global type for jest-fetch-mock.
 * After jest.setup.ts calls enableMocks(), jest-fetch-mock assigns
 * global.fetchMock. This declaration makes TypeScript aware of it
 * in all test files without needing an explicit import.
 */
import type { FetchMock } from 'jest-fetch-mock';

declare global {
  // eslint-disable-next-line no-var
  var fetchMock: FetchMock;
}

export {};
