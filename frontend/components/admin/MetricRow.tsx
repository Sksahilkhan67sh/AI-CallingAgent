import styles from "./MetricRow.module.css";

export interface Metric {
  label: string;
  value: string;
  tone?: "neutral" | "success" | "warning" | "danger";
}

/** Design direction §4: sections and grouped numbers, not a floating
 * card per metric. */
export function MetricRow({ metrics }: { metrics: Metric[] }) {
  return (
    <div className={styles.row}>
      {metrics.map((m) => (
        <div className={styles.metric} key={m.label}>
          <span className={styles.value} data-tone={m.tone ?? "neutral"}>
            {m.value}
          </span>
          <span className={styles.label}>{m.label}</span>
        </div>
      ))}
    </div>
  );
}
