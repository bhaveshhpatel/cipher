/**
 * UI Primitives — snapshot + behaviour tests.
 * Covers: Badge, Button, Card/CardHeader/CardBody, Spinner,
 *         Skeleton/SkeletonBlock, TierBadge, VerdictBadge,
 *         MarketStatusChip, Tooltip, EmptyState.
 */
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import {
  Badge,
  Button,
  Card, CardHeader, CardBody,
  Spinner,
  Skeleton, SkeletonBlock,
  TierBadge,
  VerdictBadge,
  MarketStatusChip,
  Tooltip,
  EmptyState,
} from "@/components/ui";

// ── Badge ────────────────────────────────────────────────
describe("Badge", () => {
  it("renders children", () => {
    render(<Badge>Hello</Badge>);
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("applies variant class", () => {
    const { container } = render(<Badge variant="red">X</Badge>);
    expect(container.firstChild).toHaveClass("badge-red");
  });

  it("applies size class sm", () => {
    const { container } = render(<Badge size="sm">X</Badge>);
    expect(container.firstChild).toHaveClass("text-2xs");
  });

  it("renders dot when dot=true", () => {
    const { container } = render(<Badge dot>X</Badge>);
    expect(container.querySelector("span > span")).toBeInTheDocument();
  });

  it("renders all tier variants without error", () => {
    (["tier-1", "tier-2", "tier-3"] as const).forEach(v => {
      const { unmount } = render(<Badge variant={v}>{v}</Badge>);
      expect(screen.getByText(v)).toBeInTheDocument();
      unmount();
    });
  });

  it("renders all verdict variants without error", () => {
    (["buy", "sell", "hold"] as const).forEach(v => {
      const { unmount } = render(<Badge variant={v}>{v}</Badge>);
      expect(screen.getByText(v)).toBeInTheDocument();
      unmount();
    });
  });
});

// ── Button ───────────────────────────────────────────────
describe("Button", () => {
  it("renders children", () => {
    render(<Button>Click me</Button>);
    expect(screen.getByRole("button", { name: "Click me" })).toBeInTheDocument();
  });

  it("is disabled when disabled prop is true", () => {
    render(<Button disabled>X</Button>);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("is disabled and shows spinner when loading=true", () => {
    render(<Button loading>Save</Button>);
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
    expect(btn.querySelector("[role=status]")).toBeInTheDocument();
  });

  it("calls onClick when not disabled", () => {
    const fn = jest.fn();
    render(<Button onClick={fn}>Go</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("does not call onClick when disabled", () => {
    const fn = jest.fn();
    render(<Button disabled onClick={fn}>Go</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(fn).not.toHaveBeenCalled();
  });

  it("renders iconLeft", () => {
    render(<Button iconLeft={<span data-testid="icon-l" />}>X</Button>);
    expect(screen.getByTestId("icon-l")).toBeInTheDocument();
  });

  it("renders iconRight", () => {
    render(<Button iconRight={<span data-testid="icon-r" />}>X</Button>);
    expect(screen.getByTestId("icon-r")).toBeInTheDocument();
  });

  it("applies destructive variant class", () => {
    const { container } = render(<Button variant="destructive">Del</Button>);
    expect(container.firstChild).toHaveClass("btn-destructive");
  });

  it("applies sm size class", () => {
    const { container } = render(<Button size="sm">X</Button>);
    expect(container.firstChild).toHaveClass("h-7");
  });

  it("applies lg size class", () => {
    const { container } = render(<Button size="lg">X</Button>);
    expect(container.firstChild).toHaveClass("h-11");
  });
});

// ── Card ─────────────────────────────────────────────────
describe("Card", () => {
  it("renders children", () => {
    render(<Card>body</Card>);
    expect(screen.getByText("body")).toBeInTheDocument();
  });

  it("has padding by default", () => {
    const { container } = render(<Card>x</Card>);
    expect(container.firstChild).toHaveClass("p-4");
  });

  it("omits padding when noPadding=true", () => {
    const { container } = render(<Card noPadding>x</Card>);
    expect(container.firstChild).not.toHaveClass("p-4");
  });
});

describe("CardHeader", () => {
  it("renders title", () => {
    render(<CardHeader title="My Card" />);
    expect(screen.getByText("My Card")).toBeInTheDocument();
  });

  it("renders subtitle when provided", () => {
    render(<CardHeader title="T" subtitle="Sub" />);
    expect(screen.getByText("Sub")).toBeInTheDocument();
  });

  it("renders action slot", () => {
    render(<CardHeader title="T" action={<button>Edit</button>} />);
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });
});

describe("CardBody", () => {
  it("renders children", () => {
    render(<CardBody>content</CardBody>);
    expect(screen.getByText("content")).toBeInTheDocument();
  });
});

// ── Spinner ───────────────────────────────────────────────
describe("Spinner", () => {
  it("has role=status", () => {
    render(<Spinner />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("has default aria-label Loading", () => {
    render(<Spinner />);
    expect(screen.getByLabelText("Loading")).toBeInTheDocument();
  });

  it("accepts custom label", () => {
    render(<Spinner label="Fetching data" />);
    expect(screen.getByLabelText("Fetching data")).toBeInTheDocument();
  });

  it("applies size class", () => {
    const { container } = render(<Spinner size="lg" />);
    expect(container.firstChild).toHaveClass("w-7");
  });
});

// ── Skeleton ─────────────────────────────────────────────
describe("Skeleton", () => {
  it("is aria-hidden", () => {
    const { container } = render(<Skeleton />);
    expect(container.firstChild).toHaveAttribute("aria-hidden", "true");
  });

  it("applies width and height classes", () => {
    const { container } = render(<Skeleton width="w-32" height="h-8" />);
    expect(container.firstChild).toHaveClass("w-32", "h-8");
  });

  it("uses rounded-full when rounded=true", () => {
    const { container } = render(<Skeleton rounded />);
    expect(container.firstChild).toHaveClass("rounded-full");
  });
});

describe("SkeletonBlock", () => {
  it("renders 3 rows by default", () => {
    const { container } = render(<SkeletonBlock />);
    expect(container.querySelectorAll("[aria-hidden=true]")).toHaveLength(3);
  });

  it("renders N rows when specified", () => {
    const { container } = render(<SkeletonBlock rows={5} />);
    expect(container.querySelectorAll("[aria-hidden=true]")).toHaveLength(5);
  });
});

// ── TierBadge ────────────────────────────────────────────
describe("TierBadge", () => {
  it("renders T1 label for tier 1", () => {
    render(<TierBadge tier={1} />);
    expect(screen.getByText("T1")).toBeInTheDocument();
  });

  it("renders T2 label for tier 2", () => {
    render(<TierBadge tier={2} />);
    expect(screen.getByText("T2")).toBeInTheDocument();
  });

  it("renders T3 label for tier 3", () => {
    render(<TierBadge tier={3} />);
    expect(screen.getByText("T3")).toBeInTheDocument();
  });

  it("hides label when showLabel=false", () => {
    render(<TierBadge tier={1} showLabel={false} />);
    expect(screen.queryByText("T1")).not.toBeInTheDocument();
  });
});

// ── VerdictBadge ─────────────────────────────────────────
describe("VerdictBadge", () => {
  it("renders BUY", () => {
    render(<VerdictBadge verdict="BUY" />);
    expect(screen.getByText("BUY")).toBeInTheDocument();
  });

  it("renders SELL", () => {
    render(<VerdictBadge verdict="SELL" />);
    expect(screen.getByText("SELL")).toBeInTheDocument();
  });

  it("renders HOLD", () => {
    render(<VerdictBadge verdict="HOLD" />);
    expect(screen.getByText("HOLD")).toBeInTheDocument();
  });

  it("applies buy variant class", () => {
    const { container } = render(<VerdictBadge verdict="BUY" />);
    expect(container.querySelector(".badge-buy")).toBeInTheDocument();
  });
});

// ── MarketStatusChip ─────────────────────────────────────
describe("MarketStatusChip", () => {
  it("renders Market Open", () => {
    render(<MarketStatusChip status="open" />);
    expect(screen.getByText("Market Open")).toBeInTheDocument();
  });

  it("renders Market Closed", () => {
    render(<MarketStatusChip status="closed" />);
    expect(screen.getByText("Market Closed")).toBeInTheDocument();
  });

  it("renders Pre-Market", () => {
    render(<MarketStatusChip status="pre" />);
    expect(screen.getByText("Pre-Market")).toBeInTheDocument();
  });

  it("renders After-Hours", () => {
    render(<MarketStatusChip status="after" />);
    expect(screen.getByText("After-Hours")).toBeInTheDocument();
  });

  it("adds pulse-dot class for open status", () => {
    const { container } = render(<MarketStatusChip status="open" />);
    expect(container.querySelector(".pulse-dot")).toBeInTheDocument();
  });

  it("does not add pulse-dot for closed status", () => {
    const { container } = render(<MarketStatusChip status="closed" />);
    expect(container.querySelector(".pulse-dot")).not.toBeInTheDocument();
  });
});

// ── Tooltip ───────────────────────────────────────────────
describe("Tooltip", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("does not show tooltip initially", () => {
    render(
      <Tooltip content="Tip text">
        <button>Hover me</button>
      </Tooltip>,
    );
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("shows tooltip after delay on mouseenter", () => {
    render(
      <Tooltip content="Tip text" delay={300}>
        <button>Hover me</button>
      </Tooltip>,
    );
    fireEvent.mouseEnter(screen.getByText("Hover me").closest("span")!);
    act(() => jest.advanceTimersByTime(300));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Tip text");
  });

  it("hides tooltip on mouseleave", () => {
    render(
      <Tooltip content="Tip text" delay={0}>
        <button>Hover me</button>
      </Tooltip>,
    );
    const trigger = screen.getByText("Hover me").closest("span")!;
    fireEvent.mouseEnter(trigger);
    act(() => jest.advanceTimersByTime(0));
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.mouseLeave(trigger);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("sets aria-describedby on trigger when visible", () => {
    render(
      <Tooltip content="Tip text" delay={0}>
        <button>Hover me</button>
      </Tooltip>,
    );
    const trigger = screen.getByText("Hover me").closest("span")!;
    fireEvent.mouseEnter(trigger);
    act(() => jest.advanceTimersByTime(0));
    const tooltipId = screen.getByRole("tooltip").id;
    expect(trigger).toHaveAttribute("aria-describedby", tooltipId);
  });

  it("removes aria-describedby when hidden", () => {
    render(
      <Tooltip content="Tip text" delay={0}>
        <button>Hover me</button>
      </Tooltip>,
    );
    const trigger = screen.getByText("Hover me").closest("span")!;
    fireEvent.mouseEnter(trigger);
    act(() => jest.advanceTimersByTime(0));
    fireEvent.mouseLeave(trigger);
    expect(trigger).not.toHaveAttribute("aria-describedby");
  });
});

// ── EmptyState ────────────────────────────────────────────
describe("EmptyState", () => {
  it("renders title", () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(<EmptyState title="T" description="Some detail" />);
    expect(screen.getByText("Some detail")).toBeInTheDocument();
  });

  it("renders action slot", () => {
    render(<EmptyState title="T" action={<button>Retry</button>} />);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders icon", () => {
    render(<EmptyState title="T" icon={<span data-testid="icon">⊘</span>} />);
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("applies compact styles", () => {
    const { container } = render(<EmptyState title="T" compact />);
    expect(container.firstChild).toHaveClass("py-8");
  });

  it("applies full styles by default", () => {
    const { container } = render(<EmptyState title="T" />);
    expect(container.firstChild).toHaveClass("py-16");
  });
});
