<script setup lang="ts">
// One stroke icon family (24px grid, round joins) drawn for this project, so every
// control shares weight and proportion instead of mixing Unicode glyphs.
const icons: Record<string, string> = {
  brand:
    '<path d="M6 19.5V11a6 6 0 0 1 12 0v8.5"/><path d="M4 19.5h16"/><path d="M12 5V3.5"/><path d="M12 19.5v-5.2"/><path d="M12 14.3c0-2.2 1.6-3.8 3.8-3.8 0 2.2-1.6 3.8-3.8 3.8Z"/><path d="M12 15.6c0-1.7-1.3-3-3-3 0 1.7 1.3 3 3 3Z"/>',
  chat: '<path d="M5 4.5h14a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-7.2L7 20.5V17H5a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2Z"/><path d="M7.5 9.5h9M7.5 13h5.5"/>',
  cards:
    '<rect x="7.5" y="3" width="12.5" height="16" rx="2.2"/><path d="M4 7.5v11.3A2.2 2.2 0 0 0 6.2 21h9.3"/><circle cx="13.75" cy="9" r="2.2"/><path d="M10.4 15.3c.7-1.6 1.9-2.4 3.35-2.4s2.65.8 3.35 2.4"/>',
  quill:
    '<path d="M20 4C13 4 8.3 8.4 6.8 14.6L5.5 19.5l4.9-1.3C16.6 16.7 20 11.5 20 4Z"/><path d="M5.5 19.5 13 12"/><path d="M9.3 11.6h4.2M12 8.5h4.6"/>',
  user: '<circle cx="12" cy="8.2" r="3.7"/><path d="M4.8 20.2c1.2-3.7 4-5.7 7.2-5.7s6 2 7.2 5.7"/>',
  type: '<path d="M3.5 18 8 6h1l4.5 12M5 14h7"/><path d="M15.6 11.7c.6-.6 1.4-.9 2.4-.9 1.7 0 2.7.9 2.7 2.6V18"/><path d="M20.7 15c-.7-.3-1.4-.4-2.2-.4-1.6 0-2.6.7-2.6 1.7s.8 1.7 2 1.7 2.2-.7 2.8-1.8"/>',
  cog: '<circle cx="12" cy="12" r="6.3"/><circle cx="12" cy="12" r="2.4"/><path d="M12 2.8v3M12 18.2v3M4 7.4l2.6 1.5M17.4 15.1 20 16.6M4 16.6l2.6-1.5M17.4 8.9 20 7.4"/>',
  info: '<circle cx="12" cy="12" r="8.8"/><path d="M12 11v5.2M12 7.9v.1"/>',
  sidebar:
    '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/><path d="M5.6 8h1M5.6 11h1"/>',
  'sidebar-right':
    '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/><path d="M17.4 8h1M17.4 11h1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  import:
    '<path d="M12 3.5V15m-4.6-4.6L12 15l4.6-4.6"/><path d="M4.5 15.5v2.5a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-2.5"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.3"/><path d="m15.6 15.6 4.4 4.4"/>',
  pencil: '<path d="M15.2 4.8a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4L15.2 4.8Z"/><path d="m13.5 6.5 3 3"/>',
  trash:
    '<path d="M4.5 7h15M9.5 7V4.8h5V7M6.5 7l.9 12.2a1 1 0 0 0 1 .8h7.2a1 1 0 0 0 1-.8L17.5 7"/><path d="M10 10.8v5.4M14 10.8v5.4"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.2"/><path d="M15.5 8.5V6.2A2.2 2.2 0 0 0 13.3 4H6.2A2.2 2.2 0 0 0 4 6.2v7.1a2.2 2.2 0 0 0 2.2 2.2h2.3"/>',
  branch:
    '<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="6.5" r="2"/><path d="M6.5 7.5v9"/><path d="M17.5 8.5v.8a4 4 0 0 1-4 4h-3.2a3.8 3.8 0 0 0-3.8 3.2"/>',
  'chevron-left': '<path d="m14.5 6-6 6 6 6"/>',
  'chevron-right': '<path d="m9.5 6 6 6-6 6"/>',
  'chevron-down': '<path d="m6 9.5 6 6 6-6"/>',
  'chevron-up': '<path d="m6 14.5 6-6 6 6"/>',
  'arrow-up': '<path d="M12 19.5v-15M6 10.5l6-6 6 6"/>',
  'arrow-down': '<path d="M12 4.5v15M6 13.5l6 6 6-6"/>',
  'arrow-right': '<path d="M4.5 12h15M13.5 6l6 6-6 6"/>',
  'arrow-left': '<path d="M19.5 12h-15M10.5 6l-6 6 6 6"/>',
  refresh:
    '<path d="M4.5 11.5a7.5 7.5 0 0 1 12.9-4.7L20 9.3"/><path d="M20 4.5v4.8h-4.8"/><path d="M19.5 12.5a7.5 7.5 0 0 1-12.9 4.7L4 14.7"/><path d="M4 19.5v-4.8h4.8"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.2"/>',
  layers:
    '<path d="m12 3.8-8.8 4.4L12 12.6l8.8-4.4L12 3.8Z"/><path d="m3.2 12.2 8.8 4.4 8.8-4.4"/><path d="m3.2 16.2 8.8 4.4 8.8-4.4"/>',
  activity: '<path d="M3 12.5h3.8l2.6-6.5 5 13 2.6-6.5H21"/>',
  close: '<path d="m6.5 6.5 11 11M17.5 6.5l-11 11"/>',
  notebook:
    '<rect x="5" y="3" width="14.5" height="18" rx="2.2"/><path d="M9 3v18"/><path d="M12.3 8h4M12.3 11.5h4"/>',
  braces:
    '<path d="M8.5 4C6.6 4 6 5 6 7v2.3c0 1.4-.8 2.7-2 2.7 1.2 0 2 1.3 2 2.7V17c0 2 .6 3 2.5 3"/><path d="M15.5 4c1.9 0 2.5 1 2.5 3v2.3c0 1.4.8 2.7 2 2.7-1.2 0-2 1.3-2 2.7V17c0 2-.6 3-2.5 3"/><path d="M12 9v.1M12 15v.1"/>',
  eye: '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
  bolt: '<path d="M13.2 3 5.5 13.4h6L10.8 21l7.7-10.4h-6L13.2 3Z"/>',
  compass: '<circle cx="12" cy="12" r="8.8"/><path d="m15.6 8.4-2.2 5-5 2.2 2.2-5 5-2.2Z"/>',
  wrench:
    '<path d="M14.6 3.7a4.6 4.6 0 0 0-1.1 4.9l-8.8 8.8a1.9 1.9 0 0 0 2.7 2.7l8.8-8.8a4.6 4.6 0 0 0 5.9-5.6l-2.8 2.8-2.6-.6-.6-2.6 2.8-2.8a4.6 4.6 0 0 0-4.3 1.2Z"/>',
  map: '<path d="m3.5 6.5 5.5-2.3 6 2.3 5.5-2.3v13.3L15 19.8l-6-2.3-5.5 2.3V6.5Z"/><path d="M9 4.2v13.3M15 6.5v13.3"/>',
  sliders:
    '<path d="M4 7h9M18.5 7H20M4 17h3M11.5 17H20"/><circle cx="15.8" cy="7" r="2.3"/><circle cx="9.2" cy="17" r="2.3"/>',
  flag: '<path d="M5.5 21V4.5"/><path d="M5.5 4.5h11l-2.3 4 2.3 4h-11"/>',
  sparkle:
    '<path d="m12 3.5 1.9 5.2 5.1 1.8-5.1 1.8L12 17.5l-1.9-5.2L5 10.5l5.1-1.8L12 3.5Z"/><path d="M18.5 16v4M16.5 18h4"/>',
  book: '<path d="M12 6.5C10.2 5 7.8 4.5 4 4.5v13c3.8 0 6.2.5 8 2 1.8-1.5 4.2-2 8-2v-13c-3.8 0-6.2.5-8 2Z"/><path d="M12 6.5v13"/>',
  feather:
    '<path d="M20 4C13 4 8.3 8.4 6.8 14.6L5.5 19.5l4.9-1.3C16.6 16.7 20 11.5 20 4Z"/><path d="M5.5 19.5 13 12"/>',
  external:
    '<path d="M14 4.5h5.5V10M19.5 4.5 11 13"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  file: '<path d="M14 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5l-5-5Z"/><path d="M14 3.5v5h5M9 13h6M9 16.5h4"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  more: '<path d="M6 12h.1M12 12h.1M18 12h.1"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  clock: '<circle cx="12" cy="12" r="8.8"/><path d="M12 7.5V12l3 2"/>',
  memory:
    '<path d="M9 4.5a3 3 0 0 0-3 3 3 3 0 0 0-2 5.3A3 3 0 0 0 7 17.5a2.5 2.5 0 0 0 5 .5V6.5a2 2 0 0 0-3-2Z"/><path d="M15 4.5a3 3 0 0 1 3 3 3 3 0 0 1 2 5.3 3 3 0 0 1-3 4.7 2.5 2.5 0 0 1-5 .5"/>',
};
withDefaults(defineProps<{ name: string; size?: number; stroke?: number }>(), { size: 18, stroke: 1.6 });
</script>

<template>
  <svg
    class="icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    v-html="icons[name] || ''"
  ></svg>
</template>
