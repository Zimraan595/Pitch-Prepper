---
name: Pitch Prepper
colors:
  surface: '#fcf8ff'
  surface-dim: '#dcd8e5'
  surface-bright: '#fcf8ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f5f2ff'
  surface-container: '#f0ecf9'
  surface-container-high: '#eae6f4'
  surface-container-highest: '#e4e1ee'
  on-surface: '#1b1b24'
  on-surface-variant: '#464555'
  inverse-surface: '#302f39'
  inverse-on-surface: '#f3effc'
  outline: '#777587'
  outline-variant: '#c7c4d8'
  surface-tint: '#4d44e3'
  primary: '#3525cd'
  on-primary: '#ffffff'
  primary-container: '#4f46e5'
  on-primary-container: '#dad7ff'
  inverse-primary: '#c3c0ff'
  secondary: '#006c49'
  on-secondary: '#ffffff'
  secondary-container: '#6cf8bb'
  on-secondary-container: '#00714d'
  tertiary: '#684000'
  on-tertiary: '#ffffff'
  tertiary-container: '#885500'
  on-tertiary-container: '#ffd4a4'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e2dfff'
  primary-fixed-dim: '#c3c0ff'
  on-primary-fixed: '#0f0069'
  on-primary-fixed-variant: '#3323cc'
  secondary-fixed: '#6ffbbe'
  secondary-fixed-dim: '#4edea3'
  on-secondary-fixed: '#002113'
  on-secondary-fixed-variant: '#005236'
  tertiary-fixed: '#ffddb8'
  tertiary-fixed-dim: '#ffb95f'
  on-tertiary-fixed: '#2a1700'
  on-tertiary-fixed-variant: '#653e00'
  background: '#fcf8ff'
  on-background: '#1b1b24'
  surface-variant: '#e4e1ee'
typography:
  headline-lg:
    fontFamily: Geist
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-lg-mobile:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Geist
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.05em
  metric-xl:
    fontFamily: Geist
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 48px
    letterSpacing: -0.04em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  container-max: 1280px
  gutter: 1.5rem
  margin-mobile: 1rem
  margin-desktop: 2rem
  stack-sm: 0.5rem
  stack-md: 1rem
  stack-lg: 2rem
---

## Brand & Style
The design system is engineered for **Pitch Prepper**, an AI-driven speaking coach that balances sophisticated technology with approachable mentorship. The brand personality is professional, authoritative, and encouraging. It targets professionals, educators, and students who require high-fidelity feedback on their vocal performance.

The visual style is **Corporate / Modern** with a focus on data density and clarity. It prioritizes functional minimalism, using generous white space and a structured grid to make complex analytical data digestible. The emotional response should be one of confidence and progress; the UI acts as a calm, objective lens through which users view their growth.

## Colors
The palette is rooted in a sophisticated deep indigo for primary actions, establishing a sense of trust and technical "smarts." 

A rigorous semantic system governs data visualization and feedback:
- **Success (Emerald):** Applied to "Good" scores (≥ 75) and positive reinforcement.
- **Warning (Amber):** Applied to "Fair" scores (55-74) and areas requiring attention.
- **Error (Rose):** Applied to "Poor" scores (< 55) and critical errors.
- **Neutral:** The background uses a soft slate gray to reduce eye strain, while cards and primary containers use pure white to pop against the base.

## Typography
This design system utilizes a dual-font approach to maximize legibility and technical feel. **Geist** is used for headlines, labels, and high-impact metrics to provide a precise, developer-friendly aesthetic. **Inter** is used for all body copy and descriptions for its exceptional readability in data-heavy environments.

Headlines should use tighter letter-spacing for a modern look, while labels utilize uppercase styling and increased tracking for clarity at small sizes.

## Layout & Spacing
The layout follows a **Fixed Grid** philosophy on desktop, centered within a 1280px container. It utilizes a 12-column system for maximum flexibility in dashboard layouts. 

- **Desktop:** 2rem (32px) outer margins with 1.5rem (24px) gutters.
- **Tablet:** 8-column grid with 1.5rem gutters.
- **Mobile:** 4-column fluid grid with 1rem (16px) gutters and margins.

Spacing follows an 8px base unit (4, 8, 16, 24, 32, 48, 64) to ensure a consistent vertical rhythm. Use "stack" variables to define vertical relationships between related data points.

## Elevation & Depth
Hierarchy is established through **Tonal Layers** and **Ambient Shadows**. Surfaces do not rely on heavy gradients but rather on subtle contrast and shadow depth.

- **Level 0 (Background):** #F8FAFC.
- **Level 1 (Cards/Surfaces):** White (#FFFFFF) with a `shadow-sm` (0 1px 2px 0 rgba(0,0,0,0.05)) and a 1px border of #E2E8F0.
- **Level 2 (Active/Hover):** White (#FFFFFF) with `shadow-md` (0 4px 6px -1px rgba(0,0,0,0.1)).
- **Overlays:** Modals use a background blur (`backdrop-filter: blur(8px)`) with a semi-transparent slate overlay to maintain focus on the content.

## Shapes
The shape language is "Rounded," utilizing an 8px base radius for standard elements. This provides a clean, modern look that isn't overly organic or sharp.

- **Standard Elements (Inputs, Small Cards):** 8px radius.
- **Large Elements (Main Containers, Modals):** 12px (rounded-lg) to 16px (rounded-xl) radius.
- **Pills/Tags:** Full radius (999px) to distinguish them from interactive buttons.

## Components
- **Buttons:** Primary buttons use the indigo background with white text. Hover states should darken the background slightly. Secondary buttons use a subtle gray border with indigo text.
- **Radial Gauges:** Use a 2px or 3px stroke width. The track should be a light gray (#F1F5F9) while the indicator uses the semantic color (Emerald, Amber, or Rose) based on the score value.
- **Horizontal Progress Bars:** 8px height, fully rounded. Use a subtle gray track and a solid semantic fill.
- **Pill Tags:** Used for keywords. Use light backgrounds (e.g., Indigo-50) with Indigo-700 text to keep the interface light but readable.
- **Grid Metrics Cards:** Feature a large "metric-xl" Geist font for the main number, a label above, and a small sparkline or trend indicator below.
- **Input Fields:** 1px border (#E2E8F0), focusing to a 2px Indigo outline. Use Geist for placeholder text to maintain the technical aesthetic.
- **Lists:** Clean lines with no dividers between items; use vertical spacing and subtle hover backgrounds to define rows.