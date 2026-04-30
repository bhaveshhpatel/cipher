"use client";
import { clsx } from "clsx";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "destructive";
export type ButtonSize    = "sm" | "md" | "lg";

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?:  ButtonVariant;
  size?:     ButtonSize;
  loading?:  boolean;
  iconLeft?: React.ReactNode;
  iconRight?: React.ReactNode;
}

const variantClass: Record<ButtonVariant, string> = {
  primary:     "btn btn-primary",
  secondary:   "btn btn-secondary",
  ghost:       "btn btn-ghost",
  destructive: "btn btn-destructive",
};

const sizeClass: Record<ButtonSize, string> = {
  sm: "h-7  px-3   text-xs  gap-1.5",
  md: "h-9  px-4   text-sm  gap-2",
  lg: "h-11 px-5   text-base gap-2",
};

export function Button({
  variant  = "primary",
  size     = "md",
  loading  = false,
  iconLeft,
  iconRight,
  disabled,
  className,
  children,
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;

  return (
    <button
      {...rest}
      disabled={isDisabled}
      className={clsx(
        variantClass[variant],
        sizeClass[size],
        "inline-flex items-center justify-center",
        className,
      )}
    >
      {loading ? (
        <Spinner size="sm" className="mr-1.5" />
      ) : (
        iconLeft && <span className="shrink-0">{iconLeft}</span>
      )}
      {children}
      {!loading && iconRight && (
        <span className="shrink-0">{iconRight}</span>
      )}
    </button>
  );
}
