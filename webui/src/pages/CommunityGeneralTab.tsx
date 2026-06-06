import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { LANDING_PATH } from "../routes.ts";

type CommunityResponse = components["schemas"]["CommunityResponse"];

// Map a rename rejection to a specific message; otherwise the generic one.
function renameErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    if (error.reason === "name_taken") {
      return "communitySettings.general.nameTaken";
    }
    if (error.reason === "invalid_name") {
      return "communitySettings.general.invalidName";
    }
  }
  return "communitySettings.general.saveError";
}

// General tab (WEBUI_SPEC.md 6.10): rename (gated `community:update`) and delete
// (typed-confirm with the community name, gated `community:delete`). On delete
// the caller leaves the deleted community: the communities list is invalidated
// and the active community is cleared so a stale id never stays selected.
export function CommunityGeneralTab({
  community,
  can,
}: {
  community: CommunityResponse;
  can: Can;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { setCommunityId } = useActiveCommunity();

  const [name, setName] = useState(community.name);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const canUpdate = can("community:update");
  const canDelete = can("community:delete");

  const rename = useMutation({
    mutationFn: () =>
      api.patch(
        apiPath("/communities/{community_id}", { community_id: community.id }),
        { body: JSON.stringify({ name }) },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.general.saved"), "success");
      queryClient.invalidateQueries({ queryKey: ["communities"] });
    },
    onError: (error) => {
      if (onForbidden(error)) {
        return;
      }
      showToast(t(renameErrorMessage(error)), "error");
    },
  });

  const remove = useMutation({
    mutationFn: () =>
      api.delete(
        apiPath("/communities/{community_id}", { community_id: community.id }),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.general.deleted"), "success");
      // Clear the active community first so the landing never re-resolves to the
      // now-deleted id, then refresh the list and leave for the landing.
      setCommunityId(null);
      queryClient.invalidateQueries({ queryKey: ["communities"] });
      navigate(LANDING_PATH);
    },
    onError: (error) => {
      setConfirmOpen(false);
      if (onForbidden(error)) {
        return;
      }
      showToast(t("communitySettings.general.deleteError"), "error");
    },
  });

  return (
    <section className="settings">
      <div className="card form-card">
        <h2>{t("communitySettings.general.heading")}</h2>
        <label className="field">
          {t("communitySettings.general.nameLabel")}
          <input
            type="text"
            value={name}
            disabled={!canUpdate}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn primary"
          disabled={!canUpdate || rename.isPending || name.trim().length === 0}
          onClick={() => rename.mutate()}
        >
          {t("communitySettings.general.save")}
        </button>
      </div>

      {canDelete && (
        <div className="card danger-zone">
          <h2>{t("communitySettings.general.dangerHeading")}</h2>
          <div className="row">
            <div>
              <strong>{t("communitySettings.general.deleteTitle")}</strong>
              <div className="desc">
                {t("communitySettings.general.deleteDesc")}
              </div>
            </div>
            <button
              type="button"
              className="btn danger"
              onClick={() => setConfirmOpen(true)}
            >
              {t("communitySettings.general.deleteButton")}
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmOpen}
        title={t("communitySettings.general.deleteDialogTitle")}
        body={t("communitySettings.general.deleteDialogBody")}
        confirmPhrase={community.name}
        confirmLabel={t("communitySettings.general.deleteConfirm")}
        promptLabel={t("communitySettings.general.deletePrompt")}
        onConfirm={() => remove.mutate()}
        onClose={() => setConfirmOpen(false)}
      />
    </section>
  );
}
