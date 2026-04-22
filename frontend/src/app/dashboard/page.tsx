"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { CipherLogo } from "@/components/CipherLogo";
import { SignalFeed }      from "@/components/dashboard/SignalFeed";
import { FlowTable }       from "@/components/dashboard/FlowTable";
import { SimulationPanel } from "@/components/dashboard/SimulationPanel";
import { CompositeCard }   from "@/components/dashboard/CompositeCard";
import { StreamStatsBar }  from "@/components/dashboard/StreamStatsBar";
import { useAuth }         from "@/hooks/useAuth";
import { useSignalStream } from "@/hooks/useSignalStream";
import { useFlow }         from "@/hooks/useFlow";
import { useSimulation }   from "@/hooks/useSimulation";

type Tab = "flow" | "simulation";

export default function Dashboard() {
  const router            = useRouter();
  const { token, email, isAuthenticated, logout } = useAuth();
  const { signals, connected, clear } = useSignalStream(token);
  const { events, loading: flowLoading, fetch }   = useFlow(token);
  const { result, loading: simLoading, error: simError, progress, run } = useSimulation(token);

  const [ticker,  setTicker]  = useState<string>("");
  const [tab,     setTab]     = useState<Tab>("flow");
  const [nAgents, setNAgents] = useState(6);
  const [nRuns,   setNRuns]   = useState(1);

  useEffect(() => { if (!isAuthenticated && token === null) router.push("/"); }, [isAuthenticated, token, router]);

  const handleScan = (t: string) => { setTicker(t); fetch(t); };
  const handleRunSim = () => { if (ticker) run(ticker, events, nAgents, nRuns); };

  const TAB_STYLE = (active: boolean) => ({
    padding:"8px 18px", cursor:"pointer", transition:"all 0.2s",
    fontFamily:"'JetBrains Mono',monospace", fontSize:9, fontWeight:700, letterSpacing:"0.15em",
    borderBottom:`2px solid ${active?"#00d4ff":"transparent"}`,
    color: active ? "#00d4ff" : "#304060",
    background:"none",
  });

  if (!isAuthenticated && token === null) return null;

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100dvh", position:"relative", zIndex:2, overflow:"hidden" }}>

      {/* Top nav */}
      <div style={{ display:"flex", alignItems:"center", gap:16, padding:"0 20px",
        height:52, borderBottom:"1px solid rgba(30,45,74,0.7)",
        background:"rgba(6,8,16,0.92)", backdropFilter:"blur(8px)", flexShrink:0, zIndex:10 }}>
        <CipherLogo size={28} showTagline={false} />
        <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:14, fontWeight:800,
          color:"#e8edf5", letterSpacing:"0.08em", marginRight:4 }}>CIPHER</div>

        {/* Tabs */}
        <div style={{ display:"flex", gap:0, marginLeft:16, borderBottom:"none" }}>
          {([["flow","FLOW"] ,["simulation","SWARM"]] as [Tab,string][]).map(([id,label])=>(
            <button key={id} onClick={()=>setTab(id)} style={TAB_STYLE(tab===id)}>{label}</button>
          ))}
        </div>

        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:14 }}>
          {ticker && (
            <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:11, fontWeight:700,
              color:"#e8b84b", letterSpacing:"0.12em",
              padding:"4px 12px", borderRadius:6, background:"rgba(232,184,75,0.08)",
              border:"1px solid rgba(232,184,75,0.25)" }}>
              {ticker}
            </div>
          )}
          <span style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#304060" }}>{email}</span>
          <button onClick={logout} style={{
            fontFamily:"'JetBrains Mono',monospace", fontSize:9, letterSpacing:"0.15em",
            color:"#304060", padding:"5px 12px", borderRadius:6,
            border:"1px solid rgba(30,45,74,0.5)", cursor:"pointer",
            transition:"color 0.2s",
          }}
          onMouseEnter={e=>{e.currentTarget.style.color="#ef4444";}}
          onMouseLeave={e=>{e.currentTarget.style.color="#304060";}}>
            SIGN OUT
          </button>
        </div>
      </div>

      {/* Stream stats bar */}
      {token && <StreamStatsBar token={token} connected={connected} signalCount={signals.length} />}

      {/* Main layout */}
      <div style={{ flex:1, display:"grid", gridTemplateColumns:"340px 1fr 300px", overflow:"hidden" }}>

        {/* Left — live signal feed */}
        <div style={{ borderRight:"1px solid rgba(30,45,74,0.5)", overflow:"hidden", display:"flex", flexDirection:"column" }}>
          <SignalFeed signals={signals} onClear={clear} />
        </div>

        {/* Center — flow table or simulation */}
        <div style={{ overflow:"hidden", display:"flex", flexDirection:"column" }}>
          {tab === "flow" ? (
            <FlowTable events={events} ticker={ticker} loading={flowLoading} onSearch={handleScan} />
          ) : (
            <SimulationPanel
              ticker={ticker} token={token||""} events={events}
              result={result} loading={simLoading} progress={progress} error={simError}
              nAgents={nAgents} nRuns={nRuns}
              onNAgents={setNAgents} onNRuns={setNRuns} onRun={handleRunSim}
            />
          )}
        </div>

        {/* Right — composite signal card */}
        <div style={{ borderLeft:"1px solid rgba(30,45,74,0.5)", overflow:"auto" }}>
          {token && <CompositeCard ticker={ticker||null} token={token} />}
        </div>
      </div>
    </div>
  );
}
