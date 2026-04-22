"use client";
import { FlowEvent, SimulationResult } from "@/lib/api";

const DIR_COLORS: Record<string,string> = { BUY:"#22c55e", SELL:"#ef4444", HOLD:"#e8b84b" };
const ROLE_LABELS: Record<string,string> = {
  momentum:"Momentum", contrarian:"Contrarian", fundamental:"Fundamental",
  technical:"Technical", macro:"Macro", risk:"Risk Mgr",
};

interface Props {
  ticker:      string | null;
  token:       string;
  events:      FlowEvent[];
  result:      SimulationResult | null;
  loading:     boolean;
  progress:    number;
  error:       string | null;
  nAgents:     number;
  nRuns:       number;
  onNAgents:   (n: number) => void;
  onNRuns:     (n: number) => void;
  onRun:       () => void;
}

export function SimulationPanel({
  ticker, events, result, loading, progress, error,
  nAgents, nRuns, onNAgents, onNRuns, onRun,
}: Props) {

  const btnStyle = (active: boolean, color: string) => ({
    padding:"5px 12px", borderRadius:6, cursor:"pointer", transition:"all 0.2s",
    fontFamily:"'JetBrains Mono',monospace", fontSize:10, fontWeight:700,
    background: active ? `${color}18` : "transparent",
    border:     `1px solid ${active ? color+"50" : "rgba(30,45,74,0.6)"}`,
    color:      active ? color : "#546882",
  });

  return (
    <div style={{ padding:16, display:"flex", flexDirection:"column", gap:14, height:"100%" }}>
      <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.2em", color:"#304060" }}>
        AI SWARM SIMULATION
      </div>

      {/* Config */}
      <div style={{ display:"flex", gap:16, flexWrap:"wrap" }}>
        <div>
          <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#1e2d4a", letterSpacing:"0.15em", marginBottom:6 }}>AGENTS</div>
          <div style={{ display:"flex", gap:5 }}>
            {[3,6,9,12].map(n => (
              <button key={n} onClick={() => onNAgents(n)} style={btnStyle(nAgents===n,"#00d4ff")}>{n}</button>
            ))}
          </div>
        </div>
        <div>
          <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#1e2d4a", letterSpacing:"0.15em", marginBottom:6 }}>RUNS</div>
          <div style={{ display:"flex", gap:5 }}>
            {[1,2,3].map(n => (
              <button key={n} onClick={() => onNRuns(n)} style={btnStyle(nRuns===n,"#a855f7")}>{n}</button>
            ))}
          </div>
        </div>
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"flex-end" }}>
          <button onClick={onRun} disabled={loading || !ticker}
            style={{
              padding:"8px 20px", borderRadius:8, cursor: loading||!ticker?"not-allowed":"pointer",
              fontFamily:"'JetBrains Mono',monospace", fontSize:9, fontWeight:700, letterSpacing:"0.15em",
              background: loading||!ticker ? "rgba(30,45,74,0.15)" : "rgba(0,212,255,0.08)",
              border:     `1px solid ${loading||!ticker ? "rgba(30,45,74,0.4)" : "rgba(0,212,255,0.3)"}`,
              color:      loading||!ticker ? "#304060" : "#00d4ff",
              transition:"all 0.2s",
            }}>
            {loading ? "RUNNING…" : "RUN SWARM"}
          </button>
        </div>
      </div>

      {/* Progress bar */}
      {loading && (
        <div>
          <div style={{ display:"flex", justifyContent:"space-between", marginBottom:6 }}>
            <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060" }}>
              QUERYING {nAgents * nRuns} AGENTS
            </span>
            <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#00d4ff" }}>{progress}%</span>
          </div>
          <div style={{ height:3, borderRadius:4, background:"rgba(30,45,74,0.6)", overflow:"hidden" }}>
            <div style={{ height:"100%", width:`${progress}%`, background:"linear-gradient(90deg,#00d4ff,#a855f7)",
              borderRadius:4, transition:"width 0.4s ease", boxShadow:"0 0 8px #00d4ff60" }} />
          </div>
        </div>
      )}

      {error && (
        <div style={{ padding:"10px 12px", borderRadius:8, background:"rgba(239,68,68,0.06)",
          border:"1px solid rgba(239,68,68,0.25)",
          fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#ef4444" }}>
          ⚠ {error}
        </div>
      )}

      {/* Results */}
      {result && !loading && (
        <div style={{ display:"flex", flexDirection:"column", gap:12, flex:1, overflowY:"auto" }}>
          {/* Verdict bar */}
          <div style={{ display:"flex", gap:10 }}>
            {(["BUY","SELL","HOLD"] as const).map(dir => {
              const votes = dir==="BUY" ? result.bull_votes : dir==="SELL" ? result.bear_votes : result.hold_votes;
              const total = result.bull_votes + result.bear_votes + result.hold_votes;
              const pct   = total ? Math.round(votes/total*100) : 0;
              const c     = DIR_COLORS[dir];
              return (
                <div key={dir} style={{ flex:1, padding:"10px 0", borderRadius:8, textAlign:"center",
                  background:`${c}${dir===result.direction?"18":"0a"}`,
                  border:`1px solid ${dir===result.direction?c+"40":c+"15"}`,
                }}>
                  <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:16, fontWeight:700, color:c }}>{pct}%</div>
                  <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060", marginTop:3 }}>{dir} ({votes})</div>
                </div>
              );
            })}
          </div>

          {/* Verdict */}
          <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, lineHeight:1.7, color:"#9baec8",
            padding:"10px 12px", borderRadius:8, background:"rgba(9,14,29,0.6)", border:"1px solid rgba(30,45,74,0.4)" }}>
            {result.summary}
          </div>

          {/* Agent grid */}
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:6 }}>
            {result.agents.map((a, i) => (
              <div key={i} style={{ padding:"8px 10px", borderRadius:7,
                background:"rgba(9,14,29,0.5)", border:`1px solid ${DIR_COLORS[a.direction] || "#1e2d4a"}20` }}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:4 }}>
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060" }}>
                    {ROLE_LABELS[a.role] || a.role}
                  </span>
                  <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, fontWeight:700,
                    color:DIR_COLORS[a.direction] || "#9baec8" }}>{a.direction}</span>
                </div>
                <p style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#546882",
                  lineHeight:1.6, margin:0, overflow:"hidden",
                  display:"-webkit-box", WebkitLineClamp:2, WebkitBoxOrient:"vertical" as const }}>
                  {a.reasoning}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {!result && !loading && !ticker && (
        <div style={{ flex:1, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:9, color:"#1e2d4a", letterSpacing:"0.15em" }}>
            SCAN FLOW THEN RUN SWARM
          </span>
        </div>
      )}
    </div>
  );
}
