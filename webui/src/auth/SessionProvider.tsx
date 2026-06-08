/**
 * Session context (WEBUI_SPEC.md 7.1).
 *
 * Exposes the minimal signed-in state and a logout action for #410 (login page
 * / guards) and #411 (account page) to consume. On mount it bootstraps the
 * session from the httpOnly refresh cookie; while that initial refresh is in
 * flight the status is "bootstrapping" so guards can hold rendering instead of
 * bouncing a returning user to /login.
 */

import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router";
import { setRefresher } from "../api/client.ts";
import { expiredLoginPath } from "../routes.ts";
import {
  type LogoutReason,
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
  const queryClient = useQueryClient();

  // The latest router location, kept in a ref so the hard-logout handler can
  // read where the user was without re-binding the handler on every navigation
  // (#565). A 401 hard logout fires outside React's render, so the handler
  // needs the current location at call time, not at bind time.
  const location = useLocation();
  const locationRef = useRef(location);
  locationRef.current = location;

  // Reset to signed-out and send the user to /login. Used both for an explicit
  // logout and for a hard logout triggered by a failed transparent refresh.
  // Drop every cached query so the previous user's data (user, communities,
  // servers, members…) can never render for the next account on a shared
  // browser, and so the next sign-in bootstraps from an empty cache (#532).
  // Per-device prefs (language, module-level UI stores) live outside the query
  // cache and are intentionally left untouched.
  //
  // An involuntary expiry (reason "expired") captures where the user was so
  // login can return them there and explain the logout; a deliberate logout
  // (no reason) lands on a clean /login (#565). The location is read from the
  // ref so the latest route is captured even though this handler is invoked
  // outside render by a failed transparent refresh.
  const resetToSignedOut = useCallback(
    (reason?: LogoutReason) => {
      queryClient.clear();
      setStatus("signed-out");
      navigate(
        reason === "expired" ? expiredLoginPath(locationRef.current) : "/login",
      );
    },
    [navigate, queryClient],
  );

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
