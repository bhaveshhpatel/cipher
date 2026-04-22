"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { CipherLogo } from "@/components/CipherLogo";
import { useAuth } from "@/hooks/useAuth";

export default function LoginPage() {
  const router = useRouter();
  const { login, register, isAuthenticated, loading, error } = useAuth();
  const [mode,     setMode]     = useState<"login"|"register">("login");
  const [email,    setEmail]    = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => { if (isAuthenticated) router.push("/dashboard"); }, [isAuthenticated, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "login") await login(email, password);
    else                  await register(email, password);
  };

  const inp = {
    background:"rgba(6,11,24,0.8)", border:"1px solid rgba(30,45,74,0.8)",
    borderRadius:8, padding:"12px 14px", width:"100%", outline:"none",
    fontFamily:"'JetBrains Mono',monospace", fontSize:12, color:"#e8edf5",
    transition:"border-color 0.2s",
  } as React.CSSProperties;

  return (
    <div style={{ minHeight:"100dvh", display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", padding:24, position:"relative", zIndex:2 }}>
      {/* Radial glow */}
      <div style={{ position:"fixed", top:"35%", left:"50%", transform:"translate(-50%,-50%)", width:600, height:600,
        background:"radial-gradient(circle, rgba(0,212,255,0.05) 0%, transparent 70%)", pointerEvents:"none" }} />

      {/* Card */}
      <div style={{
        width:"100%", maxWidth:420,
        background:"rgba(12,20,40,0.85)", backdropFilter:"blur(12px)",
        border:"1px solid rgba(30,45,74,0.8)", borderRadius:16,
        padding:40, display:"flex", flexDirection:"column", alignItems:"center", gap:28,
        boxShadow:"0 24px 64px rgba(0,0,0,0.5)",
        position:"relative", overflow:"hidden",
      }}>
        {/* Cyan accent line at top */}
        <div style={{ position:"absolute", top:0, left:"15%", right:"15%", height:1,
          background:"linear-gradient(90deg,transparent,#00d4ff80,transparent)" }} />

        <CipherLogo size={80} showTagline />

        {/* Mode tabs */}
        <div style={{ display:"flex", gap:0, width:"100%", background:"rgba(6,11,24,0.6)",
          borderRadius:8, border:"1px solid rgba(30,45,74,0.6)", overflow:"hidden" }}>
          {(["login","register"] as const).map(m => (
            <button key={m} onClick={()=>setMode(m)} style={{
              flex:1, padding:"9px 0",
              fontFamily:"'JetBrains Mono',monospace", fontSize:9,
              letterSpacing:"0.2em", fontWeight:700, cursor:"pointer",
              background: mode===m ? "rgba(0,212,255,0.1)" : "transparent",
              color: mode===m ? "#00d4ff" : "#304060",
              borderRight: m==="login" ? "1px solid rgba(30,45,74,0.6)" : "none",
              transition:"all 0.2s", textTransform:"uppercase",
            }}>
              {m === "login" ? "SIGN IN" : "REGISTER"}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit} style={{ width:"100%", display:"flex", flexDirection:"column", gap:14 }}>
          <div>
            <label style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060", letterSpacing:"0.15em", display:"block", marginBottom:7 }}>EMAIL</label>
            <input type="email" value={email} onChange={e=>setEmail(e.target.value)} required
              placeholder="trader@cipher.io" style={inp}
              onFocus={e=>{(e.target as HTMLInputElement).style.borderColor="rgba(0,212,255,0.4)";}}
              onBlur={e=>{(e.target as HTMLInputElement).style.borderColor="rgba(30,45,74,0.8)";}} />
          </div>
          <div>
            <label style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:8, color:"#304060", letterSpacing:"0.15em", display:"block", marginBottom:7 }}>PASSWORD</label>
            <input type="password" value={password} onChange={e=>setPassword(e.target.value)} required
              placeholder="••••••••" style={inp}
              onFocus={e=>{(e.target as HTMLInputElement).style.borderColor="rgba(0,212,255,0.4)";}}
              onBlur={e=>{(e.target as HTMLInputElement).style.borderColor="rgba(30,45,74,0.8)";}} />
          </div>

          {error && (
            <div style={{ padding:"9px 12px", borderRadius:7, background:"rgba(239,68,68,0.07)", border:"1px solid rgba(239,68,68,0.25)",
              fontFamily:"'JetBrains Mono',monospace", fontSize:10, color:"#ef4444" }}>⚠ {error}</div>
          )}

          <button type="submit" disabled={loading} style={{
            padding:"13px 0", borderRadius:8, marginTop:4, cursor: loading?"not-allowed":"pointer",
            fontFamily:"'JetBrains Mono',monospace", fontSize:10, fontWeight:700, letterSpacing:"0.2em",
            background: loading ? "rgba(30,45,74,0.3)" : "rgba(0,212,255,0.1)",
            border:`1px solid ${loading?"rgba(30,45,74,0.4)":"rgba(0,212,255,0.4)"}`,
            color: loading ? "#304060" : "#00d4ff",
            transition:"all 0.2s",
            boxShadow: loading ? "none" : "0 0 16px rgba(0,212,255,0.12)",
          }}>
            {loading ? "AUTHENTICATING…" : mode==="login" ? "ACCESS CIPHER" : "CREATE ACCOUNT"}
          </button>
        </form>

        {/* Feature pills */}
        <div style={{ display:"flex", gap:8, flexWrap:"wrap", justifyContent:"center" }}>
          {["LIVE FLOW","WHALE ALERTS","AI SWARM","COMPOSITE SIGNALS"].map(f=>(
            <span key={f} style={{ padding:"4px 10px", borderRadius:20,
              fontFamily:"'JetBrains Mono',monospace", fontSize:8, letterSpacing:"0.15em",
              color:"#304060", background:"rgba(30,45,74,0.2)", border:"1px solid rgba(30,45,74,0.4)" }}>
              {f}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
