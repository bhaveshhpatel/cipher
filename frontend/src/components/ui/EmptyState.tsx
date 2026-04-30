"use client";
import { clsx } from "clsx";

export interface EmptyStateProps {
  icon?:       React.ReactNode;
  title:       string;
  description?: string;
  action?:     React.ReactNode;
  className?:  string;
  /** Compact mode — less vertical padding, smaller text */
  compact?:    boolean;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  compact = false,
}: EmptyStateProps) {
  return (
    <div
      className={clsx(
        "flex flex-col items-center justify-center text-center",
        compact ? "py-8 gap-2" : "py-16 gap-3",
        className,
      )}
    >
      {icon && (
        <span
          className={clsx(
            "text-muted opacity-40",
            compact ? "text-2xl" : "text-4xl",
          )}
        >
          {icon}
        </span>
      )}
      <p
        className={clsx(
          "font-medium text-muted",
          compact ? "text-xs" : "text-sm",
        )}
      >
        {title}
      </p>
      {description && (
        <p className="text-xs text-faint max-w-xs">{description}</p>
      )}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
