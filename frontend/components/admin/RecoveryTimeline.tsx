import type { RecoveryEvent } from "@/lib/admin-types";
import { EmptyState } from "./States";
import styles from "./RecoveryTimeline.module.css";

/** §21: displays backend retry state only -- no retry logic lives
 * here. A call with no recovery events simply never disconnected. */
export function RecoveryTimeline({ events }: { events: RecoveryEvent[] }) {
  if (events.length === 0) {
    return <EmptyState title="No retries for this call" hint="The call did not disconnect." />;
  }

  return (
    <ol className={styles.timeline}>
      {events.map((event, i) => (
        <li key={i} className={styles.item}>
          <span className={styles.dot} aria-hidden="true" />
          <div>
            <p className={styles.type}>{event.event_type.replaceAll("_", " ").toLowerCase()}</p>
            <p className={styles.time}>{new Date(event.occurred_at).toLocaleString()}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}
