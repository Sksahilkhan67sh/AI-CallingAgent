import styles from "./Pagination.module.css";

/** §30, §32: server-side pagination, offset/limit persisted in the
 * URL by the caller (via useSearchParams/router.push) so refresh and
 * back/forward both work. */
export function Pagination({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (nextOffset: number) => void;
}) {
  const currentPage = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className={styles.wrapper}>
      <span className={styles.summary}>
        {total === 0
          ? "0 results"
          : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`}
      </span>
      <div className={styles.controls}>
        <button
          type="button"
          className={styles.button}
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <span className={styles.pageLabel}>
          Page {currentPage} of {totalPages}
        </span>
        <button
          type="button"
          className={styles.button}
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
