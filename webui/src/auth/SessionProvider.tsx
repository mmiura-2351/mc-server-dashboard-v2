/** Hold route guards while the httpOnly cookie is exchanged for an access token. */

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
  signIn as signInSession,
} from "./session.ts";
import { getAuthEpoch } from "./tokenStore.ts";

export type SessionStatus = "bootstrapping" | "signed-in" | "signed-out";

interface SessionContextValue {
  status: SessionStatus;
  signIn: (accessToken: string) => void;
  logout: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("bootstrapping");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Read the current route at logout time without rebinding the handler on navigation.
  const location = useLocation();
  const locationRef = useRef(location);
  locationRef.current = location;

  // Clear cached data before another account can sign in.
  // Only involuntary expiry preserves the current route and shows an expiry notice.
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

  useEffect(() => {
    setRefresher(refreshForRetry);
    setHardLogoutHandler(resetToSignedOut);
  }, [resetToSignedOut]);

  // Bootstrap does not rotate the refresh cookie.
  // Recheck the epoch when applying status; sign-in or logout may overtake result delivery.
  useEffect(() => {
    let active = true;
    const epoch = getAuthEpoch();
    restoreSession().then((result) => {
      if (active && result !== "superseded" && epoch === getAuthEpoch()) {
        setStatus(result);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const signIn = useCallback((accessToken: string) => {
    signInSession(accessToken);
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
