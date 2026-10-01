import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import { BlackbirdCommunicate } from "./BlackbirdCommunicate";
import "./blackbird-communicate.css";

declare global {
  interface Window {
    blackbirdCommunicate?: {
      context(): Promise<{ viewerId: number; authenticated: boolean; sharedUrl: string }>;
      request(action: "communicate" | "communicate-update", data?: Record<string, string>): Promise<Record<string, unknown>>;
      close(): Promise<void>;
      minimize(): Promise<void>;
      openLink(url: string): Promise<boolean>;
      copyLink(url: string): Promise<boolean>;
      onShare(listener: (url: string) => void): () => void;
    };
  }
}

function CommunicateWindow() {
  const [share, setShare] = useState({ url: "", sequence: 0 });
  useEffect(() => {
    void window.blackbirdCommunicate?.context().then(value => { if (value.sharedUrl) setShare(current => ({ url: value.sharedUrl, sequence: current.sequence + 1 })); });
    return window.blackbirdCommunicate?.onShare(url => setShare(current => ({ url, sequence: current.sequence + 1 })));
  }, []);
  return <BlackbirdCommunicate servers={[]} onBack={() => void window.blackbirdCommunicate?.close()} sharedUrl={share.url} shareSequence={share.sequence}/>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><CommunicateWindow/></React.StrictMode>);
