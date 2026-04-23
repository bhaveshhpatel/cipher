"use client";
// NOTE: This is a client layout so we can read system preference for dark mode on first load.
// The <html> tag gets the "dark" class based on localStorage + system preference.
// We use a small inline script to prevent flash of wrong theme.

import type { Metadata } from "next";
import "./globals.css";

// Inline script prevents flash-of-wrong-theme (FOUT) on load
const themeScript = `
(function(){
  try {
    var s = localStorage.getItem('cipher-theme');
    if (s === 'dark' || (!s && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.classList.add('dark');
    }
  } catch(e){}
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="theme-color" content="#f4f6fa" media="(prefers-color-scheme: light)" />
        <meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)" />
        <title>Cipher — Decode the Market</title>
        <meta name="description" content="Institutional options flow intelligence. Real-time whale detection and AI swarm simulation." />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap"
          rel="stylesheet"
        />
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
