import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        mono: ["var(--font-mono)", "JetBrains Mono", "Fira Code", "monospace"],
      },
      colors: {
        bg:           "var(--bg)",
        surface:      "var(--surface)",
        "surface-2":  "var(--surface-2)",
        border:       "var(--border)",
        "border-2":   "var(--border-2)",
        text:         "var(--text)",
        muted:        "var(--muted)",
        faint:        "var(--faint)",
        amber:        "var(--amber)",
        "amber-light":"var(--amber-light)",
        teal:         "var(--teal)",
        green:        "var(--green)",
        red:          "var(--red)",
        blue:         "var(--blue)",
        orange:       "var(--orange)",
        gold:         "var(--gold)",
        "tier-1":     "var(--tier-1-color)",
        "tier-2":     "var(--tier-2-color)",
        "tier-3":     "var(--tier-3-color)",
        "verdict-buy":  "var(--verdict-buy)",
        "verdict-sell": "var(--verdict-sell)",
        "verdict-hold": "var(--verdict-hold)",
      },
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "1rem" }],
      },
      width: {
        sidebar:          "var(--sidebar-width-expanded)",
        "sidebar-collapsed": "var(--sidebar-width-collapsed)",
      },
      borderRadius: {
        DEFAULT: "0.375rem",
        sm:  "var(--radius-sm)",
        md:  "var(--radius-md)",
        lg:  "var(--radius-lg)",
        xl:  "var(--radius-xl)",
      },
      boxShadow: {
        card:       "var(--shadow-card)",
        "card-hover": "var(--shadow-card-hover)",
        modal:      "var(--shadow-modal)",
      },
      transitionTimingFunction: {
        spring: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        "fade-up": {
          "0%":   { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          "0%":   { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "scale-up": {
          "0%":   { opacity: "0", transform: "scale(0.95) translateY(4px)" },
          "100%": { opacity: "1", transform: "scale(1) translateY(0)" },
        },
        "slide-up": {
          "0%":   { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%":   { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition:  "200% 0" },
        },
        "pulse-dot": {
          "0%, 100%": { opacity: "1",    transform: "scale(1)" },
          "50%":      { opacity: "0.35", transform: "scale(0.75)" },
        },
      },
      animation: {
        "fade-up":  "fade-up  0.25s ease-out both",
        "fade-in":  "fade-in  0.2s  ease-out",
        "scale-up": "scale-up 0.25s cubic-bezier(0.16,1,0.3,1)",
        "slide-up": "slide-up 0.3s  cubic-bezier(0.16,1,0.3,1)",
        shimmer:    "shimmer  1.5s  ease-in-out infinite",
        "pulse-dot":"pulse-dot 2s   ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
