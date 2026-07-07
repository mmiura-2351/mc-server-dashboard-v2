import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { ApiError, api, postFormWithProgress } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { fieldErrorsFromValidation } from "../api/validationErrors.ts";
import { FilePicker } from "../components/FilePicker.tsx";
import { useToast } from "../components/Toast.tsx";
import { UploadProgress } from "../components/UploadProgress.tsx";
import { useUploadProgress } from "../components/useUploadProgress.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { useCanCode } from "../permissions/useCan.ts";
import { dashboardPath } from "../routes.ts";
import { handleTabKeyDown, panelId, tabId, useTabHash } from "./urlState.ts";

// Server create wizard (WEBUI_SPEC.md 6.3). Two steps for a fresh server
// (type & version → config & EULA) plus an "Import ZIP" tab that uploads a
// prior export. The whole page is gated on `server:create`; the server stays
// authoritative (any 403/422/409 is surfaced honestly).

// The catalogued types `GET /versions` can resolve (vanilla/paper/fabric/forge).
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

// Per-server memory limit / CPU allocation in the create wizard (issue #715),
// mirroring the Settings tab (ServerDetailPage.tsx). Both ride the `config` blob
// as reserved keys and are optional: blank = driver default / auto.
const MEMORY_LIMIT_KEY = "memory_limit_mb";
const MEMORY_LIMIT_FLOOR_MIB = 512;
const MEMORY_LIMIT_DEFAULT_CEILING_MIB = 1024 * 1024;
const CPU_ALLOCATION_KEY = "cpu_millis";
const CPU_ALLOCATION_FLOOR_MILLIS = 100;
const CPU_ALLOCATION_CEILING_MILLIS = 128_000;

// A non-blank input is valid only as a whole number within range; blank is
// always allowed (the key is omitted so the server falls back to the default).
// The ceiling can be overridden by the operator-configurable max (#1069).
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

// DNS-label regex for slug inline validation — mirrors the API rule (issue #981).
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

// A slug input is valid when it matches the DNS-label regex; blank is also
// allowed (leave field empty → auto-generate a random slug).
function slugCreateValid(value: string): boolean {
  return value.trim() === "" || SLUG_RE.test(value.trim());
}

// Create-path problem reasons that map to a specific inline/toast message. A
// 409 `port_taken` is surfaced specifically (issue requirement); everything else
// falls back to the generic toast.
const CREATE_ERROR_KEY: Record<string, TranslationKey> = {
  port_taken: "serverCreate.error.port_taken",
  port_out_of_range: "serverCreate.error.port_out_of_range",
  server_name_exists: "serverCreate.error.server_name_exists",
  invalid_server_name: "serverCreate.error.invalid_server_name",
  unknown_version: "serverCreate.error.unknown_version",
  invalid_memory_limit: "serverCreate.error.invalid_memory_limit",
  invalid_cpu_allocation: "serverCreate.error.invalid_cpu_allocation",
  invalid_slug: "serverCreate.error.invalid_slug",
  slug_taken: "serverCreate.error.slug_taken",
};

export function ServerCreatePage() {
  // Create in the community named by the URL `:cid` (#784), not the active one:
  // the two can disagree on a stale bookmark or a community the user has left,
  // and creating in the active community while the URL says another is wrong.
  // The active-community list is the caller's membership; the URL cid must be in
  // it. `useUrlCommunitySync` (AppShell) adopts an in-membership cid as the
  // active community, so the `server:create` capability resolves for this cid.
  const { cid } = useParams();
  const { communities } = useActiveCommunity();
  const canCreate = useCanCode("server:create");

  // Membership still loading: hold the chrome rather than flash a not-found.
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

// The two modes live in the URL hash (#540, following the #514/#528 tab
// convention): the new-server wizard is the default and keeps a clean URL,
// while #import deep-links to the ZIP-import tab and Back walks the tab history.
// The wizard's internal steps stay in component state — they hold form input, so
// per-step history would make Back discard half-filled fields.
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

// ---------------------------------------------------------------------------
// New-server wizard (2 steps)
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
  const [port, setPort] = useState("");
  // Once the user edits the port, the auto-suggest must never overwrite it.
  const [portTouched, setPortTouched] = useState(false);
  const [name, setName] = useState("");
  // Optional join address name (slug, issue #981). Blank = auto-generate.
  const [slug, setSlug] = useState("");
  const [props, setProps] = useState<PropOverride[]>([]);
  // Empty string ↔ unset (driver default / auto); a number ↔ the value (MiB /
  // millicores). Mirrors the Settings tab's optional resource fields (#715).
  const [memoryLimit, setMemoryLimit] = useState("");
  const [cpuAllocation, setCpuAllocation] = useState("");
  const [acceptEula, setAcceptEula] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>();
  const [slugError, setSlugError] = useState<string | undefined>();

  const typesQuery = useQuery({
    queryKey: ["versions"],
    queryFn: () => api.get("/api/versions"),
  });
  const catalogTypes = typesQuery.data?.server_types ?? [];

  // In relay mode the game port is hidden and API-managed: players join port-less
  // via the slug hostname, so the create form surfaces no port control (#1002).
  // In direct mode the port stays user-editable (a port-forward is needed).
  const metaQuery = useQuery({
    queryKey: ["meta"],
    queryFn: () => api.get("/api/meta"),
  });
  // While loading or on error, default to showing the port control (treat as
  // direct-mode): hiding port controls on error is more disruptive than briefly
  // showing them, and direct-mode is the majority case (#1061).
  const relayEnabled =
    metaQuery.isLoading || metaQuery.isError
      ? false
      : metaQuery.data?.relay_enabled === true;
  // Operator-configurable memory-limit knobs from /meta (issue #1069).
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

  // Pre-fill the memory-limit field with the operator default when the meta
  // response arrives and the user has not yet typed a value (#1069).
  const [memoryLimitTouched, setMemoryLimitTouched] = useState(false);
  useEffect(() => {
    if (defaultMemoryLimitMb !== null && !memoryLimitTouched) {
      setMemoryLimit(String(defaultMemoryLimitMb));
    }
  }, [defaultMemoryLimitMb, memoryLimitTouched]);

  const portCheck = usePortCheck(port);

  // On reaching the config step, prefill the game port from the next free port
  // (GET /ports/available, SPEC 6.3) unless the user has already typed one. A
  // failed suggest leaves the field empty — the user can still type a port.
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
        // Leave the field empty; the user types a port and the on-blur check
        // still validates it.
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
    // Layer the resource fields: a number when set, omitted when blank so the
    // server falls back to the driver default / auto share (#715).
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
            // In relay mode the port is hidden and API-allocated, so omit it; in
            // direct mode send the chosen port (null = auto-assign) (#1002).
            ...(relayEnabled
              ? {}
              : { game_port: port === "" ? null : Number(port) }),
            // Blank slug = omit so the API auto-generates a random one (issue #981).
            ...(slug.trim() !== "" ? { slug: slug.trim() } : {}),
          }),
        },
      );
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      if (!handleCreateError(err, showToast, setNameError, setSlugError)) {
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
  // Only the latest issued check may apply its result: blurs on different ports
  // can resolve out of order and a stale response must not clobber the current
  // verdict (#1592).
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

// ---------------------------------------------------------------------------
// Import tab (POST .../servers/import, multipart)
// ---------------------------------------------------------------------------

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
    progress.start(file.size);
    try {
      const server = await postFormWithProgress(
        apiPath("/api/communities/{community_id}/servers/import", {
          community_id: communityId,
        }),
        form,
        progress.onProgress,
      );
      navigate(`${dashboardPath(communityId)}/servers/${server.id}`);
    } catch (err) {
      progress.reset();
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

// ---------------------------------------------------------------------------
// Shared error surfacing
// ---------------------------------------------------------------------------

// Surface a create failure. A mapped reason becomes a specific inline error or
// toast: `invalid_server_name` and a structural validation_error on `name` go
// inline against the name field; `invalid_slug`/`slug_taken` go inline against
// the slug field; all other mapped reasons become toasts.
// Returns whether it was handled.
function handleCreateError(
  err: unknown,
  showToast: (m: string, k: "error") => void,
  setNameError: (m: string) => void,
  setSlugError: (m: string) => void,
): boolean {
  if (!(err instanceof ApiError)) {
    return false;
  }
  if (err.reason !== undefined) {
    if (err.reason === "invalid_server_name") {
      setNameError(t("serverCreate.error.invalid_server_name"));
      return true;
    }
    if (err.reason === "invalid_slug") {
      setSlugError(t("serverCreate.error.invalid_slug"));
      return true;
    }
    if (err.reason === "slug_taken") {
      setSlugError(t("serverCreate.error.slug_taken"));
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
// reasons it shares (name conflict, …). Import has no slug field, so
// slug errors (not reachable from import) fall back to the generic toast path.
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
  return handleCreateError(err, showToast, setNameError, () => {});
}
