import { Component, type ErrorInfo, type ReactNode } from "react";
import { t } from "../i18n/index.ts";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

/**
 * Top-level error boundary that catches unhandled rendering errors and shows a
 * recovery UI instead of React's default white-screen unmount (#1211).
 *
 * Placed around the routing tree in `<App>` so a crash in any page still lets
 * the user reload.  React requires a class component for `getDerivedStateFromError`.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    // Log for diagnostics; no telemetry endpoint yet.
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught:", error, info);
  }

  override render() {
    if (this.state.hasError) {
      return (
        <div className="auth-wrap" role="alert">
          <div style={{ textAlign: "center" }}>
            <h1>{t("errorBoundary.title")}</h1>
            <p>{t("errorBoundary.body")}</p>
            <button
              type="button"
              onClick={() => {
                window.location.reload();
              }}
            >
              {t("errorBoundary.reload")}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
