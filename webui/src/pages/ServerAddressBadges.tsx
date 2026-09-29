import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type { components } from "../api/schema";
import { copyToClipboard } from "../clipboard.ts";
import { t } from "../i18n/index.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

// A server's join addresses as click-to-copy buttons, shared by the dashboard
// card/table rows and the server detail header: the Java join hostname (issue
// #961) and, when the server holds a Bedrock port, the Bedrock address:port
// (issue #1543). `fallback` renders in place of the Java button when the server
// has no join hostname (relay off). Returns a fragment so the buttons sit
// directly in the caller's layout.
export function ServerAddressBadges({
  server,
  buttonClassName,
  buttonStyle,
  fallback,
}: {
  server: Pick<
    ServerResponse,
    "join_hostname" | "bedrock_address" | "bedrock_port"
  >;
  buttonClassName: string;
  buttonStyle?: CSSProperties;
  fallback: ReactNode;
}) {
  return (
    <>
      {server.join_hostname !== null ? (
        <CopyButton
          text={server.join_hostname}
          title={server.join_hostname}
          copiedLabel={t("dashboard.copiedJoinHostname")}
          className={buttonClassName}
          style={buttonStyle}
        >
          {server.join_hostname}
        </CopyButton>
      ) : (
        fallback
      )}
      {server.bedrock_address !== null && server.bedrock_port !== null && (
        // Copy the host only: Bedrock's "Add Server" screen has a separate Port
        // field, and pasting `host:port` into the address field fails validation.
        <CopyButton
          text={server.bedrock_address}
          title={t("dashboard.bedrockAddressCopyTitle", {
            port: server.bedrock_port,
          })}
          copiedLabel={t("dashboard.copiedBedrockAddress")}
          className={buttonClassName}
          style={buttonStyle}
        >
          {t("dashboard.bedrockLabel")}: {server.bedrock_address}:
          {server.bedrock_port}
        </CopyButton>
      )}
    </>
  );
}

// One copy button with its own "Copied!" state, so the Java and Bedrock
// feedback never share a flag or a reset timer. A failed copy drops the
// feedback at once rather than leaving an earlier success showing (#976).
function CopyButton({
  text,
  title,
  copiedLabel,
  className,
  style,
  children,
}: {
  text: string;
  title: string;
  copiedLabel: string;
  className: string;
  style: CSSProperties | undefined;
  children: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopy = useCallback(() => {
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    copyToClipboard(text).then(
      () => {
        setCopied(true);
        copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
      },
      () => {
        setCopied(false);
      },
    );
  }, [text]);

  return (
    <button
      type="button"
      className={className}
      title={title}
      style={style}
      onClick={handleCopy}
    >
      {copied ? copiedLabel : children}
    </button>
  );
}
