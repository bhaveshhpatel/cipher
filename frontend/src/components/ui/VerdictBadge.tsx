"use client";
import { Badge } from "./Badge";
import type { Verdict } from "@/types";

export interface VerdictBadgeProps {
  verdict:    Verdict;
  size?:      "sm" | "md";
  className?: string;
}

const VERDICT_VARIANT = {
  BUY:  "buy",
  SELL: "sell",
  HOLD: "hold",
} as const;

export function VerdictBadge({ verdict, size = "md", className }: VerdictBadgeProps) {
  return (
    <Badge variant={VERDICT_VARIANT[verdict]} size={size} className={className}>
      {verdict}
    </Badge>
  );
}
