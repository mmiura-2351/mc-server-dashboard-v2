/**
 * The keep-all retention warning (issue #4084), shown on both the Backups and
 * Schedules tabs (WEBUI_SPEC.md 6.7, 6.13).
 *
 * A server with no retention policy (`backup_retention` null) keeps every
 * scheduled backup, so an enabled backup schedule there grows without bound
 * until the disk fills. A disabled schedule takes no backups and any policy
 * bounds them, so neither warns.
 */

import type { ReactNode } from "react";
import type { components } from "../api/schema";
import { t } from "../i18n/index.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type ScheduleResponse = components["schemas"]["ScheduleResponse"];

export function backupsGrowUnbounded(
  schedules: readonly ScheduleResponse[],
  retention: ServerResponse["backup_retention"],
): boolean {
  return (
    retention == null &&
    schedules.some(
      (schedule) => schedule.action === "backup" && schedule.enabled,
    )
  );
}

/** The warning text, followed by each tab's way to the retention editor. */
export function UnboundedBackupsWarning({
  children,
}: {
  children?: ReactNode;
}) {
  return (
    <div className="unbounded-backups-warning">
      <span>{t("backups.retention.unboundedWarning")}</span>
      {children}
    </div>
  );
}
