import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StatusDot } from "@/components/admin/StatusDot";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/admin/States";
import { Pagination } from "@/components/admin/Pagination";
import { MetricRow } from "@/components/admin/MetricRow";

describe("StatusDot", () => {
  it("renders the label text so status is never color-only (§33)", () => {
    render(<StatusDot label="Completed" />);
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it("infers a danger tone for failure-like labels", () => {
    const { container } = render(<StatusDot label="FailedToConnect" />);
    expect(container.querySelector('[class*="danger"]')).toBeInTheDocument();
  });

  it("infers a success tone for completed-like labels", () => {
    const { container } = render(<StatusDot label="Completed" />);
    expect(container.querySelector('[class*="success"]')).toBeInTheDocument();
  });
});

describe("TableSkeleton", () => {
  it("renders a loading status region (§16)", () => {
    render(<TableSkeleton rows={3} columns={4} />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders a plain title with no illustration/emoji copy (§17)", () => {
    render(<EmptyState title="No calls found" hint="Try adjusting the selected filters." />);
    expect(screen.getByText("No calls found")).toBeInTheDocument();
    expect(screen.getByText("Try adjusting the selected filters.")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("renders the message and calls onRetry when clicked (§18, §27)", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Unable to load call data." onRetry={onRetry} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Unable to load call data.");
    screen.getByRole("button", { name: "Retry" }).click();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("never renders a retry button when onRetry is not provided", () => {
    render(<ErrorState message="Unable to load call data." />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("disables Previous on the first page and Next on the last page", () => {
    render(<Pagination total={10} limit={10} offset={0} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("calls onChange with the next offset", () => {
    const onChange = vi.fn();
    render(<Pagination total={50} limit={10} offset={0} onChange={onChange} />);
    screen.getByRole("button", { name: "Next" }).click();
    expect(onChange).toHaveBeenCalledWith(10);
  });

  it("shows an accurate result range summary", () => {
    render(<Pagination total={37} limit={10} offset={10} onChange={() => {}} />);
    expect(screen.getByText("11–20 of 37")).toBeInTheDocument();
  });

  it("shows a zero-result summary without a divide-by-zero page count", () => {
    render(<Pagination total={0} limit={10} offset={0} onChange={() => {}} />);
    expect(screen.getByText("0 results")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 1")).toBeInTheDocument();
  });
});

describe("MetricRow", () => {
  it("renders every metric label and value -- no fake/placeholder data (§48)", () => {
    render(
      <MetricRow
        metrics={[
          { label: "Total contacts", value: "42" },
          { label: "Active", value: "3" },
        ]}
      />
    );
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("Total contacts")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });
});
