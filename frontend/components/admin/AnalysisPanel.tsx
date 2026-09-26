import type { CallAttemptAnalysis } from "@/lib/admin-types";
import { StatusDot } from "./StatusDot";
import { EmptyState } from "./States";
import styles from "./AnalysisPanel.module.css";

/** §16, §19-20: keeps analysis (what the system inferred) visually and
 * structurally separate from the transcript (what was said) -- never
 * mixed into the same viewer. Lead score is shown as a bounded
 * priority signal, never labeled "guaranteed" or "certain" (§19-20). */
export function AnalysisPanel({ analysis }: { analysis: CallAttemptAnalysis | null }) {
  if (analysis === null) {
    return <EmptyState title="No analysis for this call" hint="Analysis may still be pending." />;
  }

  if (analysis.status !== "completed") {
    return (
      <div className={styles.pending}>
        <StatusDot label={analysis.status} />
        <span className={styles.pendingHint}>
          {analysis.status === "failed"
            ? "Analysis failed for this call."
            : "Analysis is still processing."}
        </span>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {analysis.summary && <p className={styles.summary}>{analysis.summary}</p>}

      <div className={styles.grid}>
        <Field label="Intent" value={analysis.intent} />
        <Field label="Interest" value={analysis.interest_status} />
        <Field label="Sentiment" value={analysis.sentiment} />
        <Field label="Next action" value={analysis.next_action} />
        <Field label="Language" value={analysis.language} />
        <div className={styles.field}>
          <span className={styles.fieldLabel}>Lead score</span>
          <span className={styles.leadScore}>
            {analysis.lead_score ?? "—"}
            <span className={styles.leadScoreMax}>/100</span>
          </span>
        </div>
      </div>

      {analysis.feedback && (
        <div className={styles.block}>
          <span className={styles.fieldLabel}>Feedback</span>
          <p>{analysis.feedback}</p>
        </div>
      )}

      <ListBlock label="Key facts" items={analysis.key_facts} />
      <ListBlock label="Objections" items={analysis.objections} />
      <ListBlock label="Customer needs" items={analysis.customer_needs} />

      {analysis.analysis_version && (
        <p className={styles.version}>Analysis version {analysis.analysis_version}</p>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <StatusDot label={value} />
    </div>
  );
}

function ListBlock({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className={styles.block}>
      <span className={styles.fieldLabel}>{label}</span>
      <ul className={styles.list}>
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
