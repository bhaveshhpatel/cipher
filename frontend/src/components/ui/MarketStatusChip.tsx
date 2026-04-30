"use client";
import { clsx } from "clsx";
import type { MarketStatus } from "@/types";

export interface MarketStatusChipProps {
  status:     MarketStatus;
  className?: string;
}

const STATUS_CONFIG: Record<
  MarketStatus,
  { label: string; dotColor: string; textColor: string }
> = {
  open:   { label: "Market Open",   dotColor: "bg-[var(--market-open)]",   textColor: "text-[var(--market-open)]"   },
  closed: { label: "Market Closed", dotColor: "bg-[var(--market-closed)]", textColor: "text-[var(--market-closed)]" },
  pre:    { label: "Pre-Market",    dotColor: "bg-[var(--market-pre)]",    textColor: "text-[var(--market-pre)]"    },
  after:  { label: "After-Hours",   dotColor: "bg-[var(--market-after)]",  textColor: "text-[var(--market-after)]"  },
};

export function MarketStatusChip({ status, className }: MarketStatusChipProps) {
  const { label, dotColor, textColor } = STATUS_CONFIG[status];
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 text-xs font-medium",
        textColor,
        className,
      )}
    >
      <span
        className={clsx(
          "w-1.5 h-1.5 rounded-full shrink-0",
          dotColor,
          status === "open" && "pulse-dot",
        )}
      />
      {label}
    </span>
  );
}
