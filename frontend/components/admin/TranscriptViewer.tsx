import type { TranscriptLine } from "@/lib/admin-types";
import { EmptyState } from "./States";
import styles from "./TranscriptViewer.module.css";

const SPEAKER_LABEL: Record<TranscriptLine["role"], string> = {
  agent: "AI",
  contact: "Customer",
  system: "System",
};

/** §17: chronological, speaker-labeled, timestamped, read-only. This
 * is the original transcript as recorded -- never edited or
 * "corrected" by the frontend (§17). */
export function TranscriptViewer({ lines }: { lines: TranscriptLine[] }) {
  if (lines.length === 0) {
    return <EmptyState title="No transcript captured for this call" />;
  }

  return (
    <div className={styles.transcript}>
      {lines.map((line, i) => (
        <div key={i} className={styles.line} data-role={line.role}>
          <div className={styles.meta}>
            <span className={styles.speaker}>{SPEAKER_LABEL[line.role]}</span>
            <span className={styles.time}>
              {new Date(line.created_at).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })}
            </span>
          </div>
          <p className={styles.content}>{line.content}</p>
        </div>
      ))}
    </div>
  );
}
