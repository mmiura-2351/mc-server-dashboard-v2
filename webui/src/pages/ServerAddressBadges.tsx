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

// Render caller-provided fallback content when no Java join hostname is available.
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
        // Copy only the host; Bedrock's Add Server screen rejects host:port in its address field.
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

// Keep copy feedback and timers independent for Java and Bedrock; clear earlier success on failure.
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
