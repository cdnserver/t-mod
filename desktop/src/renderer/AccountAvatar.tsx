import { useState } from "react";

export function trustedDiscordAvatar(value?: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port ||
        !["cdn.discordapp.com", "media.discordapp.net"].includes(url.hostname) ||
        !/^\/(?:avatars\/|embed\/avatars\/|guilds\/\d+\/users\/\d+\/avatars\/)/.test(url.pathname)) return undefined;
    return url.href;
  } catch { return undefined; }
}

export function AccountAvatar({ url, name }: { url?: string | null; name: string }) {
  const [failedUrl, setFailedUrl] = useState<string>();
  const source = trustedDiscordAvatar(url);
  return <span className="bb-account-avatar" aria-hidden="true">
    {source && source !== failedUrl
      ? <img src={source} alt="" referrerPolicy="no-referrer" draggable={false} onError={() => setFailedUrl(source)}/>
      : <span>{Array.from(name.trim())[0]?.toLocaleUpperCase() || "•"}</span>}
  </span>;
}
