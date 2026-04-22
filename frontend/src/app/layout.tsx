import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title:"Cipher — Decode the Market",
  description:"Institutional options flow intelligence.",
  themeColor:"#060b18",
};
export default function RootLayout({ children }:{ children:React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </head>
      <body style={{ background:"#060b18", color:"#e8edf5" }}>{children}</body>
    </html>
  );
}
