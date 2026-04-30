"use client";
import { clsx } from "clsx";

export interface CardProps {
  /** Extra classes on the outer wrapper */
  className?: string;
  /** Removes default padding — useful for full-bleed tables */
  noPadding?: boolean;
  children: React.ReactNode;
}

export interface CardHeaderProps {
  title:      React.ReactNode;
  subtitle?:  React.ReactNode;
  action?:    React.ReactNode;
  className?: string;
}

export interface CardBodyProps {
  className?: string;
  children:   React.ReactNode;
}

/** Outer card shell */
export function Card({ className, noPadding = false, children }: CardProps) {
  return (
    <div className={clsx("card", !noPadding && "p-4", className)}>
      {children}
    </div>
  );
}

/** Title row with optional subtitle + right-aligned action slot */
export function CardHeader({ title, subtitle, action, className }: CardHeaderProps) {
  return (
    <div className={clsx("flex items-start justify-between gap-3 mb-3", className)}>
      <div className="min-w-0">
        <h3 className="text-sm font-semibold text-text leading-tight truncate">
          {title}
        </h3>
        {subtitle && (
          <p className="text-xs text-muted mt-0.5 leading-tight">{subtitle}</p>
        )}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** Body wrapper — adds top border when used after CardHeader */
export function CardBody({ className, children }: CardBodyProps) {
  return (
    <div className={clsx("pt-3 border-t border-border", className)}>
      {children}
    </div>
  );
}
