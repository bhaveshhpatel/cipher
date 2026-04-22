"use client";
import { useEffect, useState } from "react";
import { api, StreamStats } from "@/lib/api";

interface Props { token: string; connected: boolean; signalCount: number; }

export function StreamStatsBar({ token, connected, signalCount }: Props) {
  const [stats, setStats] = useState<StreamStats | null>(null);

  useEffect(() => {
    if (!token) return;
    const poll = async () => {
      try { const d = await api.getStats(token); setStats(d.stats); } catch {}
    };
    poll();
    const iv = setInterval(poll, 10_000);
    return () => clearInterval(iv);
  }, [token]);

  const items = [
    { label:"STREAM",    value: connected ? "LIVE" : "RECONNECTING", color: connected ? "#22c55e" : "#e8b84b", dot:true },
    { label:"SYMBOLS",   value: stats?.active_symbols ?? "—", color:"#9baec8" },
    { label:"TICKS",     value: stats ? stats.ticks.toLocaleString() : "—", color:"#9baec8" },
    { label:"CLASSIFIED",value: stats ? stats.classified.toLocaleString() : "—", color:"#9baec8" },
    { label:"SIGNALS",   value: signalCount.toLocaleString(), color:"#00d4ff" },
    { label:"ERRORS",    value: stats?.errors ?? "—", color: (stats?.errors ?? 0)>0 ? "#ef4444" : "#304060" },
  ];

  return (
    <div style={{
      display:"flex", alignItems:"center", gap:0,
      background:"rgba(6,8,16,0.9)", borderBottom:"1px solid rgba(30,45,74,0.6)",
      flexShrink:0, height:34, paddingLeft:16, overflow:"hidden",
    }}>
      {items.map(({ label, value, color, dot }) => (
        <div key={label} style={{ display:"flex", alignItems:"center", gap:7, paddingRight:20, borderRight:"1px solid rgba(30,45,74,0.4)", marginRight:20 }}>
          {dot && (
            <div style={{ width:6, height:6, borderRadius:"50%", background:color, boxShadow:`0 0 6px ${color}` }} />
          )}
          <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color, fontWeight:700 }}>{value}</span>
          <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060", letterSpacing:"0.15em" }}>{label}</span>
        </div>
      ))}
    </div>
  );
}
