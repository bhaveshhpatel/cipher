
"use client";
import { useState, useEffect, useRef, useCallback } from "react";

export interface WsSignal {
  ticker:string; direction:string; contract_type?:string; strike?:number;
  expiry?:string; total_premium?:number; trade_count?:number; alert_level:string;
  is_accelerating?:boolean; seed_episode?:string; timestamp:string;
}

export function useSignalStream(token:string|null) {
  const [signals,setSignals] = useState<WsSignal[]>([]);
  const [connected,setConnected] = useState(false);
  const wsRef = useRef<WebSocket|null>(null);
  const reconnectRef = useRef<number|undefined>(undefined);

  useEffect(()=>{
    if (!token) return;
    let active = true;
    const base = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";
    const wsUrl = `${base}/ws/signals?token=${token}`;
    const connect=()=>{
      if (!active) return;
      const ws=new WebSocket(wsUrl); wsRef.current=ws;
      ws.onopen=()=>setConnected(true);
      ws.onclose=()=>{ setConnected(false); if (active) reconnectRef.current = window.setTimeout(connect,3000); };
      ws.onerror=()=>setConnected(false);
      ws.onmessage=(e)=>{
        try {
          const m=JSON.parse(e.data);
          if(m.type==="ping") return;
          const payload = m.type === "signal" && m.data ? m.data : m;
          if (payload?.ticker && payload?.alert_level) setSignals(p=>[payload,...p].slice(0,200));
        } catch {}
      };
    };
    connect();
    return ()=>{ active = false; if (reconnectRef.current) window.clearTimeout(reconnectRef.current); wsRef.current?.close(); };
  },[token]);

  const clear=useCallback(()=>setSignals([]),[]);
  return { signals, connected, clear };
}
