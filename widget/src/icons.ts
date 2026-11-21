/**
 * The widget's icons: thin line icons (24×24, 1.75 stroke, Lucide's shapes), drawn in the text colour. Fixed
 * strings of ours only, never text from a message, so they may go in as markup.
 */
const PATHS = {
  paperclip:
    '<path d="m16 6-8.41 8.59a2 2 0 0 0 2.83 2.83l8.41-8.59a4 4 0 0 0-5.66-5.66l-8.41 8.59a6 6 0 1 0 8.49 8.49L21 12"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><path d="M12 18v4"/>',
  send: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  newChat:
    '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 1 1 3 3L12 15l-4 1 1-4Z"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  sparkle:
    '<path d="M9.94 14.06 4 16l5.94 1.94L12 24l2.06-6.06L20 16l-5.94-1.94L12 8Z" transform="translate(0 -4)"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
} as const;

export type IconName = keyof typeof PATHS;

/** An icon as markup, hidden from screen readers (its button carries the name). */
export function icon(name: IconName, size = 18): string {
  return (
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75"` +
    ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${PATHS[name]}</svg>`
  );
}
