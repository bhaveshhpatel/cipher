"use client";
import { clsx } from "clsx";

export interface SkeletonProps {
  /** Tailwind width class, e.g. "w-32" or "w-full" */
  width?:     string;
  /** Tailwind height class, e.g. "h-4" */
  height?:    string;
  rounded?:   boolean;
  className?: string;
}

export function Skeleton({
  width     = "w-full",
  height    = "h-4",
  rounded   = false,
  className,
}: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={clsx(
        "bg-surface-2 animate-pulse",
        rounded ? "rounded-full" : "rounded-md",
        width,
        height,
        className,
      )}
    />
  );
}

/** Convenience: a block of stacked skeleton rows */
export function SkeletonBlock({
  rows = 3,
  className,
}: {
  rows?: number;
  className?: string;
}) {
  return (
    <div className={clsx("space-y-2", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} width={i === rows - 1 ? "w-3/4" : "w-full"} />
      ))}
    </div>
  );
}
