import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const repository = process.env.GITHUB_REPOSITORY?.split('/')?.pop() ?? '';

export default defineConfig({
  root: resolve(__dirname, '.'),
  base: repository ? `/${repository}/` : '/',
  server: {
    port: 4173,
    host: '0.0.0.0'
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true
  }
});
