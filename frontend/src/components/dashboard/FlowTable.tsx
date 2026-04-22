"use client";
import { useState } from "react";
import { FlowEvent } from "@/lib/api";

const TIER: Record<string, { color: string; bg: string }> = {
  WHALE:         { color:"#00d4ff", bg:"rgba(0,212,255,0.1)"   },
  INSTITUTIONAL: { color:"#a855f7", bg:"rgba(168,85,247,0.1)"  },
  LARGE:         { color:"#e8b84b", bg:"rgba(232,184,75,0.1)"  },
  RETAIL:        { color:"#546882", bg:"rgba(84,104,130,0.08)" },
};

interface Props {
  events:   FlowEvent[];
  ticker:   string;
  loading:  boolean;
  onSearch: (ticker: string) => void;
}

export function FlowTable({ events, ticker, loading, onSearch }: Props) {
  const [input, setInput] = useState(ticker || "");
  const bull      = events.filter(e => e.sentiment === "BULLISH").length;
  const bear      = events.filter(e => e.sentiment === "BEARISH").length;
  const totalPrem = events.reduce((s, e) => s + (e.premium || 0), 0);
  const premStr   = totalPrem >= 1_000_000
    ? `$${(totalPrem / 1_000_000).toFixed(1)}M`
    : `$${(totalPrem / 1000).toFixed(0)}K`;

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%" }}>
      {/* Search bar */}
      <div style={{
        display:"flex", alignItems:"center", gap:12, padding:"10px 16px", flexShrink:0,
        borderBottom:"1px solid rgba(30,45,74,0.6)", background:"rgba(9,14,29,0.4)",
      }}>
        <div style={{ display:"flex", gap:8, flex:1 }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value.toUpperCase())}
            onKeyDown={e => e.key === "Enter" && input && onSearch(input)}
            placeholder="TICKER" maxLength={8}
            style={{
              width:100, padding:"8px 12px", borderRadius:8, textTransform:"uppercase",
              fontFamily:"'JetBrains Mono',monospace", fontSize:12, fontWeight:700,
              background:"rgba(6,11,24,0.8)", border:"1px solid rgba(30,45,74,0.8)",
              color:"#e8edf5", outline:"none", letterSpacing:"0.12em",
            }}
            onFocus={e => { (e.target as HTMLInputElement).style.borderColor = "rgba(232,184,75,0.5)"; }}
            onBlur ={e => { (e.target as HTMLInputElement).style.borderColor = "rgba(30,45,74,0.8)";  }}
          />
          <button onClick={() => input && onSearch(input)} style={{
            padding:"8px 18px", borderRadius:8,
            fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.2em", fontWeight:700,
            background:"rgba(232,184,75,0.1)", border:"1px solid rgba(232,184,75,0.35)", color:"#e8b84b",
            cursor:"pointer", transition:"background 0.2s",
          }}
          onMouseEnter={e => { e.currentTarget.style.background = "rgba(232,184,75,0.2)"; }}
          onMouseLeave={e => { e.currentTarget.style.background = "rgba(232,184,75,0.1)"; }}>
            SCAN FLOW
          </button>
        </div>
        {events.length > 0 && (
          <div style={{ display:"flex", gap:8 }}>
            {[
              { label:"BULL", val:bull,    color:"#22c55e" },
              { label:"BEAR", val:bear,    color:"#ef4444" },
              { label:"PREM", val:premStr, color:"#e8b84b" },
            ].map(({ label, val, color }) => (
              <div key={label} style={{
                display:"flex", alignItems:"center", gap:5,
                padding:"4px 10px", borderRadius:6,
                background:`${color}0d`, border:`1px solid ${color}30`,
              }}>
                <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:11, fontWeight:700, color }}>{val}</span>
                <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060" }}>{label}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Table */}
      <div style={{ flex:1, overflowY:"auto" }}>
        {loading ? (
          <div style={{ padding:16, display:"flex", flexDirection:"column", gap:8 }}>
            {Array.from({length:10}).map((_,i) => (
              <div key={i} className="skeleton" style={{ height:40, borderRadius:8, opacity:Math.max(0.15,1-i*0.08) }} />
            ))}
          </div>
        ) : events.length === 0 ? (
          <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", height:"100%", gap:12 }}>
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#1e2d4a" strokeWidth="1.5">
              <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.25em", color:"#304060" }}>
              ENTER TICKER TO SCAN OPTIONS FLOW
            </span>
          </div>
        ) : (
          <table style={{ width:"100%", borderCollapse:"collapse" }}>
            <thead>
              <tr style={{ borderBottom:"1px solid rgba(30,45,74,0.6)", background:"rgba(9,14,29,0.7)", position:"sticky", top:0 }}>
                {["TYPE","STRIKE / EXP","PREMIUM","TIER","SENTIMENT","CONVICTION"].map(h => (
                  <th key={h} style={{
                    textAlign:"left", padding:"9px 12px",
                    fontFamily:"'JetBrains Mono',monospace", fontSize:9,
                    letterSpacing:"0.15em", color:"#304060", fontWeight:500,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => {
                const t = TIER[ev.influence_tier] || TIER.RETAIL;
                const pStr = ev.premium >= 1_000_000
                  ? `$${(ev.premium/1_000_000).toFixed(1)}M`
                  : `$${(ev.premium/1000).toFixed(0)}K`;
                return (
                  <tr key={i} style={{ borderBottom:"1px solid rgba(30,45,74,0.3)", transition:"background 0.15s" }}
                      onMouseEnter={e => { e.currentTarget.style.background = "rgba(12,20,40,0.9)"; }}
                      onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}>
                    <td style={{ padding:"11px 12px" }}>
                      <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                        {ev.is_golden_sweep && <span style={{ color:"#e8b84b", fontSize:11 }} title="Golden Sweep">★</span>}
                        <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:12, fontWeight:700,
                          color: ev.contract_type==="CALL" ? "#22c55e" : "#ef4444" }}>{ev.contract_type}</span>
                        <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060" }}>{ev.trade_type}</span>
                      </div>
                    </td>
                    <td style={{ padding:"11px 12px" }}>
                      <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:12, color:"#e8edf5", fontWeight:600 }}>${ev.strike}</div>
                      <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#546882" }}>{ev.expiry}</div>
                    </td>
                    <td style={{ padding:"11px 12px" }}>
                      <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:13, fontWeight:700,
                        color: ev.premium >= 1_000_000 ? "#e8b84b" : "#9baec8" }}>{pStr}</span>
                    </td>
                    <td style={{ padding:"11px 12px" }}>
                      <span style={{ padding:"3px 8px", borderRadius:4, background:t.bg, color:t.color,
                        fontFamily:"'JetBrains Mono',monospace", fontSize:9, fontWeight:700, letterSpacing:"0.08em" }}>
                        {ev.influence_tier}
                      </span>
                    </td>
                    <td style={{ padding:"11px 12px" }}>
                      <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, fontWeight:600,
                        color: ev.sentiment==="BULLISH"?"#22c55e":ev.sentiment==="BEARISH"?"#ef4444":"#546882" }}>
                        {ev.sentiment}
                      </span>
                    </td>
                    <td style={{ padding:"11px 12px" }}>
                      <div style={{ display:"flex", alignItems:"center", gap:8 }}>
                        <div style={{ width:56, height:4, borderRadius:4, overflow:"hidden", background:"rgba(30,45,74,0.6)" }}>
                          <div style={{
                            height:"100%", borderRadius:4,
                            width:`${ev.conviction_score*100}%`,
                            background: ev.conviction_score>0.75?"#00d4ff":ev.conviction_score>0.5?"#e8b84b":"#304060",
                            transition:"width 0.7s ease",
                          }} />
                        </div>
                        <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#546882" }}>
                          {(ev.conviction_score*100).toFixed(0)}%
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
