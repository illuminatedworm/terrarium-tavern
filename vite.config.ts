import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:51273', '/static': 'http://127.0.0.1:51273' } },
  build: {
    outDir: 'dist',
    rollupOptions: {
      onwarn(warning, defaultHandler) {
        // Zod 4.6.7 mentions @__PURE__ in prose; these are not optimization annotations.
        // Match only the two explanatory comments so genuine annotation warnings remain visible.
        if (
          warning.code === 'INVALID_ANNOTATION' &&
          /[/\\]node_modules[/\\]zod[/\\]v4[/\\]core[/\\](regexes|util)\.js$/.test(warning.id ?? '') &&
          (warning.message.includes('/** Anchors a pattern source.') ||
            warning.message.includes('// Wrapped in a `@__PURE__` IIFE:'))
        ) {
          return;
        }
        defaultHandler(warning);
      },
      output: {
        // Share Vue across the entry and lazy pages without growing the application chunk.
        manualChunks: { 'vue-vendor': ['vue'] },
      },
    },
  },
});
