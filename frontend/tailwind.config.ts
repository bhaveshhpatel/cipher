import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        cipher: {
          // Light mode surfaces
          "bg":          "#f4f6fa",
          "surface":     "#ffffff",
          "surface-2":   "#f9fafc",
          "border":      "#dde2ec",
          "border-2":    "#c8d0e0",
          // Dark mode surfaces
          "dark-bg":     "#0d1117",
          "dark-surface":"#161b27",
          "dark-surface-2":"#1e2535",
          "dark-border": "#2a3347",
          "dark-border-2":"#354160",
          // Brand accents
          "amber":       "#e8a020",
          "amber-light": "#f5c04a",
          "amber-dim":   "#7a4e00",
          "teal":        "#0a9b8c",
          "teal-light":  "#12c4b0",
          "teal-dim":    "#04504a",
          "red":         "#dc3545",
          "red-light":   "#ff6b7a",
          "green":       "#1a9e5a",
          "green-light": "#22c870",
          "blue":        "#1a6ef5",
          "blue-light":  "#4d94ff",
          // Text
          "text":        "#111827",
          "muted":       "#4b5563",
          "faint":       "#9ca3af",
          "dark-text":   "#e8edf5",
          "dark-muted":  "#8899b4",
          "dark-faint":  "#4a5568",
        },
      },
      fontFamily: {
        mono:  ["JetBrains Mono", "Menlo", "monospace"],
        sans:  ["Inter", "system-ui", "sans-serif"],
        display: ["Inter", "system-ui", "sans-serif"],
      },
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "1rem" }],   // 10px
        "xs":  ["0.75rem",  { lineHeight: "1.1rem" }], // 12px
        "sm":  ["0.875rem", { lineHeight: "1.4rem" }], // 14px
        "base":["1rem",     { lineHeight: "1.6rem" }], // 16px
        "lg":  ["1.125rem", { lineHeight: "1.7rem" }], // 18px
        "xl":  ["1.25rem",  { lineHeight: "1.75rem"}], // 20px
        "2xl": ["1.5rem",   { lineHeight: "2rem"   }], // 24px
        "3xl": ["1.875rem", { lineHeight: "2.25rem"}], // 30px
        "4xl": ["2.25rem",  { lineHeight: "2.5rem" }], // 36px
      },
      borderRadius: {
        "sm": "0.25rem",
        "md": "0.5rem",
        "lg": "0.75rem",
        "xl": "1rem",
        "2xl":"1.5rem",
      },
      boxShadow: {
        "card":   "0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.06)",
        "card-lg":"0 4px 16px rgba(0,0,0,0.10), 0 12px 32px rgba(0,0,0,0.08)",
        "amber":  "0 0 0 3px rgba(232,160,32,0.2)",
        "teal":   "0 0 0 3px rgba(10,155,140,0.2)",
        "inset":  "inset 0 1px 2px rgba(0,0,0,0.06)",
      },
      animation: {
        "fade-up": "fadeUp 0.3s cubic-bezier(0.16,1,0.3,1)",
        "pulse-dot":"pulseDot 2s ease-in-out infinite",
        "shimmer": "shimmer 1.5s ease-in-out infinite",
        "spin-slow":"spin 3s linear infinite",
        "slide-in": "slideIn 0.25s cubic-bezier(0.16,1,0.3,1)",
      },
      keyframes: {
        fadeUp:  { from:{ opacity:"0", transform:"translateY(8px)" }, to:{ opacity:"1", transform:"translateY(0)" } },
        pulseDot:{ "0%,100%":{ opacity:"1", transform:"scale(1)" }, "50%":{ opacity:"0.35", transform:"scale(0.75)" } },
        shimmer: { "0%":{ backgroundPosition:"-400px 0" }, "100%":{ backgroundPosition:"400px 0" } },
        slideIn: { from:{ opacity:"0", transform:"translateX(-6px)" }, to:{ opacity:"1", transform:"translateX(0)" } },
      },
      transitionTimingFunction: {
        "spring": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
  },
  plugins: [],
};

export default config;
