# Huginn Theme Style Guide
**For Niri + Quickshell on Arch Linux**

This is the official reference for theming my desktop as **Huginn** — Odin's raven of Thought.  
All themes should feel like a sleek, intelligent, all-seeing raven reborn as a modern AI assistant: dark, elegant, minimal, and slightly mystical.

## Core Philosophy
- Dark, premium, calm intelligence (inspired by Tokyo Night comfort)
- Subtle Norse/raven touches only — never cartoonish or cluttered
- High contrast where it matters (focus, active states, hovers)
- Smooth, thoughtful animations and glows
- Performance-first on Wayland/Niri
- "Thought takes flight" — the core feeling

## Color Palettes (All Four Variants)

### 1. Midnight Raven (Blue) — Current Favorite
- Background: `#1a1b26`
- Surface / Panels: `#24283b`
- **Thought Glow (primary accent)**: `#89ddff`
- Rune Wisdom Gold: `#f7c95e` (shiny, luminous, with glow)
- Primary Text: `#c0caf5`
- Secondary Text: `#a9b1d6`

### 2. Obsidian Raven (Purple)
- Background: `#1a1b26`
- Surface / Panels: `#24283b`
- **Thought Glow (primary accent)**: `#bb9af7`
- Rune Wisdom Gold: `#f7c95e`
- Primary Text: `#c0caf5`
- Secondary Text: `#a9b1d6`

### 3. Ember Raven (Red)
- Background: `#1a1b26`
- Surface / Panels: `#24283b`
- **Thought Glow (primary accent)**: `#f55d7a`
- Rune Wisdom Gold: `#f7c95e`
- Primary Text: `#c0caf5`
- Secondary Text: `#a9b1d6`

### 4. Verdant Raven (Green)
- Background: `#1a1b26`
- Surface / Panels: `#24283b`
- **Thought Glow (primary accent)**: `#7ed9a3`
- Rune Wisdom Gold: `#f7c95e`
- Primary Text: `#c0caf5`
- Secondary Text: `#a9b1d6`

**Gold Rule**: The gold `#f7c95e` should always feel slightly shiny/luminous. Use subtle text-shadow or box-shadow when possible to give it inner glow.

## Theming Rules & Aesthetic Guidelines

### General Vibe
- Elegant dark minimalism with cyber-Norse soul
- Lots of negative space
- Thin, clean lines and soft glows
- Subtle mythic elements only (faint runes ᚱ ᚨ, single feather motifs, very light bind-runes)
- No busy patterns, no bright primary colors except the chosen accent

### UI Element Guidelines
- **Focus rings / Active states**: Strong accent color with soft glow (`box-shadow: 0 0 12px currentColor`)
- **Hover effects**: Gentle accent glow + slight scale or brightness lift
- **Bar / Panels**: Semi-transparent with backdrop blur if available (`rgba(36,40,59,0.85)` base)
- **Workspace indicator**: Clean pills or numbers. Active one gets strong accent background + glow
- **System monitors**: Label as "Thought Load" (CPU/RAM) or "Memory Stream"
- **Clock**: Elegant, slightly modern font, primary text color
- **Runes**: Use sparingly as separators or icons. Color them with gold `#f7c95e`

### Fonts
- UI / Sans: Inter or system sans-serif (clean and readable)
- Mono (for terminals, stats): JetBrainsMono Nerd Font
- Clock can be slightly bolder or spaced

### Animations & Polish
- Smooth fades and transitions (200–300ms)
- Subtle glow pulses on active elements (very gentle)
- Optional: tiny feather or rune fade-in on certain triggers (keep performant)

## Quickshell Implementation Notes
- Define a central `Theme` object or root properties for easy switching
- Use CSS-like variables or Qt property bindings for colors
- Prefer modern Quickshell patterns (Qt 6 style)
- Support multiple themes via a switcher (future goal)
- Keep the bar minimal: workspaces + clock + system info + optional small raven/status

## Wallpaper Recommendations
- Dark, atmospheric, minimal
- Lone raven in flight, stormy Nordic sky, faint Yggdrasil, northern lights, or subtle glowing runes
- Plenty of negative space so icons and bar don't clash

## Tagline
"Thought takes flight"

---

**When editing configs:**
- Always preserve existing keybinds and core functionality
- Make minimal, targeted changes
- Prioritize elegance and performance
- Reference this guide for all color and style decisions

Last updated: April 2026
