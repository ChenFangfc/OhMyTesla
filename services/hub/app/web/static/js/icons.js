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
  sliders:   '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  battery:   '<rect x="2.5" y="7" width="17" height="10" rx="2.5"/><path d="M21.5 10.5v3"/><path d="M5.5 10v4"/>',
  thermo:    '<path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z"/><path d="M12 17v-6"/>',
  seat:      '<path d="M7 3.5c1.4 0 2.3.9 2.5 2.3L11 14h6a2 2 0 0 1 2 2v1.5H8.2a2 2 0 0 1-2-1.7L4.6 6.4C4.3 4.8 5.4 3.5 7 3.5Z"/><path d="M8 17.5V21M17 17.5V21"/>',
  fan:       '<circle cx="12" cy="12" r="1.6"/><path d="M12 10.4C12 6 13.5 3.5 16 3.5c2 0 2.8 2.4 1.2 4.1-1.4 1.5-3.4 2.4-5.2 2.8ZM13.6 12c4.4 0 6.9 1.5 6.9 4 0 2-2.4 2.8-4.1 1.2-1.5-1.4-2.4-3.4-2.8-5.2ZM12 13.6c0 4.4-1.5 6.9-4 6.9-2 0-2.8-2.4-1.2-4.1 1.4-1.5 3.4-2.4 5.2-2.8ZM10.4 12C6 12 3.5 10.5 3.5 8c0-2 2.4-2.8 4.1-1.2 1.5 1.4 2.4 3.4 2.8 5.2Z"/>',
  wheel:     '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.2"/><path d="M3.8 10.5c2.5-.7 5.2-.7 6 .3M20.2 10.5c-2.5-.7-5.2-.7-6 .3M12 14.2V20.5"/>',
  bed:       '<path d="M2.5 15h19M4 15V9.5L9 7h12v8"/><path d="M9 7v8M13 10h5"/><circle cx="7" cy="17.5" r="1.8"/><circle cx="17.5" cy="17.5" r="1.8"/>',
  outlet:    '<rect x="4" y="3.5" width="16" height="17" rx="3"/><path d="M9.5 9v2.5M14.5 9v2.5M10 16h4"/>',
  tent:      '<path d="M3 20 12 4l9 16H3Z"/><path d="M12 4v16M9 20l3-6 3 6"/>',
  height:    '<path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4"/><path d="M3 12h4M17 12h4"/>',
  garage:    '<path d="M3 10.5 12 4l9 6.5V20H3v-9.5Z"/><path d="M7 20v-7h10v7M7 15.5h10M7 18h10"/>',
  key:       '<circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M16 7l2.5 2.5M14 9l2 2"/>',
  sun:       '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  defrost:   '<path d="M3 17c2.5-6.5 15.5-6.5 18 0"/><path d="M8 13.2V8.5M12 12.2V6M16 13.2V8.5M10.5 8.5 12 6l1.5 2.5"/>',
  dog:       '<path d="M5 9.5 4 4l4 2.5h8L20 4l-1 5.5"/><path d="M5 9.5c0 5 3 9.5 7 9.5s7-4.5 7-9.5"/><circle cx="9.5" cy="12" r=".8"/><circle cx="14.5" cy="12" r=".8"/><path d="M11 15.5h2"/>',
  next:      '<path d="M6 5.5v13l9-6.5-9-6.5ZM18 5.5v13"/>',
  prev:      '<path d="M18 5.5v13l-9-6.5 9-6.5ZM6 5.5v13"/>',
  volume:    '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
  download:  '<path d="M12 4v12M8 12l4 4 4-4M4 20h16"/>',
  clock:     '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  map:       '<path d="M9 4 3 6.5v13.5L9 17.5l6 2.5 6-2.5V4l-6 2.5L9 4Z"/><path d="M9 4v13.5M15 6.5V20"/>',
  timeline:  '<path d="M6 4v16"/><circle cx="6" cy="7" r="2"/><circle cx="6" cy="17" r="2"/><path d="M11 7h9M11 12h6M11 17h9"/>',
  menu:      '<path d="M4 7h16M4 12h16M4 17h16"/>',
  car:       '<path d="M4 15.5V12l2-5h12l2 5v3.5"/><path d="M3 15.5h18v2.5H3z"/><circle cx="7.5" cy="18" r="1.6"/><circle cx="16.5" cy="18" r="1.6"/>',
  wind:      '<path d="M3 9h11a3 3 0 1 0-3-3M3 15h15a3 3 0 1 1-3 3M3 12h7"/>',
  refresh:   '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 4.5v4h-4"/>',
  power:     '<path d="M12 3v8M7.2 6.3a7.5 7.5 0 1 0 9.6 0"/>',
  plus:      '<path d="M12 5v14M5 12h14"/>',
  minus:     '<path d="M5 12h14"/>',
  warn:      '<path d="M12 4 2.5 20h19L12 4Z"/><path d="M12 10v4.5M12 17.2v.1"/>',
  info:      '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 8v.1"/>',
  link:      '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  phone:     '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  chevdown:  '<path d="m6 9 6 6 6-6"/>',
  eye:       '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
  tire:      '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4"/>',
  cube:      '<path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/>',
  user:      '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/>',
  alarm:     '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 18h14v3H5zM12 3v2M4.2 6.2l1.4 1.4M19.8 6.2l-1.4 1.4"/>',
  external:  '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
};

/* size defaults to the control grid; stroke inherits currentColor */
function icon(name, size) {
  const p = ICONS[name] || ICONS.dots;
  const s = size || 24;
  return `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor"
            stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true">${p}</svg>`;
}
