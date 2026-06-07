/**
 * Session context (WEBUI_SPEC.md 7.1).
 *
 * Exposes the minimal signed-in state and a logout action for #410 (login page
 * / guards) and #411 (account page) to consume. On mount it bootstraps the
 * session from the httpOnly refresh cookie; while that initial refresh is in
 * flight the status is "bootstrapping" so guards can hold rendering instead of
 * bouncing a returning user to /login.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useNavigate } from "react-router";
import { setRefresher } from "../api/client.ts";
import {
  logout as logoutSession,
  refreshForRetry,
  restoreSession,
  setHardLogoutHandler,
} from "./session.ts";
import { setAccessToken } from "./tokenStore.ts";

export type SessionStatus = "bootstrapping" | "signed-in" | "signed-out";

interface SessionContextValue {
  status: SessionStatus;
  /** Adopt the access token from a fresh /auth/login and mark signed-in. */
  signIn: (accessToken: string) => void;
  logout: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("bootstrapping");
  const navigate = useNavigate();

  // Reset to signed-out and send the user to /login. Used both for an explicit
  // logout and for a hard logout triggered by a failed transparent refresh.
  const resetToSignedOut = useCallback(() => {
    setStatus("signed-out");
    navigate("/login");
  }, [navigate]);

  // Wire the framework-free session core to React: the client retries 401s
  // through the single-flight refresh, and a hard logout resets this state.
  useEffect(() => {
    setRefresher(refreshForRetry);
    setHardLogoutHandler(resetToSignedOut);
  }, [resetToSignedOut]);

  // Bootstrap once on load: the cookie is exchanged for an access token via the
  // NON-rotating /api/auth/session probe (issue #512), so a page load / F5 never
  // rotates the refresh token and can never leave a torn rotation in the jar.
  // Rotation stays on the in-session refresh path. The probe decides
  // signed-in vs signed-out.
  useEffect(() => {
    let active = true;
    restoreSession().then((ok) => {
      if (active) {
        setStatus(ok ? "signed-in" : "signed-out");
      }
    });
    return () => {
      active = false;
    };
  }, []);

  // Login already authenticated against /auth/login and holds the issued access
  // token; adopt it and flip to signed-in. The refresh cookie is set by that
  // same response, so a later reload re-bootstraps cleanly.
  const signIn = useCallback((accessToken: string) => {
    setAccessToken(accessToken);
    setStatus("signed-in");
  }, []);

  const logout = useCallback(async () => {
    await logoutSession();
  }, []);

  const value = useMemo<SessionContextValue>(
    () => ({ status, signIn, logout }),
    [status, signIn, logout],
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used within a SessionProvider");
  }
  return value;
}
