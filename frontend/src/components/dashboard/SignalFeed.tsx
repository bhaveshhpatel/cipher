"use client";
import { useEffect, useRef } from "react";
import { WsSignal } from "@/hooks/useSignalStream";

const LEVEL_STYLES: Record<string, { border:string; glow:string; badge:string; badgeBg:string }> = {
  CONVICTION:    { border:"rgba(0,212,255,0.5)",  glow:"0 0 16px rgba(0,212,255,0.12)", badge:"#00d4ff", badgeBg:"rgba(0,212,255,0.1)"   },
  STRONG_SIGNAL: { border:"rgba(168,85,247,0.4)", glow:"0 0 12px rgba(168,85,247,0.1)", badge:"#a855f7", badgeBg:"rgba(168,85,247,0.1)"  },
  ALERT:         { border:"rgba(232,184,75,0.35)",glow:"0 0 8px rgba(232,184,75,0.08)", badge:"#e8b84b", badgeBg:"rgba(232,184,75,0.08)" },
  WATCH:         { border:"rgba(30,45,74,0.6)",   glow:"none",                          badge:"#546882", badgeBg:"rgba(30,45,74,0.15)"   },
};

function premiumStr(prem: number) {
  if (prem >= 1_000_000) return `$${(prem/1_000_000).toFixed(1)}M`;
  if (prem >= 1_000)     return `$${(prem/1000).toFixed(0)}K`;
  return `$${prem}`;
}

function timeAgo(ts: string) {
  const secs = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (secs < 60)  return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs/60)}m`;
  return `${Math.floor(secs/3600)}h`;
}

interface Props { signals: WsSignal[]; onClear: () => void; }

export function SignalFeed({ signals, onClear }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior:"smooth" });
  }, [signals.length]);

  const counts = {
    CONVICTION:    signals.filter(s => s.alert_level === "CONVICTION").length,
    STRONG_SIGNAL: signals.filter(s => s.alert_level === "STRONG_SIGNAL").length,
    ALERT:         signals.filter(s => s.alert_level === "ALERT").length,
    WATCH:         signals.filter(s => s.alert_level === "WATCH").length,
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"10px 14px",
        borderBottom:"1px solid rgba(30,45,74,0.6)", flexShrink:0, background:"rgba(6,8,16,0.7)" }}>
        <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.2em", color:"#304060", flex:1 }}>
          LIVE SIGNAL FEED
        </span>
        {Object.entries(counts).filter(([,v])=>v>0).map(([lvl, cnt]) => {
          const s = LEVEL_STYLES[lvl];
          return (
            <div key={lvl} style={{ padding:"2px 8px", borderRadius:5,
              background:s.badgeBg, border:`1px solid ${s.badge}40`,
              fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:s.badge }}>
              {cnt} {lvl.replace("_"," ")}
            </div>
          );
        })}
        {signals.length > 0 && (
          <button onClick={onClear} style={{
            fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060",
            background:"none", border:"none", cursor:"pointer", letterSpacing:"0.12em" }}>
            CLEAR
          </button>
        )}
      </div>

      {/* Feed */}
      <div style={{ flex:1, overflowY:"auto", padding:"8px 10px", display:"flex", flexDirection:"column", gap:6 }}>
        {signals.length === 0 ? (
          <div style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", gap:10 }}>
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#1e2d4a" strokeWidth="1.5">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
            </svg>
            <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#1e2d4a", letterSpacing:"0.2em" }}>
              AWAITING SIGNALS
            </span>
          </div>
        ) : (
          [...signals].reverse().map((sig, i) => {
            const s = LEVEL_STYLES[sig.alert_level] || LEVEL_STYLES.WATCH;
            const isBull = sig.direction.includes("BUY");
            return (
              <div key={i} style={{
                padding:"10px 12px", borderRadius:9,
                background:`rgba(9,14,29,0.7)`, border:`1px solid ${s.border}`,
                boxShadow: s.glow,
                animation: i === 0 ? "fadeUp 0.35s cubic-bezier(0.16,1,0.3,1)" : "none",
              }}>
                <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:5 }}>
                  {/* Ticker */}
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:13, fontWeight:800,
                    color:"#e8edf5", letterSpacing:"0.08em" }}>{sig.ticker}</span>
                  {/* Direction */}
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, fontWeight:700,
                    color: isBull ? "#22c55e" : "#ef4444",
                    padding:"2px 8px", borderRadius:4,
                    background: isBull ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)" }}>
                    {sig.contract_type} {isBull?"↑":"↓"}
                  </span>
                  {sig.is_accelerating && (
                    <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#e8b84b",
                      padding:"2px 7px", borderRadius:4, background:"rgba(232,184,75,0.08)",
                      border:"1px solid rgba(232,184,75,0.2)" }}>⚡ ACCEL</span>
                  )}
                  {/* Level badge */}
                  <span style={{ marginLeft:"auto", fontFamily:"'JetBrains Mono',monospace", fontSize:8,
                    color:s.badge, background:s.badgeBg, padding:"2px 8px", borderRadius:4,
                    border:`1px solid ${s.badge}35` }}>
                    {sig.alert_level.replace("_"," ")}
                  </span>
                </div>

                <div style={{ display:"flex", gap:12, alignItems:"center" }}>
                  <div>
                    <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:12, fontWeight:700,
                      color:"#e8b84b" }}>{premiumStr(sig.total_premium)}</span>
                    <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060", marginLeft:5 }}>total prem</span>
                  </div>
                  <div style={{ width:1, height:12, background:"rgba(30,45,74,0.8)" }} />
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#546882" }}>
                    ${sig.strike} exp {sig.expiry}
                  </span>
                  <div style={{ width:1, height:12, background:"rgba(30,45,74,0.8)" }} />
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060" }}>
                    {sig.trade_count}x fills
                  </span>
                  <span style={{ marginLeft:"auto", fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#1e2d4a" }}>
                    {timeAgo(sig.timestamp)}
                  </span>
                </div>

                <div style={{ marginTop:6, fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060",
                  lineHeight:1.5, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                  {sig.seed_episode}
                </div>
              </div>
            );
          })
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
