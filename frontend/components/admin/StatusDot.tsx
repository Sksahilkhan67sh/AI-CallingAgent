import styles from "./StatusDot.module.css";

type Tone = "neutral" | "success" | "warning" | "danger" | "info";

const TONE_BY_KEYWORD: Record<string, Tone> = {
  active: "success",
  completed: "success",
  ok: "success",
  connected: "success",
  interested: "success",
  paused: "warning",
  retryscheduled: "warning",
  retry_scheduled: "warning",
  processing: "warning",
  maybe: "warning",
  pending: "neutral",
  draft: "neutral",
  dialing: "info",
  inconversation: "info",
  reconnecting: "info",
  failed: "danger",
  failedtoconnect: "danger",
  droppedmidcall: "danger",
  not_interested: "danger",
  notinterested: "danger",
  degraded: "danger",
  closed: "neutral",
  unknown: "neutral",
  disconnected: "warning",
};

function toneFor(label: string): Tone {
  const key = label.toLowerCase().replace(/[\s_-]/g, "");
  return TONE_BY_KEYWORD[key] ?? "neutral";
}

/**
 * Checkpoint 07 design direction §9: "small indicator + text" rather
 * than oversized colorful pills. The tone conveys status, but the text
 * label is what actually communicates it (§33: never color-only).
 */
export function StatusDot({ label, tone }: { label: string; tone?: Tone }) {
  const resolvedTone = tone ?? toneFor(label);
  return (
    <span className={styles.wrapper}>
      <span className={`${styles.dot} ${styles[resolvedTone]}`} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
