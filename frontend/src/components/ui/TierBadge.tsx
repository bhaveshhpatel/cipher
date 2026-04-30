"use client";
import { Badge } from "./Badge";
import type { Tier } from "@/types";

export interface TierBadgeProps {
  tier:       Tier;
  showLabel?: boolean;
  size?:      "sm" | "md";
  className?: string;
}

const TIER_LABEL: Record<Tier, string> = {
  1: "T1",
  2: "T2",
  3: "T3",
};

const TIER_VARIANT = {
  1: "tier-1",
  2: "tier-2",
  3: "tier-3",
} as const;

export function TierBadge({
  tier,
  showLabel = true,
  size      = "md",
  className,
}: TierBadgeProps) {
  return (
    <Badge
      variant={TIER_VARIANT[tier]}
      size={size}
      dot
      className={className}
    >
      {showLabel ? TIER_LABEL[tier] : null}
    </Badge>
  );
}
