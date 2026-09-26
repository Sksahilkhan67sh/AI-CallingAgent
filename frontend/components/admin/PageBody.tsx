import { TopBar } from "./TopBar";
import styles from "./PageBody.module.css";

export function PageBody({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <TopBar title={title} />
      <div className={styles.body}>{children}</div>
    </>
  );
}
