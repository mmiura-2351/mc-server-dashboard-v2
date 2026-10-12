import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import {
  ApiError,
  api,
  isUploadAbortError,
  postFormWithProgress,
} from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { fieldErrorsFromValidation } from "../api/validationErrors.ts";
import { FilePicker } from "../components/FilePicker.tsx";
import { useToast } from "../components/Toast.tsx";
import { UploadProgress } from "../components/UploadProgress.tsx";
import { useUploadProgress } from "../components/useUploadProgress.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { useCanCode } from "../permissions/useCan.ts";
import { classifyQueryResult } from "../queryState.ts";
import { dashboardPath } from "../routes.ts";
import {
  serverCreateErrorPresentation,
  serverImportErrorPresentation,
} from "./serverCreateErrorPresentation.ts";
import { handleTabKeyDown, panelId, tabId, useTabHash } from "./urlState.ts";

type CatalogType = "vanilla" | "paper" | "fabric" | "forge";

const TYPE_LABEL: Record<string, TranslationKey> = {
  vanilla: "serverCreate.type.vanilla",
  paper: "serverCreate.type.paper",
  fabric: "serverCreate.type.fabric",
  forge: "serverCreate.type.forge",
};
const TYPE_SUB: Record<string, TranslationKey> = {
  vanilla: "serverCreate.typeSub.vanilla",
  paper: "serverCreate.typeSub.paper",
  fabric: "serverCreate.typeSub.fabric",
  forge: "serverCreate.typeSub.forge",
};

// Memory and CPU limits are reserved config keys; blank inputs omit them for default allocation.
const MEMORY_LIMIT_KEY = "memory_limit_mb";
const MEMORY_LIMIT_FLOOR_MIB = 512;
const MEMORY_LIMIT_DEFAULT_CEILING_MIB = 1024 * 1024;
const CPU_ALLOCATION_KEY = "cpu_millis";
const CPU_ALLOCATION_FLOOR_MILLIS = 100;
const CPU_ALLOCATION_CEILING_MILLIS = 128_000;

// Blank omits the key; nonblank limits must be whole numbers within the operator-configured range.
function memoryLimitValid(
  value: string,
  ceiling: number = MEMORY_LIMIT_DEFAULT_CEILING_MIB,
): boolean {
  if (value.trim() === "") {
    return true;
  }
  const parsed = Number(value);
  return (
    Number.isInteger(parsed) &&
    parsed >= MEMORY_LIMIT_FLOOR_MIB &&
    parsed <= ceiling
  );
}

function cpuAllocationValid(value: string): boolean {
  if (value.trim() === "") {
    return true;
  }
  const parsed = Number(value);
  return (
    Number.isInteger(parsed) &&
    parsed >= CPU_ALLOCATION_FLOOR_MILLIS &&
    parsed <= CPU_ALLOCATION_CEILING_MILLIS
  );
}

const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

function slugCreateValid(value: string): boolean {
  return value.trim() === "" || SLUG_RE.test(value.trim());
}

export function ServerCreatePage() {
  // Create in the URL community after verifying membership; the active community may differ on a stale link.
  const { cid } = useParams();
  const { communities } = useActiveCommunity();
  const canCreate = useCanCode("server:create");

  if (communities === undefined) {
    return (
      <Chrome>
        <p className="sub">{t("auth.loading")}</p>
      </Chrome>
    );
  }
  if (cid === undefined || !communities.some((c) => c.id === cid)) {
    return (
      <Chrome>
        <div className="empty">
          <div className="big">{t("community.notFound.title")}</div>
          <p className="sub">{t("community.notFound.body")}</p>
        </div>
      </Chrome>
    );
  }
  if (!canCreate) {
    return (
      <Chrome>
        <p className="field-error">{t("serverCreate.denied")}</p>
      </Chrome>
    );
  }
  return <Wizard communityId={cid} />;
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

// Keep top-level modes in URL history, but wizard steps in component state to preserve half-filled inputs.
const TABS = ["new", "import"] as const;

function Wizard({ communityId }: { communityId: string }) {
  const [tab, setTab] = useTabHash(TABS);
  return (
    <Chrome>
      <div className="tabs" role="tablist">
        <button
          id={tabId("sc", "new")}
          type="button"
          role="tab"
          aria-selected={tab === "new"}
          aria-controls={panelId("sc", "new")}
          tabIndex={tab === "new" ? 0 : -1}
          className={`tab${tab === "new" ? " active" : ""}`}
          onClick={() => setTab("new")}
          onKeyDown={(e) => handleTabKeyDown(e, TABS, tab, setTab, "sc")}
        >
          {t("serverCreate.tab.new")}
        </button>
        <button
          id={tabId("sc", "import")}
          type="button"
          role="tab"
          aria-selected={tab === "import"}
          aria-controls={panelId("sc", "import")}
          tabIndex={tab === "import" ? 0 : -1}
          className={`tab${tab === "import" ? " active" : ""}`}
          onClick={() => setTab("import")}
          onKeyDown={(e) => handleTabKeyDown(e, TABS, tab, setTab, "sc")}
        >
          {t("serverCreate.tab.import")}
        </button>
      </div>
      {tab === "new" ? (
        <div
          role="tabpanel"
          id={panelId("sc", "new")}
          aria-labelledby={tabId("sc", "new")}
        >
          <NewServerWizard communityId={communityId} />
        </div>
      ) : (
        <div
          role="tabpanel"
          id={panelId("sc", "import")}
          aria-labelledby={tabId("sc", "import")}
        >
          <ImportForm communityId={communityId} />
        </div>
      )}
    </Chrome>
  );
}

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
  const [port, setPort] = useState("");
  // Once edited, a port must not be overwritten by a late suggestion.
  const [portTouched, setPortTouched] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [props, setProps] = useState<PropOverride[]>([]);
  const [memoryLimit, setMemoryLimit] = useState("");
  const [cpuAllocation, setCpuAllocation] = useState("");
  const [acceptEula, setAcceptEula] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>();
  const [slugError, setSlugError] = useState<string | undefined>();

  const typesQuery = useQuery({
    queryKey: ["versions"],
    queryFn: ({ signal }) => api.get("/api/versions", { signal }),
  });
  const typesState = classifyQueryResult(typesQuery);
  const catalogTypes = typesQuery.data?.server_types ?? [];

  // Relay mode allocates ports in the API; direct mode exposes them for port forwarding.
  const metaQuery = useQuery({
    queryKey: ["meta"],
    queryFn: ({ signal }) => api.get("/api/meta", { signal }),
  });
  // Show the port control until metadata confirms relay mode, including metadata failures.
  const relayEnabled =
    metaQuery.isLoading || metaQuery.isError
      ? false
      : metaQuery.data?.relay_enabled === true;
  const defaultMemoryLimitMb: number | null =
    typeof metaQuery.data?.default_memory_limit_mb === "number"
      ? metaQuery.data.default_memory_limit_mb
      : null;
  const maxMemoryLimitMb: number =
    typeof metaQuery.data?.max_memory_limit_mb === "number"
      ? metaQuery.data.max_memory_limit_mb
      : MEMORY_LIMIT_DEFAULT_CEILING_MIB;

  const versionsQuery = useQuery({
    queryKey: ["versions", type],
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/versions/{server_type}", { server_type: type as string }),
        { signal },
      ),
    enabled: type !== null,
  });
  const versionsState = classifyQueryResult(versionsQuery);
  const versions = versionsQuery.data?.versions ?? [];

  // Catalogs list newest first, so seed the selected version from the first entry.
  useEffect(() => {
    if (versions.length > 0) {
      setVersion(versions[0]);
    }
  }, [versions]);

  const [memoryLimitTouched, setMemoryLimitTouched] = useState(false);
  useEffect(() => {
    if (defaultMemoryLimitMb !== null && !memoryLimitTouched) {
      setMemoryLimit(String(defaultMemoryLimitMb));
    }
  }, [defaultMemoryLimitMb, memoryLimitTouched]);

  const portCheck = usePortCheck(port);

  // Suggest a port on the config step only before user edits; suggestion failure leaves manual entry available.
  useEffect(() => {
    if (step !== 2 || portTouched || relayEnabled) {
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
        // A failed suggestion leaves the port editable and its on-blur validation available.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [step, portTouched, relayEnabled]);

  const memoryLimitOk = memoryLimitValid(memoryLimit, maxMemoryLimitMb);
  const cpuAllocationOk = cpuAllocationValid(cpuAllocation);
  const slugOk = slugCreateValid(slug);

  async function onCreate() {
    setSubmitting(true);
    setNameError(undefined);
    setSlugError(undefined);
    const config: Record<string, string | number> = {};
    for (const { key, value } of props) {
      if (key.trim() !== "") {
        config[key.trim()] = value;
      }
    }
    if (memoryLimit.trim() !== "") {
      config[MEMORY_LIMIT_KEY] = Number(memoryLimit);
    }
    if (cpuAllocation.trim() !== "") {
      config[CPU_ALLOCATION_KEY] = Number(cpuAllocation);
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
            config,
            accept_eula: acceptEula,
            ...(relayEnabled
              ? {}
              : { game_port: port === "" ? null : Number(port) }),
            ...(slug.trim() !== "" ? { slug: slug.trim() } : {}),
          }),
        },
      );
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      handleCreateError(err, showToast, setNameError, setSlugError);
      setSubmitting(false);
    }
  }

  return (
    <>
      <StepRail step={step} />

      {step === 1 && (
        <div className="card">
          <h2>{t("serverCreate.typeHeading")}</h2>
          {typesState.kind === "pending" ? (
            <p className="sub">{t("serverCreate.typeLoading")}</p>
          ) : /* Retain cached content when only a background refetch fails. */
          typesState.kind === "error" ? (
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
                    <div className="t-name">
                      {TYPE_LABEL[typeOption] !== undefined
                        ? t(TYPE_LABEL[typeOption])
                        : typeOption}
                    </div>
                    <div className="t-sub">
                      {TYPE_SUB[typeOption] !== undefined
                        ? t(TYPE_SUB[typeOption])
                        : ""}
                    </div>
                  </button>
                ))}
              </div>
              {type !== null && (
                <div className="field">
                  <label htmlFor="version-select">
                    {t("serverCreate.versionLabel")}
                  </label>
                  {versionsState.kind === "pending" ? (
                    <p className="sub">{t("serverCreate.versionLoading")}</p>
                  ) : /* Retain cached content when only a background refetch fails. */
                  versionsState.kind === "error" ? (
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

          {relayEnabled ? null : (
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
          )}

          {relayEnabled && (
            <div className="field">
              <label htmlFor="slug-input">{t("serverCreate.slugLabel")}</label>
              <input
                id="slug-input"
                type="text"
                value={slug}
                placeholder={t("serverCreate.slugPlaceholder")}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setSlugError(undefined);
                }}
              />
              {slugError !== undefined ? (
                <div className="error" role="alert">
                  {slugError}
                </div>
              ) : !slugOk ? (
                <div className="error" role="alert">
                  {t("serverCreate.slugInvalid")}
                </div>
              ) : (
                <div className="hint">{t("serverCreate.slugHint")}</div>
              )}
            </div>
          )}

          <div className="field">
            <label htmlFor="memory-limit-input">
              {t("serverCreate.memoryLimitLabel")}
            </label>
            <input
              id="memory-limit-input"
              type="number"
              value={memoryLimit}
              placeholder={t("serverCreate.memoryLimitDefault")}
              onChange={(e) => {
                setMemoryLimitTouched(true);
                setMemoryLimit(e.target.value);
              }}
            />
            {memoryLimitOk ? (
              <div className="hint">{t("serverCreate.memoryLimitHint")}</div>
            ) : (
              <div className="error" role="alert">
                {t("serverCreate.memoryLimitRange")}
              </div>
            )}
          </div>

          <div className="field">
            <label htmlFor="cpu-allocation-input">
              {t("serverCreate.cpuAllocationLabel")}
            </label>
            <input
              id="cpu-allocation-input"
              type="number"
              value={cpuAllocation}
              placeholder={t("serverCreate.cpuAllocationDefault")}
              onChange={(e) => setCpuAllocation(e.target.value)}
            />
            {cpuAllocationOk ? (
              <div className="hint">{t("serverCreate.cpuAllocationHint")}</div>
            ) : (
              <div className="error" role="alert">
                {t("serverCreate.cpuAllocationRange")}
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
              onClick={() => setStep(1)}
            >
              {t("serverCreate.back")}
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={
                submitting ||
                name.trim() === "" ||
                !memoryLimitOk ||
                !cpuAllocationOk ||
                !slugOk
              }
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
      <span className="group-label">{t("serverCreate.propsHeading")}</span>
      <div className="hint">{t("serverCreate.propsHint")}</div>
      {props.map((row, i) => (
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

type PortState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "available" }
  | { kind: "taken" }
  | { kind: "out_of_range" }
  | { kind: "error" };

function usePortCheck(port: string) {
  const [state, setState] = useState<PortState>({ kind: "idle" });
  // Ignore earlier port checks that finish after a newer check.
  const latestRequest = useRef(0);

  async function check() {
    if (port === "") {
      latestRequest.current += 1;
      setState({ kind: "idle" });
      return;
    }
    const requestId = ++latestRequest.current;
    setState({ kind: "checking" });
    try {
      const result = await api.get(
        apiPath("/api/ports/check/{port}", { port }),
      );
      if (latestRequest.current !== requestId) return;
      if (result.in_range === false) {
        setState({ kind: "out_of_range" });
      } else if (result.available === true) {
        setState({ kind: "available" });
      } else {
        setState({ kind: "taken" });
      }
    } catch {
      if (latestRequest.current !== requestId) return;
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

function ImportForm({ communityId }: { communityId: string }) {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>();
  const progress = useUploadProgress();

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
    const signal = progress.start(file.size);
    try {
      const server = await postFormWithProgress(
        apiPath("/api/communities/{community_id}/servers/import", {
          community_id: communityId,
        }),
        form,
        progress.onProgress,
        signal,
      );
      progress.reset();
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      progress.reset();
      if (isUploadAbortError(err)) {
        setSubmitting(false);
        return;
      }
      handleImportError(err, showToast, setNameError);
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
        <label htmlFor="import-file">
          {t("serverCreate.import.fileLabel")}
        </label>
        <FilePicker
          id="import-file"
          accept=".zip"
          file={file}
          onSelect={setFile}
        />
      </div>
      {progress.active && (
        <UploadProgress
          loaded={progress.loaded}
          total={progress.total}
          percent={progress.percent}
          elapsedMs={progress.elapsedMs}
          onCancel={progress.cancel}
        />
      )}
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

function handleCreateError(
  err: unknown,
  showToast: (m: string, k: "error") => void,
  setNameError: (m: string) => void,
  setSlugError: (m: string) => void,
): void {
  if (surfaceNameValidationError(err, setNameError)) {
    return;
  }
  const { target, key } = serverCreateErrorPresentation(
    err instanceof ApiError ? err.reason : undefined,
  );
  if (target === "name") {
    setNameError(t(key));
  } else if (target === "slug") {
    setSlugError(t(key));
  } else {
    showToast(t(key), "error");
  }
}

function handleImportError(
  err: unknown,
  showToast: (m: string, k: "error") => void,
  setNameError: (m: string) => void,
): void {
  if (surfaceNameValidationError(err, setNameError)) {
    return;
  }
  const problem = err instanceof ApiError ? err : undefined;
  const { target, key } = serverImportErrorPresentation(
    problem?.status,
    problem?.reason,
  );
  if (target === "name") {
    setNameError(t(key));
  } else {
    showToast(t(key), "error");
  }
}

// Structural name-validation errors carry API messages rather than translation keys.
function surfaceNameValidationError(
  err: unknown,
  setNameError: (m: string) => void,
): boolean {
  if (!(err instanceof ApiError) || err.reason !== "validation_error") {
    return false;
  }
  const fields = fieldErrorsFromValidation(err.body, ["name"]);
  if (fields?.name === undefined) {
    return false;
  }
  setNameError(fields.name);
  return true;
}
