"use client";
import { useEffect, useState } from "react";
import { api, CompositeSignal } from "@/lib/api";

interface Props { ticker: string | null; token: string; }

export function CompositeCard({ ticker, token }: Props) {
  const [data,    setData]    = useState<CompositeSignal | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!ticker || !token) return;
    setLoading(true);
    api.getComposite(ticker, token)
      .then(setData).catch(() => {}).finally(() => setLoading(false));
  }, [ticker, token]);

  const REC_COLORS: Record<string,string> = { BUY:"#22c55e", SELL:"#ef4444", HOLD:"#e8b84b" };
  const pct = (n: number) => `${(n * 100).toFixed(0)}%`;

  return (
    <div style={{ padding:16, height:"100%", display:"flex", flexDirection:"column", gap:16 }}>
      <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.2em", color:"#304060" }}>
        COMPOSITE SIGNAL
      </div>

      {!ticker ? (
        <div style={{ flex:1, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#1e2d4a", letterSpacing:"0.15em" }}>SCAN A TICKER</span>
        </div>
      ) : loading ? (
        <div style={{ flex:1, display:"flex", flexDirection:"column", gap:10 }}>
          {Array.from({length:4}).map((_,i) => (
            <div key={i} className="skeleton" style={{ height:30, borderRadius:8 }} />
          ))}
        </div>
      ) : data ? (
        <>
          {/* Recommendation */}
          <div style={{
            padding:"18px 20px", borderRadius:12, textAlign:"center",
            background:`${REC_COLORS[data.recommendation] || "#546882"}10`,
            border:`1px solid ${REC_COLORS[data.recommendation] || "#546882"}30`,
          }}>
            <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:28, fontWeight:700,
              color: REC_COLORS[data.recommendation] || "#9baec8", letterSpacing:"0.1em",
              textShadow:`0 0 24px ${REC_COLORS[data.recommendation] || "#9baec8"}60`,
            }}>{data.recommendation}</div>
            <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:11, color:"#546882", marginTop:4 }}>
              {data.ticker} · composite {pct(data.composite_score)}
            </div>
          </div>

          {/* Score bars */}
          {[
            { label:"COMPOSITE", value:data.composite_score, color:"#00d4ff" },
            { label:"FLOW SCORE", value:data.flow_score, color:"#e8b84b" },
            { label:"BACKTEST",   value:data.backtest_score, color:"#a855f7" },
          ].map(({ label, value, color }) => (
            <div key={label}>
              <div style={{ display:"flex", justifyContent:"space-between", marginBottom:6 }}>
                <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#304060", letterSpacing:"0.12em" }}>{label}</span>
                <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, color, fontWeight:700 }}>{pct(value)}</span>
              </div>
              <div style={{ height:5, borderRadius:4, background:"rgba(30,45,74,0.6)", overflow:"hidden" }}>
                <div style={{ height:"100%", width:`${value*100}%`, borderRadius:4, background:color,
                  boxShadow:`0 0 8px ${color}60`, transition:"width 0.8s cubic-bezier(0.16,1,0.3,1)" }} />
              </div>
            </div>
          ))}

          {/* Reasoning */}
          <div style={{
            padding:"10px 12px", borderRadius:8,
            background:"rgba(9,14,29,0.6)", border:"1px solid rgba(30,45,74,0.4)",
            fontFamily:"'JetBrains Mono',monospace", fontSize:10, lineHeight:1.65, color:"#9baec8",
          }}>
            {data.reasoning}
          </div>
        </>
      ) : null}
    </div>
  );
}
