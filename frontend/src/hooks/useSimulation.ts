"use client";
import { useState, useCallback } from "react";
import { api, FlowEvent, SimulationResult } from "@/lib/api";

export function useSimulation(token:string|null) {
  const [result,setResult]=useState<SimulationResult|null>(null);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [progress,setProgress]=useState(0);
  const run=useCallback(async(ticker:string,events:FlowEvent[],nAgents:number,nRuns:number)=>{
    if(!token) return;
    setLoading(true); setError(null); setProgress(0); setResult(null);
    const iv=setInterval(()=>setProgress(p=>Math.min(p+3,90)),500);
    try {
      const d=await api.runSimulation(ticker,events,nAgents,nRuns,token);
      setResult(d); setProgress(100);
    } catch(e:unknown){ setError(e instanceof Error?e.message:"Simulation failed"); }
    finally { clearInterval(iv); setLoading(false); }
  },[token]);
  return { result, loading, error, progress, run };
}
