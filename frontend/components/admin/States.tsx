import styles from "./States.module.css";

/** §16: skeletons that match the final layout, not a giant spinner. */
export function TableSkeleton({ rows = 6, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <div className={styles.skeletonTable} role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, r) => (
        <div className={styles.skeletonRow} key={r}>
          {Array.from({ length: columns }).map((_, c) => (
            <span
              className={styles.skeletonCell}
              key={c}
              style={{ width: c === 0 ? "60%" : "80%" }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/** §17: simple, no illustrations, no motivational copy. */
export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className={styles.empty}>
      <p className={styles.emptyTitle}>{title}</p>
      {hint && <p className={styles.emptyHint}>{hint}</p>}
    </div>
  );
}

/** §18, §27, §47: calm, no stack traces, a retry action. */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className={styles.error} role="alert">
      <p className={styles.errorTitle}>{message}</p>
      {onRetry && (
        <button className={styles.retryButton} onClick={onRetry} type="button">
          Retry
        </button>
      )}
    </div>
  );
}
