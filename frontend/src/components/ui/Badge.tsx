"use client";
import { clsx } from "clsx";

export type BadgeVariant =
  | "default" | "amber" | "red" | "green" | "blue" | "muted"
  | "tier-1" | "tier-2" | "tier-3"
  | "buy" | "sell" | "hold";

export type BadgeSize = "sm" | "md";

export interface BadgeProps {
  variant?: BadgeVariant;
  size?:    BadgeSize;
  dot?:     boolean;
  className?: string;
  children: React.ReactNode;
}

const variantClass: Record<BadgeVariant, string> = {
  default:  "badge-amber",
  amber:    "badge-amber",
  red:      "badge-red",
  green:    "badge-green",
  blue:     "badge-blue",
  muted:    "badge-muted",
  "tier-1": "badge-tier-1",
  "tier-2": "badge-tier-2",
  "tier-3": "badge-tier-3",
  buy:      "badge-buy",
  sell:     "badge-sell",
  hold:     "badge-hold",
};

const sizeClass: Record<BadgeSize, string> = {
  sm: "text-2xs px-1.5 py-0.5",
  md: "text-xs  px-2   py-0.5",
};

export function Badge({
  variant  = "default",
  size     = "md",
  dot      = false,
  className,
  children,
}: BadgeProps) {
  return (
    <span
      className={clsx(
        "badge",
        variantClass[variant],
        sizeClass[size],
        className,
      )}
    >
      {dot && (
        <span
          className={clsx(
            "inline-block w-1.5 h-1.5 rounded-full mr-1.5 shrink-0",
            "bg-current opacity-80",
          )}
        />
      )}
      {children}
    </span>
  );
}
