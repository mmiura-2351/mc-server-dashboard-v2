import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { fieldErrorsFromValidation } from "../api/validationErrors.ts";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { useCanCode } from "../permissions/useCan.ts";
import { dashboardPath } from "../routes.ts";

// Server create wizard (WEBUI_SPEC.md 6.3). Three steps for a fresh server
// (type & version → runtime → config & EULA) plus an "Import ZIP" tab that
// uploads a prior export. The whole page is gated on `server:create`; the
// server stays authoritative (any 403/422/409 is surfaced honestly).

// The catalogued types `GET /versions` can resolve (vanilla/paper/fabric/forge).
// Spigot is intentionally not catalogued — it is shown as a disabled card with a
// "use Paper" hint (the create/import endpoints 422 `spigot_unsupported`).
type CatalogType = "vanilla" | "paper" | "fabric" | "forge";
const SPIGOT = "spigot";

const TYPE_LABEL: Record<string, TranslationKey> = {
  vanilla: "serverCreate.type.vanilla",
  paper: "serverCreate.type.paper",
  fabric: "serverCreate.type.fabric",
  forge: "serverCreate.type.forge",
  spigot: "serverCreate.type.spigot",
};
const TYPE_SUB: Record<string, TranslationKey> = {
  vanilla: "serverCreate.typeSub.vanilla",
  paper: "serverCreate.typeSub.paper",
  fabric: "serverCreate.typeSub.fabric",
  forge: "serverCreate.typeSub.forge",
  spigot: "serverCreate.typeSub.spigot",
};

type Backend = "host_process" | "container";
const BACKENDS: Backend[] = ["host_process", "container"];

// Create-path problem reasons that map to a specific inline/toast message. A
// 409 `port_taken` is surfaced specifically (issue requirement); everything else
// falls back to the generic toast.
const CREATE_ERROR_KEY: Record<string, TranslationKey> = {
  spigot_unsupported: "serverCreate.error.spigot_unsupported",
  port_taken: "serverCreate.error.port_taken",
  port_out_of_range: "serverCreate.error.port_out_of_range",
  server_name_exists: "serverCreate.error.server_name_exists",
  invalid_server_name: "serverCreate.error.invalid_server_name",
  unknown_version: "serverCreate.error.unknown_version",
};

export function ServerCreatePage() {
  const { communityId } = useActiveCommunity();
  const canCreate = useCanCode("server:create");

  if (!canCreate) {
    return (
      <Chrome>
        <p className="field-error">{t("serverCreate.denied")}</p>
      </Chrome>
    );
  }
  if (communityId === null) {
    return (
      <Chrome>
        <p className="sub">{t("shell.noCommunities")}</p>
      </Chrome>
    );
  }
  return <Wizard communityId={communityId} />;
}

function Chrome({ children }: { children: React.ReactNode }) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("page.serverCreate")}</h1>
          <div className="sub">{t("serverCreate.subtitle")}</div>
        </div>
      </div>
      {children}
    </>
  );
}

function Wizard({ communityId }: { communityId: string }) {
  const [tab, setTab] = useState<"new" | "import">("new");
  return (
    <Chrome>
      <div className="tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "new"}
          className={`tab${tab === "new" ? " active" : ""}`}
          onClick={() => setTab("new")}
        >
          {t("serverCreate.tab.new")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "import"}
          className={`tab${tab === "import" ? " active" : ""}`}
          onClick={() => setTab("import")}
        >
          {t("serverCreate.tab.import")}
        </button>
      </div>
      {tab === "new" ? (
        <NewServerWizard communityId={communityId} />
      ) : (
        <ImportForm communityId={communityId} />
      )}
    </Chrome>
  );
}

// ---------------------------------------------------------------------------
// New-server wizard (3 steps)
// ---------------------------------------------------------------------------

interface PropOverride {
  key: string;
  value: string;
}

function NewServerWizard({ communityId }: { communityId: string }) {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [step, setStep] = useState(1);
  const [type, setType] = useState<CatalogType | null>(null);
  const [version, setVersion] = useState("");
  const [backend, setBackend] = useState<Backend>("host_process");
  const [port, setPort] = useState("");
  // Once the user edits the port, the auto-suggest must never overwrite it.
  const [portTouched, setPortTouched] = useState(false);
  const [name, setName] = useState("");
  const [props, setProps] = useState<PropOverride[]>([]);
  const [acceptEula, setAcceptEula] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>();

  const typesQuery = useQuery({
    queryKey: ["versions"],
    queryFn: () => api.get("/api/versions"),
  });
  const catalogTypes = typesQuery.data?.server_types ?? [];

  const versionsQuery = useQuery({
    queryKey: ["versions", type],
    queryFn: () =>
      api.get(
        apiPath("/api/versions/{server_type}", { server_type: type as string }),
      ),
    enabled: type !== null,
  });
  const versions = versionsQuery.data?.versions ?? [];

  // Preselect the latest version (catalogs list newest first) whenever the list
  // for the selected type loads or changes.
  useEffect(() => {
    if (versions.length > 0) {
      setVersion(versions[0]);
    }
  }, [versions]);

  const portCheck = usePortCheck(port);

  // On reaching the runtime step, prefill the game port from the next free port
  // (GET /ports/available, SPEC 6.3) unless the user has already typed one. A
  // failed suggest leaves the field empty — the user can still type a port.
  useEffect(() => {
    if (step !== 2 || portTouched) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const result = await api.get("/api/ports/available");
        const next = result.ports?.[0];
        if (!cancelled && !portTouched && typeof next === "number") {
          setPort(String(next));
        }
      } catch {
        // Leave the field empty; the user types a port and the on-blur check
        // still validates it.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [step, portTouched]);

  async function onCreate() {
    setSubmitting(true);
    setNameError(undefined);
    const config: Record<string, string> = {};
    for (const { key, value } of props) {
      if (key.trim() !== "") {
        config[key.trim()] = value;
      }
    }
    try {
      const server = await api.post(
        apiPath("/api/communities/{community_id}/servers", {
          community_id: communityId,
        }),
        {
          body: JSON.stringify({
            name,
            mc_edition: "java",
            mc_version: version,
            server_type: type,
            execution_backend: backend,
            config,
            accept_eula: acceptEula,
            game_port: port === "" ? null : Number(port),
          }),
        },
      );
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      if (!handleCreateError(err, showToast, setNameError)) {
        showToast(t("serverCreate.genericError"), "error");
      }
      setSubmitting(false);
    }
  }

  return (
    <>
      <StepRail step={step} />

      {step === 1 && (
        <div className="card">
          <h2>{t("serverCreate.typeHeading")}</h2>
          {typesQuery.isPending ? (
            <p className="sub">{t("serverCreate.typeLoading")}</p>
          ) : typesQuery.isError ? (
            <p className="field-error">{t("serverCreate.typeLoadError")}</p>
          ) : (
            <>
              <div className="type-cards">
                {catalogTypes.map((typeOption) => (
                  <button
                    key={typeOption}
                    type="button"
                    className={`type-card${type === typeOption ? " selected" : ""}`}
                    onClick={() => {
                      setType(typeOption as CatalogType);
                      setVersion("");
                    }}
                  >
                    <div className="t-name">{t(TYPE_LABEL[typeOption])}</div>
                    <div className="t-sub">{t(TYPE_SUB[typeOption])}</div>
                  </button>
                ))}
                <button
                  type="button"
                  className="type-card disabled"
                  disabled
                  title={t("serverCreate.spigotHint")}
                >
                  <div className="t-name">{t(TYPE_LABEL[SPIGOT])}</div>
                  <div className="t-sub">{t("serverCreate.spigotHint")}</div>
                </button>
              </div>
              {type !== null && (
                <div className="field">
                  <label htmlFor="version-select">
                    {t("serverCreate.versionLabel")}
                  </label>
                  {versionsQuery.isPending ? (
                    <p className="sub">{t("serverCreate.versionLoading")}</p>
                  ) : versionsQuery.isError ? (
                    <p className="field-error">
                      {t("serverCreate.versionLoadError")}
                    </p>
                  ) : (
                    <select
                      id="version-select"
                      value={version}
                      onChange={(e) => setVersion(e.target.value)}
                    >
                      {versions.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              )}
            </>
          )}
          <div className="wizard-foot">
            <button
              type="button"
              className="btn primary"
              disabled={type === null || version === ""}
              onClick={() => setStep(2)}
            >
              {t("serverCreate.next")}
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <div className="field">
            <label htmlFor="backend-select">
              {t("serverCreate.backendLabel")}
            </label>
            <select
              id="backend-select"
              value={backend}
              onChange={(e) => setBackend(e.target.value as Backend)}
            >
              {BACKENDS.map((b) => (
                <option key={b} value={b}>
                  {t(`serverCreate.backend.${b}` as TranslationKey)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="port-input">{t("serverCreate.portLabel")}</label>
            <input
              id="port-input"
              type="number"
              value={port}
              onChange={(e) => {
                setPortTouched(true);
                setPort(e.target.value);
              }}
              onBlur={() => portCheck.check()}
            />
            <PortFeedback state={portCheck.state} />
          </div>
          <div className="wizard-foot">
            <button
              type="button"
              className="btn ghost"
              onClick={() => setStep(1)}
            >
              {t("serverCreate.back")}
            </button>
            <button
              type="button"
              className="btn primary"
              onClick={() => setStep(3)}
            >
              {t("serverCreate.next")}
            </button>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="card">
          <div className="field">
            <label htmlFor="name-input">{t("serverCreate.nameLabel")}</label>
            <input
              id="name-input"
              type="text"
              value={name}
              placeholder={t("serverCreate.namePlaceholder")}
              onChange={(e) => setName(e.target.value)}
            />
            {nameError !== undefined && (
              <div className="error" role="alert">
                {nameError}
              </div>
            )}
          </div>

          <PropsEditor props={props} onChange={setProps} />

          <div className="field">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={acceptEula}
                onChange={(e) => setAcceptEula(e.target.checked)}
              />
              {t("serverCreate.eulaLabel")}
            </label>
            {!acceptEula && (
              <div className="hint warn" role="status">
                {t("serverCreate.eulaWarning")}
              </div>
            )}
          </div>

          <div className="wizard-foot">
            <button
              type="button"
              className="btn ghost"
              onClick={() => setStep(2)}
            >
              {t("serverCreate.back")}
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={submitting || name.trim() === ""}
              onClick={onCreate}
            >
              {submitting
                ? t("serverCreate.creating")
                : t("serverCreate.create")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function StepRail({ step }: { step: number }) {
  const steps: TranslationKey[] = [
    "serverCreate.step.type",
    "serverCreate.step.runtime",
    "serverCreate.step.config",
  ];
  return (
    <div className="wizard-steps">
      {steps.map((key, i) => {
        const n = i + 1;
        const cls = n === step ? "active" : n < step ? "done" : "";
        return (
          <div key={key} className={`step ${cls}`.trim()}>
            {n} · {t(key)}
          </div>
        );
      })}
    </div>
  );
}

function PropsEditor({
  props,
  onChange,
}: {
  props: PropOverride[];
  onChange: (next: PropOverride[]) => void;
}) {
  return (
    <div className="field">
      {/* A group heading for the override rows, not a control label. */}
      <span className="group-label">{t("serverCreate.propsHeading")}</span>
      <div className="hint">{t("serverCreate.propsHint")}</div>
      {props.map((row, i) => (
        // The list is positional and user-built; the index is a stable enough key.
        // biome-ignore lint/suspicious/noArrayIndexKey: positional override rows
        <div key={i} className="prop-row">
          <input
            type="text"
            aria-label={t("serverCreate.propKeyPlaceholder")}
            placeholder={t("serverCreate.propKeyPlaceholder")}
            value={row.key}
            onChange={(e) =>
              onChange(
                props.map((p, j) =>
                  j === i ? { ...p, key: e.target.value } : p,
                ),
              )
            }
          />
          <input
            type="text"
            aria-label={t("serverCreate.propValuePlaceholder")}
            placeholder={t("serverCreate.propValuePlaceholder")}
            value={row.value}
            onChange={(e) =>
              onChange(
                props.map((p, j) =>
                  j === i ? { ...p, value: e.target.value } : p,
                ),
              )
            }
          />
          <button
            type="button"
            className="btn sm ghost"
            onClick={() => onChange(props.filter((_, j) => j !== i))}
          >
            {t("serverCreate.propRemove")}
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn sm"
        onClick={() => onChange([...props, { key: "", value: "" }])}
      >
        {t("serverCreate.propAdd")}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Port availability check (GET /ports/check/{port} on blur)
// ---------------------------------------------------------------------------

type PortState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "available" }
  | { kind: "taken" }
  | { kind: "out_of_range" }
  | { kind: "error" };

function usePortCheck(port: string) {
  const [state, setState] = useState<PortState>({ kind: "idle" });

  async function check() {
    if (port === "") {
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "checking" });
    try {
      const result = await api.get(
        apiPath("/api/ports/check/{port}", { port }),
      );
      if (result.in_range === false) {
        setState({ kind: "out_of_range" });
      } else if (result.available === true) {
        setState({ kind: "available" });
      } else {
        setState({ kind: "taken" });
      }
    } catch {
      setState({ kind: "error" });
    }
  }

  return { state, check };
}

function PortFeedback({ state }: { state: PortState }) {
  if (state.kind === "idle") {
    return <div className="hint">{t("serverCreate.portHint")}</div>;
  }
  if (state.kind === "checking") {
    return <div className="hint">{t("serverCreate.portChecking")}</div>;
  }
  if (state.kind === "available") {
    return (
      <div className="hint ok" role="status">
        {t("serverCreate.portAvailable")}
      </div>
    );
  }
  const key: TranslationKey =
    state.kind === "taken"
      ? "serverCreate.portTaken"
      : state.kind === "out_of_range"
        ? "serverCreate.portOutOfRange"
        : "serverCreate.portCheckFailed";
  return (
    <div className="error" role="alert">
      {t(key)}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Import tab (POST .../servers/import, multipart)
// ---------------------------------------------------------------------------

function ImportForm({ communityId }: { communityId: string }) {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [name, setName] = useState("");
  const [backend, setBackend] = useState<Backend>("host_process");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>();

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (file === null) {
      showToast(t("serverCreate.import.noFile"), "error");
      return;
    }
    setSubmitting(true);
    setNameError(undefined);
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    form.append("execution_backend", backend);
    try {
      const server = await api.postForm(
        apiPath("/api/communities/{community_id}/servers/import", {
          community_id: communityId,
        }),
        form,
      );
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      if (!handleImportError(err, showToast, setNameError)) {
        showToast(t("serverCreate.genericError"), "error");
      }
      setSubmitting(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit}>
      <h2>{t("serverCreate.import.heading")}</h2>
      <p className="hint">{t("serverCreate.import.hint")}</p>
      <div className="field">
        <label htmlFor="import-name">{t("serverCreate.nameLabel")}</label>
        <input
          id="import-name"
          type="text"
          value={name}
          placeholder={t("serverCreate.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
          required
        />
        {nameError !== undefined && (
          <div className="error" role="alert">
            {nameError}
          </div>
        )}
      </div>
      <div className="field">
        <label htmlFor="import-backend">{t("serverCreate.backendLabel")}</label>
        <select
          id="import-backend"
          value={backend}
          onChange={(e) => setBackend(e.target.value as Backend)}
        >
          {BACKENDS.map((b) => (
            <option key={b} value={b}>
              {t(`serverCreate.backend.${b}` as TranslationKey)}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="import-file">
          {t("serverCreate.import.fileLabel")}
        </label>
        <input
          id="import-file"
          type="file"
          accept=".zip"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </div>
      <div className="wizard-foot">
        <button
          type="submit"
          className="btn primary"
          disabled={submitting || name.trim() === ""}
        >
          {submitting
            ? t("serverCreate.import.importing")
            : t("serverCreate.import.submit")}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Shared error surfacing
// ---------------------------------------------------------------------------

// Surface a create failure. A mapped reason (incl. the 409 `port_taken`) becomes
// a specific toast; `invalid_server_name` and a structural validation_error on
// `name` go inline against the name field. Returns whether it was handled.
function handleCreateError(
  err: unknown,
  showToast: (m: string, k: "error") => void,
  setNameError: (m: string) => void,
): boolean {
  if (!(err instanceof ApiError)) {
    return false;
  }
  if (err.reason !== undefined) {
    if (err.reason === "invalid_server_name") {
      setNameError(t("serverCreate.error.invalid_server_name"));
      return true;
    }
    const key = CREATE_ERROR_KEY[err.reason];
    if (key !== undefined) {
      showToast(t(key), "error");
      return true;
    }
    if (err.reason === "validation_error") {
      const fields = fieldErrorsFromValidation(err.body, ["name"]);
      if (fields?.name !== undefined) {
        setNameError(fields.name);
        return true;
      }
    }
  }
  return false;
}

// Import-specific surfacing: a bad archive / oversize upload, plus the create
// reasons it shares (spigot, name conflict, …).
function handleImportError(
  err: unknown,
  showToast: (m: string, k: "error") => void,
  setNameError: (m: string) => void,
): boolean {
  if (!(err instanceof ApiError)) {
    return false;
  }
  if (err.status === 413) {
    showToast(t("serverCreate.import.tooLarge"), "error");
    return true;
  }
  if (err.reason === "invalid_export_metadata") {
    showToast(t("serverCreate.import.error.invalid_export_metadata"), "error");
    return true;
  }
  return handleCreateError(err, showToast, setNameError);
}
