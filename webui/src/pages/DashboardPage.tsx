import { useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";

// Dashboard placeholder. Beyond the routed page it demonstrates the UX
// primitives (toast, modal, typed-confirm) wired to real buttons so the shell
// exercises them end-to-end (WEBUI_SPEC.md Section 7.4). No data fetching yet.
export function DashboardPage() {
  const { showToast } = useToast();
  const [modalOpen, setModalOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <div className="page-head">
        <h1>{t("page.dashboard")}</h1>
        <div className="actions">
          <button
            type="button"
            className="btn"
            onClick={() => showToast(t("demo.toastSuccess"), "success")}
          >
            {t("demo.showToast")}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => showToast(t("demo.toastError"), "error")}
          >
            {t("demo.showError")}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setModalOpen(true)}
          >
            {t("demo.openModal")}
          </button>
          <button
            type="button"
            className="btn danger"
            onClick={() => setConfirmOpen(true)}
          >
            {t("demo.deleteServer")}
          </button>
        </div>
      </div>
      <p className="sub">{t("page.placeholder")}</p>

      <Modal
        open={modalOpen}
        title={t("demo.modalTitle")}
        onClose={() => setModalOpen(false)}
        footer={
          <button
            type="button"
            className="btn"
            onClick={() => setModalOpen(false)}
          >
            {t("common.close")}
          </button>
        }
      >
        <p>{t("demo.modalBody")}</p>
      </Modal>

      <ConfirmDialog
        open={confirmOpen}
        title={t("demo.confirmTitle")}
        body={t("demo.confirmBody")}
        confirmPhrase={t("demo.confirmServerName")}
        confirmLabel={t("demo.deleteServer")}
        promptLabel={t("demo.confirmPrompt")}
        onConfirm={() => {
          setConfirmOpen(false);
          showToast(t("demo.deleted"), "success");
        }}
        onClose={() => setConfirmOpen(false)}
      />
    </>
  );
}
