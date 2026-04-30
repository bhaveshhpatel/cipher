"use client";
import { clsx } from "clsx";

export type SpinnerSize = "xs" | "sm" | "md" | "lg";

export interface SpinnerProps {
  size?:      SpinnerSize;
  className?: string;
  /** Screen-reader label */
  label?:     string;
}

const sizeClass: Record<SpinnerSize, string> = {
  xs: "w-3 h-3 border",
  sm: "w-4 h-4 border",
  md: "w-5 h-5 border-2",
  lg: "w-7 h-7 border-2",
};

export function Spinner({ size = "md", className, label = "Loading" }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={label}
      className={clsx(
        "inline-block rounded-full",
        "border-current border-t-transparent",
        "animate-spin-dot",
        sizeClass[size],
        className,
      )}
    />
  );
}
