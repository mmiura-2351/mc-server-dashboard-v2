import { t } from "../i18n/index.ts";

// Keep the hidden input labelled and keyboard-focusable while styling its picker separately.
interface FilePickerProps {
  id: string;
  accept?: string;
  file: File | null;
  onSelect: (file: File | null) => void;
}

export function FilePicker({ id, accept, file, onSelect }: FilePickerProps) {
  return (
    <div className="file-picker">
      <input
        id={id}
        type="file"
        className="file-picker-input"
        accept={accept}
        onChange={(e) => onSelect(e.target.files?.[0] ?? null)}
      />
      <label className="btn" htmlFor={id}>
        {t("common.chooseFile")}
      </label>
      <span className="file-picker-name">
        {file?.name ?? t("common.noFileChosen")}
      </span>
    </div>
  );
}
