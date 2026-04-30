"use client";
/**
 * Thin wrapper around react-hot-toast that applies Cipher design tokens.
 * Import `useToast` anywhere in the app — no Provider needed (react-hot-toast
 * uses a global store; <Toaster /> is already mounted in layout.tsx).
 */
import toast from "react-hot-toast";

export interface ToastAPI {
  success: (message: string) => void;
  error:   (message: string) => void;
  info:    (message: string) => void;
  loading: (message: string) => string;
  dismiss: (id?: string)    => void;
}

export function useToast(): ToastAPI {
  return {
    success(message) {
      toast.success(message, {
        style: {
          background:   "var(--surface)",
          color:        "var(--text)",
          border:       "1px solid var(--border)",
          borderRadius: "var(--radius-md)",
        },
        iconTheme: { primary: "var(--green)", secondary: "var(--surface)" },
      });
    },

    error(message) {
      toast.error(message, {
        style: {
          background:   "var(--surface)",
          color:        "var(--text)",
          border:       "1px solid rgba(220,53,69,0.4)",
          borderRadius: "var(--radius-md)",
        },
        iconTheme: { primary: "var(--red)", secondary: "var(--surface)" },
        duration: 5000,
      });
    },

    info(message) {
      toast(message, {
        icon: "•",
        style: {
          background:   "var(--surface)",
          color:        "var(--text)",
          border:       "1px solid var(--border)",
          borderRadius: "var(--radius-md)",
        },
      });
    },

    loading(message) {
      return toast.loading(message, {
        style: {
          background:   "var(--surface)",
          color:        "var(--muted)",
          border:       "1px solid var(--border)",
          borderRadius: "var(--radius-md)",
        },
      });
    },

    dismiss(id) {
      toast.dismiss(id);
    },
  };
}
