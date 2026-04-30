import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
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
        text:         "var(--text)",
        muted:        "var(--muted)",
        faint:        "var(--faint)",
        amber:        "var(--amber)",
        teal:         "var(--teal)",
        green:        "var(--green)",
        red:          "var(--red)",
        blue:         "var(--blue)",
        orange:       "var(--orange)",
        gold:         "var(--gold)",
        cyan:         "var(--cyan)",
        indigo:       "var(--indigo)",
        purple:       "var(--purple)",
        "tier-1":     "var(--tier-1)",
        "tier-2":     "var(--tier-2)",
        "tier-3":     "var(--tier-3)",
        "verdict-buy":  "var(--verdict-buy)",
        "verdict-sell": "var(--verdict-sell)",
        "verdict-hold": "var(--verdict-hold)",
      },
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "1rem" }],
      },
      borderRadius: {
        DEFAULT: "0.375rem",
        lg:      "0.5rem",
        xl:      "0.75rem",
      },
      boxShadow: {
        card:     "var(--shadow-card)",
        elevated: "var(--shadow-elevated)",
        sidebar:  "var(--shadow-sidebar)",
        modal:    "var(--shadow-modal)",
      },
      transitionDuration: {
        fast: "var(--duration-fast)",
        base: "var(--duration-base)",
        slow: "var(--duration-slow)",
      },
      keyframes: {
        "fade-up": {
          "0%":   { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%":   { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition:  "200% 0" },
        },
        "modal-in": {
          "0%":   { opacity: "0", transform: "scale(0.96) translateY(4px)" },
          "100%": { opacity: "1", transform: "scale(1) translateY(0)" },
        },
        "modal-out": {
          "0%":   { opacity: "1", transform: "scale(1) translateY(0)" },
          "100%": { opacity: "0", transform: "scale(0.96) translateY(4px)" },
        },
        "pulse-dot": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%":      { opacity: "0.35", transform: "scale(0.75)" },
        },
      },
      animation: {
        "fade-up":   "fade-up 0.25s ease-out both",
        shimmer:     "shimmer 1.5s ease-in-out infinite",
        "modal-in":  "modal-in 0.2s cubic-bezier(0.16,1,0.3,1) both",
        "modal-out": "modal-out 0.15s cubic-bezier(0.16,1,0.3,1) both",
        "pulse-dot": "pulse-dot 2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
