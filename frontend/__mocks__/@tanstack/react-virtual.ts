/**
 * Global manual mock for @tanstack/react-virtual.
 * Adjacent to node_modules — Jest auto-applies this for every test file.
 * Returns all items (count = virtual items) so existing row-data assertions pass unchanged.
 */
export const useVirtualizer = ({ count }: { count: number }) => ({
  getVirtualItems: () =>
    Array.from({ length: count }, (_, i) => ({
      index: i,
      key:   String(i),
      start: i * 45,
      size:  45,
    })),
  getTotalSize: () => count * 45,
});
