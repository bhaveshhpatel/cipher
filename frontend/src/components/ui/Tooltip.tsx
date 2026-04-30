"use client";
import { useState, useRef, useEffect, useCallback } from "react";
import { clsx } from "clsx";

export type TooltipPlacement = "top" | "bottom" | "left" | "right";

export interface TooltipProps {
  content:    React.ReactNode;
  placement?: TooltipPlacement;
  delay?:     number;
  className?: string;
  children:   React.ReactElement;
}

const placementClass: Record<TooltipPlacement, string> = {
  top:    "bottom-full left-1/2 -translate-x-1/2 mb-2",
  bottom: "top-full    left-1/2 -translate-x-1/2 mt-2",
  left:   "right-full  top-1/2  -translate-y-1/2 mr-2",
  right:  "left-full   top-1/2  -translate-y-1/2 ml-2",
};

export function Tooltip({
  content,
  placement = "top",
  delay     = 300,
  className,
  children,
}: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback(() => {
    timer.current = setTimeout(() => setVisible(true), delay);
  }, [delay]);

  const hide = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    setVisible(false);
  }, []);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  return (
    <span className="relative inline-flex">
      <span
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        className="inline-flex"
      >
        {children}
      </span>
      {visible && (
        <span
          role="tooltip"
          className={clsx(
            "absolute z-50 pointer-events-none",
            "px-2 py-1 rounded-md text-xs whitespace-nowrap",
            "bg-surface border border-border shadow-modal",
            "text-text animate-fade-in",
            placementClass[placement],
            className,
          )}
        >
          {content}
        </span>
      )}
    </span>
  );
}
