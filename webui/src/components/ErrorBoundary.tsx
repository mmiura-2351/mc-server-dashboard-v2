import { Component, type ErrorInfo, type ReactNode } from "react";
import { t } from "../i18n/index.ts";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

/** Keep a reload action available when a routed page fails to render.
 * React requires a class component for getDerivedStateFromError. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
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
