import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TranscriptViewer } from "@/components/admin/TranscriptViewer";
import { AnalysisPanel } from "@/components/admin/AnalysisPanel";
import type { CallAttemptAnalysis, TranscriptLine } from "@/lib/admin-types";

describe("TranscriptViewer", () => {
  it("renders lines in the given (chronological) order with speaker labels (§17)", () => {
    const lines: TranscriptLine[] = [
      { role: "agent", content: "Hello there", created_at: "2026-01-01T09:12:02Z" },
      { role: "contact", content: "Hi, yes", created_at: "2026-01-01T09:12:07Z" },
    ];
    render(<TranscriptViewer lines={lines} />);

    const rendered = screen.getAllByText(/Hello there|Hi, yes/);
    expect(rendered[0]).toHaveTextContent("Hello there");
    expect(rendered[1]).toHaveTextContent("Hi, yes");
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("Customer")).toBeInTheDocument();
  });

  it("shows an empty state instead of a blank viewer when there is no transcript", () => {
    render(<TranscriptViewer lines={[]} />);
    expect(screen.getByText("No transcript captured for this call")).toBeInTheDocument();
  });

  it("never rewrites the message content (§17)", () => {
    const lines: TranscriptLine[] = [
      {
        role: "contact",
        content: "  raw   spacing   preserved  ",
        created_at: "2026-01-01T09:00:00Z",
      },
    ];
    render(<TranscriptViewer lines={lines} />);
    // RTL/jest-dom's text matchers normalize whitespace for matching,
    // so assert against the raw DOM node content directly to prove
    // the component didn't collapse/trim it itself.
    const paragraph = document.querySelector("p");
    expect(paragraph?.textContent).toBe("  raw   spacing   preserved  ");
  });
});

const COMPLETED_ANALYSIS: CallAttemptAnalysis = {
  status: "completed",
  summary: "Customer was interested and asked for pricing.",
  intent: "interested",
  interest_status: "interested",
  sentiment: "positive",
  feedback: "Wants a callback next week.",
  next_action: "callback",
  key_facts: ["Has 5 employees"],
  objections: ["Price seems high"],
  customer_needs: ["Needs onboarding support"],
  language: "en",
  lead_score: 82,
  analysis_version: "v1",
};

describe("AnalysisPanel", () => {
  it("renders the lead score as a bounded /100 signal, not a guarantee (§19-20)", () => {
    render(<AnalysisPanel analysis={COMPLETED_ANALYSIS} />);
    expect(screen.getByText("82")).toBeInTheDocument();
    expect(screen.getByText("/100")).toBeInTheDocument();
    expect(screen.queryByText(/guaranteed/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/certain/i)).not.toBeInTheDocument();
  });

  it("renders summary, feedback, key facts, objections, and customer needs", () => {
    render(<AnalysisPanel analysis={COMPLETED_ANALYSIS} />);
    expect(
      screen.getByText("Customer was interested and asked for pricing.")
    ).toBeInTheDocument();
    expect(screen.getByText("Wants a callback next week.")).toBeInTheDocument();
    expect(screen.getByText("Has 5 employees")).toBeInTheDocument();
    expect(screen.getByText("Price seems high")).toBeInTheDocument();
    expect(screen.getByText("Needs onboarding support")).toBeInTheDocument();
  });

  it("shows an empty state when there is no analysis row at all", () => {
    render(<AnalysisPanel analysis={null} />);
    expect(screen.getByText("No analysis for this call")).toBeInTheDocument();
  });

  it("shows a pending indicator without fabricating fields when analysis is still processing", () => {
    render(
      <AnalysisPanel
        analysis={{ ...COMPLETED_ANALYSIS, status: "processing", summary: null, lead_score: null }}
      />
    );
    expect(screen.getByText("processing")).toBeInTheDocument();
    expect(screen.queryByText("82")).not.toBeInTheDocument();
  });
});
