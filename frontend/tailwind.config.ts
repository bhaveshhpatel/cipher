import type { Config } from "tailwindcss";
const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        cipher: {
          bg:"#060b18", surface:"#0c1428", border:"#1e2d4a",
          cyan:"#00d4ff", gold:"#e8b84b", purple:"#a855f7",
          text:"#e8edf5", muted:"#9baec8", faint:"#546882", dim:"#304060",
        },
      },
      fontFamily: { mono:["JetBrains Mono","monospace"], sans:["Inter","sans-serif"] },
    },
  },
  plugins: [],
};
export default config;
