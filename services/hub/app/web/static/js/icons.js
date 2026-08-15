/* One thin-line icon set for the whole product (§31).
 * 24×24 grid, 1.6px strokes, rounded caps, geometric, minimal detail.
 * Emoji are never used as UI icons — they drag in another vendor's colour
 * palette and break the monochrome surface.
 */
const ICONS = {
  lock:      '<path d="M7 11V8a5 5 0 0 1 10 0v3"/><rect x="4" y="11" width="16" height="10" rx="2"/>',
  unlock:    '<path d="M7 11V8a5 5 0 0 1 9.6-2"/><rect x="4" y="11" width="16" height="10" rx="2"/>',
  climate:   '<path d="M12 2v20M12 6l3.5-2.6M12 6 8.5 3.4M12 18l3.5 2.6M12 18l-3.5 2.6"/>'
           + '<path d="m3.3 7 17.4 10M7 8.2 5.5 3.2M5.5 3.2 9.8 4.3M17 15.8l1.5 5M18.5 20.8 14.2 19.7"/>'
           + '<path d="m20.7 7-17.4 10M17 8.2l1.5-5M18.5 3.2 14.2 4.3M7 15.8l-1.5 5M5.5 20.8 9.8 19.7"/>',
  bolt:      '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
  shield:    '<path d="M12 3 4.5 6v6c0 4.4 3.1 8.4 7.5 9.5 4.4-1.1 7.5-5.1 7.5-9.5V6L12 3Z"/>',
  plug:      '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0V8ZM12 17v5"/>',
  gauge:     '<path d="M12 21a9 9 0 1 1 9-9"/><path d="m12 12 5-3"/>',
  lightbulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.5.4.8 1 .9 1.6h5.2c.1-.6.4-1.2.9-1.6A6 6 0 0 0 12 3Z"/>',
  horn:      '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>',
  box:       '<path d="M3 8h18v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8ZM3 8l2.4-4.2A2 2 0 0 1 7.1 3h9.8a2 2 0 0 1 1.7 1L21 8M12 3v5"/>',
  window:    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 12h18M12 4v16"/>',
  pin:       '<path d="M12 21s7-5.3 7-11a7 7 0 1 0-14 0c0 5.7 7 11 7 11Z"/><circle cx="12" cy="10" r="2.5"/>',
  route:     '<circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M8.5 19H15a3.5 3.5 0 0 0 0-7H9a3.5 3.5 0 0 1 0-7h6.5"/>',
  film:      '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M7 5v14M17 5v14M2.5 12h19M2.5 8.5h4.5M2.5 15.5h4.5M17 8.5h4.5M17 15.5h4.5"/>',
  drive:     '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M8 20v-6h8v6M12 4v4"/>',
  chart:     '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  gear:      '<circle cx="12" cy="12" r="3.2"/>'
           + '<path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.9 19.3a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.7 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9.1A1.7 1.7 0 0 0 10.1 3.1V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.08a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.56 1.03Z"/>',
  home:      '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1v-9.5Z"/>',
  dots:      '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  arrow:     '<path d="M5 12h13M13 6l6 6-6 6"/>',
  spark:     '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>',
  chevron:   '<path d="m9 6 6 6-6 6"/>',
  check:     '<path d="m5 13 4 4L19 7"/>',
  x:         '<path d="M6 6l12 12M18 6 6 18"/>',
  play:      '<path d="M8 5.5v13l11-6.5-11-6.5Z"/>',
  upload:    '<path d="M12 16V4M8 8l4-4 4 4M4 20h16"/>',
  bell:      '<path d="M18 8a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7ZM10.3 21a2 2 0 0 0 3.4 0"/>',
  search:    '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
};

/* size defaults to the control grid; stroke inherits currentColor */
function icon(name, size) {
  const p = ICONS[name] || ICONS.dots;
  const s = size || 24;
  return `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor"
            stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true">${p}</svg>`;
}
