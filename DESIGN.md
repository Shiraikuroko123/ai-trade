---
name: AI Trade
version: "0.12.0"
description: A calm, auditable workstation for systematic investment decisions.
colors:
  closing-bell-honey: "oklch(0.76 0.15 78)"
  closing-bell-honey-hover: "oklch(0.71 0.16 76)"
  honey-wash: "oklch(0.94 0.05 80)"
  ledger-teal: "oklch(0.45 0.09 195)"
  clear-desk: "oklch(1 0 0)"
  sidebar-mist: "oklch(0.965 0.008 220)"
  quiet-surface: "oklch(0.985 0.004 220)"
  graphite-ink: "oklch(0.24 0.018 250)"
  secondary-ink: "oklch(0.43 0.02 245)"
  quiet-rule: "oklch(0.88 0.012 230)"
  positive: "oklch(0.43 0.12 145)"
  negative: "oklch(0.47 0.16 25)"
  warning: "oklch(0.49 0.11 65)"
typography:
  headline:
    fontFamily: "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "1.35rem"
    fontWeight: 720
    lineHeight: 1.25
    letterSpacing: "0"
  title:
    fontFamily: "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "0.98rem"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "0"
  body:
    fontFamily: "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  label:
    fontFamily: "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "0"
  numeric:
    fontFamily: "Cascadia Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0"
rounded:
  control: "6px"
  container: "7px"
  pill: "999px"
spacing:
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.5rem"
  xl: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.closing-bell-honey}"
    textColor: "{colors.graphite-ink}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.85rem"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.closing-bell-honey-hover}"
    textColor: "{colors.graphite-ink}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.85rem"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.clear-desk}"
    textColor: "{colors.graphite-ink}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.85rem"
    height: "44px"
  status-chip:
    backgroundColor: "{colors.quiet-surface}"
    textColor: "{colors.secondary-ink}"
    rounded: "{rounded.pill}"
    padding: "0.2rem 0.55rem"
---

# Design System: AI Trade

## 1. Overview

**Creative North Star: "The Closing Desk"**

AI Trade feels like a well-run investment desk after the market closes: bright enough for sustained review, dense enough for evidence comparison, and quiet enough that warnings and permissions remain unmistakable. TradingView informs information density, Linear informs interaction clarity, and Stripe informs data hierarchy; none is copied literally.

This is an operational product, not a marketing surface. Information arrives in full-width bands, unframed panels, compact tables, and restrained state color. Desktop keeps comparison context visible; mobile structurally reflows to a fixed bottom navigation and scrollable data tables without shrinking the information model.

**Key Characteristics:**

- The release baseline is the `v0.12.0` local workstation, including the professional read-only market view.
- Evidence appears before action.
- Honey-amber is reserved for current authority and primary commands.
- Tables, charts, ledgers, and gate checks share one compact vocabulary.
- Research, paper, sandbox, and live-disabled stages are never visually conflated.
- Motion communicates state in 150–250 ms and disappears under reduced-motion preferences.

## 2. Colors

The palette is a literal white workspace with cool, low-chroma structural surfaces. Honey-amber marks authority, ledger teal marks evidence, and semantic colors always carry text or symbols.

### Primary

- **Closing Bell Honey:** the primary command, current navigation selection, and focused promotion stage. It never decorates inactive space.

### Secondary

- **Ledger Teal:** evidence links, strategy chart series, information callouts, and non-authoritative highlights.

### Neutral

- **Clear Desk:** the application canvas.
- **Sidebar Mist:** the desktop navigation surface.
- **Quiet Surface:** metric bands and low-emphasis structure.
- **Graphite Ink:** body text and numeric emphasis.
- **Quiet Rule:** dividers, table rows, and inactive boundaries.

### Named Rules

**The Ten Percent Rule.** Closing Bell Honey occupies no more than ten percent of a screen. Its rarity tells the user where authority sits.

**The Signed State Rule.** Profit, loss, success, and failure always pair color with a sign, label, or familiar symbol.

## 3. Typography

**Display Font:** Segoe UI with Microsoft YaHei UI and system sans fallbacks
**Body Font:** the same system stack
**Label/Mono Font:** Cascadia Mono with Consolas fallback for comparable financial figures

**Character:** Native, technical-humanist, and fast. A tight fixed scale lets weight, alignment, and tabular numerals carry hierarchy without display-type theatrics.

### Hierarchy

- **Headline** (720, 1.35rem, 1.25): page titles only.
- **Title** (650, 0.98rem, 1.25): workflow panels and tables.
- **Body** (400, 1rem, 1.5): prose capped near 72 characters.
- **Label** (650, 0.875rem, 1.4): controls, compact metadata, and table headers; never uppercase-tracked.
- **Numeric** (400, 0.875rem, 1.4): tabular financial values and identifiers.

### Named Rules

**The Numeric Alignment Rule.** Comparable financial figures use tabular numerals and align by sign or decimal.

## 4. Elevation

The workstation is flat by default. Tonal surfaces and one-pixel dividers establish ownership; ordinary panels, tables, metrics, and buttons use no decorative shadow. Depth is reserved for browser-native top layers such as future dialogs or tooltips.

### Named Rules

**The Accounting Surface Rule.** A shadow never implies accounting ownership. Ledgers, positions, and limits use structure and labels.

## 5. Components

### Buttons

- **Shape:** restrained rectangular control with gently curved corners (6px) and a 44px minimum height.
- **Primary:** Closing Bell Honey with Graphite Ink; one primary command per workflow region.
- **Secondary:** Clear Desk with a strong quiet-rule border.
- **Hover / Focus:** darker role color over 170–180 ms; a visible three-pixel teal-derived focus outline.
- **Disabled:** low-contrast neutral surface, explicit disabled cursor, and unchanged geometry.

### Chips

- **Style:** full-pill only for compact status, bordered in its own semantic color.
- **State:** success, warning, error, information, and neutral each include readable text; color never stands alone.

### Cards / Containers

- **Corner Style:** panels are unframed; genuine callouts and the operating-stage card use 7px corners.
- **Background:** white or Quiet Surface, never glass.
- **Shadow Strategy:** none for ordinary content.
- **Border:** full one-pixel boundaries or horizontal dividers; colored side stripes are forbidden.
- **Internal Padding:** one rem for compact framed content.

### Inputs / Fields

- **Style:** white fill, one-pixel structural border, 6px corners, and 44px minimum height.
- **Focus:** the shared teal-derived focus outline.
- **Error / Disabled:** semantic label plus role color; no layout shift.

### Navigation

Desktop uses a fixed 224px sidebar with compact 44px rows. Mobile replaces it at 820px with a fixed, horizontally scrollable 68px bottom navigation. Current selection uses Honey Wash and a honey edge; hover and focus preserve stable dimensions.

### Evidence Chart

Canvas charts have fixed responsive height, high-DPI rendering, two named series at most, restrained grid lines, three date labels, and a visible text summary. A chart with insufficient points renders an explanatory state rather than an empty canvas.

### Market Workstation

The market route uses locally vendored KLineChart assets and keeps price, volume, and one oscillator in a stable three-pane layout. Daily, weekly, and monthly modes share one control vocabulary. Source provider, adjustment, data date, completion cutoff, manifest hash, symbol-file hash, and stale/missing status remain adjacent to the chart. A-share red-up/green-down color always carries signed text, and mobile reorders the quote strip below the chart so the primary inspection surface appears first without hiding evidence.

## 6. Do's and Don'ts

### Do:

- **Do** show data dates, model configuration, account stage, and live permissions near decision surfaces.
- **Do** separate the completed market date from the time the current page payload was generated.
- **Do** keep chart controls usable at 320px and destroy chart instances when leaving the market route.
- **Do** keep repeated workflows compact, keyboard-accessible, and readable at 200% zoom.
- **Do** explain errors as what happened, why it matters, and the next valid action.
- **Do** preserve every core workflow on mobile through structural reflow.
- **Do** keep real-order controls visibly disabled until every independent gate passes.

### Don't:

- **Don't** use profit-guarantee marketing, countdown urgency, social proof, or language implying the model cannot lose.
- **Don't** use flashing red/green gambling-terminal aesthetics or decorative ticker walls.
- **Don't** use opaque AI-agent theatre where commentary replaces deterministic evidence, parameters, and code.
- **Don't** use marketing hero layouts, oversized slogans, glassmorphism, purple-blue gradients, or endless decorative card grids.
- **Don't** hide stale data, rejected orders, drawdown, configuration drift, or unavailable live permissions.
- **Don't** imply that a daily chart is intraday, real-time, exchange-certified, or an order-entry surface.
- **Don't** use colored side-stripe borders, gradient text, decorative grid backgrounds, or shadows on ordinary accounting surfaces.
