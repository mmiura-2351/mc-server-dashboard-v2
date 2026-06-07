import { t } from "../i18n/index.ts";

// Styled file picker for selecting a file into form state (issue #539). The
// bare browser file input renders unstyled, so it is visually hidden and driven
// through a design-token button, with the chosen filename shown alongside. The
// input keeps its `id` so an external <label htmlFor> still names it, and it
// stays in the tab order — label association and keyboard operability are both
// preserved.
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
