import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    exclude: ['tests/**', 'node_modules/**'],
    // Answers `fetch("/config")` locally. Without it, the auth chain rejects on
    // a relative URL in Node and vitest exits non-zero on the unhandled
    // rejection even when every test passes -- which is how CI failed on a
    // suite that was green.
    setupFiles: ['./vitest.setup.ts'],
  },
});
