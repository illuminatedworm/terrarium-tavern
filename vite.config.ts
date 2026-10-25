import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:51273', '/static': 'http://127.0.0.1:51273' } },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        // Share Vue across the entry and lazy pages without growing the application chunk.
        manualChunks: { 'vue-vendor': ['vue'] },
      },
    },
  },
});
