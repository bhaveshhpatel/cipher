"use client";
import { useState, useEffect, useRef, useCallback } from "react";

export interface WsSignal {
  ticker:string; direction:string; contract_type:string; strike:number;
  expiry:string; total_premium:number; trade_count:number; alert_level:string;
  is_accelerating:boolean; seed_episode:string; timestamp:string;
}

export function useSignalStream(token:string|null) {
  const [signals,setSignals] = useState<WsSignal[]>([]);
  const [connected,setConnected] = useState(false);
  const wsRef = useRef<WebSocket|null>(null);

  useEffect(()=>{
    if (!token) return;
    const wsUrl=(process.env.NEXT_PUBLIC_WS_URL||"ws://localhost:8000")+`/ws/signals?token=${token}`;
    const connect=()=>{
      const ws=new WebSocket(wsUrl); wsRef.current=ws;
      ws.onopen=()=>setConnected(true);
      ws.onclose=()=>{ setConnected(false); setTimeout(connect,3000); };
      ws.onerror=()=>setConnected(false);
      ws.onmessage=(e)=>{
        try { const m=JSON.parse(e.data); if(m.type==="signal") setSignals(p=>[m.data,...p].slice(0,200)); } catch {}
      };
    };
    connect();
    return ()=>{ wsRef.current?.close(); };
  },[token]);

  const clear=useCallback(()=>setSignals([]),[]);
  return { signals, connected, clear };
}
