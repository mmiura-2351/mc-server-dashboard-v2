import { humanizeBytes } from "../format.ts";
import { t } from "../i18n/index.ts";

// Shared upload progress bar (issue #1207): a percentage-filled bar plus the
// transferred / total bytes and elapsed time, driven by `useUploadProgress`.
// Used by the four upload sites (resource packs, backups, files, server import)
// so the feedback is identical everywhere.
interface UploadProgressProps {
  loaded: number;
  total: number;
  percent: number;
  elapsedMs: number;
}

export function UploadProgress({
  loaded,
  total,
  percent,
  elapsedMs,
}: UploadProgressProps) {
  const seconds = Math.round(elapsedMs / 1000);
  const bytes = t("upload.bytes", {
    loaded: humanizeBytes(loaded),
    total: humanizeBytes(total),
  });

  return (
    <div className="upload-progress">
      <div
        className="upload-bar"
        role="progressbar"
        aria-label={t("upload.label")}
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="upload-bar-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="upload-progress-meta">
        <span>{t("upload.percent", { percent })}</span>
        {total > 0 && <span>{bytes}</span>}
        <span>{t("upload.elapsed", { seconds })}</span>
      </div>
    </div>
  );
}
