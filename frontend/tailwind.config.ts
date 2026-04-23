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
        bg:          "var(--bg)",
        surface:     "var(--surface)",
        "surface-2": "var(--surface-2)",
        border:      "var(--border)",
        text:        "var(--text)",
        muted:       "var(--muted)",
        faint:       "var(--faint)",
        amber:       "var(--amber)",
        teal:        "var(--teal)",
        green:       "var(--green)",
        red:         "var(--red)",
        blue:        "var(--blue)",
      },
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "1rem" }],
      },
      borderRadius: {
        DEFAULT: "0.375rem",
        lg:      "0.5rem",
        xl:      "0.75rem",
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
      },
      animation: {
        "fade-up": "fade-up 0.25s ease-out both",
        shimmer:   "shimmer 1.5s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
