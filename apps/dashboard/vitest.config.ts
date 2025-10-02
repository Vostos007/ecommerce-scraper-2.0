import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['**/*.{test,spec}.{ts,tsx}'],
    exclude: ['node_modules', '.next', 'out', 'dist', 'e2e/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80
      },
      exclude: ['vitest.setup.ts', 'vitest.config.ts']
    },
    pool: 'threads'
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
      '@/app': path.resolve(__dirname, 'app'),
      '@/components': path.resolve(__dirname, 'components'),
      '@/hooks': path.resolve(__dirname, 'hooks'),
      '@/lib': path.resolve(__dirname, 'lib'),
      '@/stores': path.resolve(__dirname, 'stores')
    }
  },
  define: {
    // Define Node.js globals for browser environment
    global: 'globalThis',
  },
  optimizeDeps: {
    include: ['react', 'react-dom']
  }
});
