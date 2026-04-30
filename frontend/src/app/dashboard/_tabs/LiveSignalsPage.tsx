"use client";
import { SignalFeed } from "@/components/dashboard/SignalFeed";
import type { WsSignal } from "@/hooks/useSignalStream";

interface Props {
  signals:   WsSignal[];
  connected: boolean;
  token:     string | null;
}

export function LiveSignalsPage({ signals, connected, token }: Props) {
  return (
    <div className="flex flex-col gap-4" data-testid="live-signals-page">
      <div>
        <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Live Signal Feed</h1>
        <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
          {connected
            ? "WebSocket connected · streaming real-time signals"
            : "Connecting to stream…"}
        </p>
      </div>
      <SignalFeed signals={signals} connected={connected} token={token} />
    </div>
  );
}
