import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/global.css";
import "./markdown.css";
import LabApp from "./LabApp";
import AuthGate from "./auth/AuthGate";
import CallbackRoute from "./auth/CallbackRoute";
import SilentCallbackRoute from "./auth/SilentCallbackRoute";
import RenderDeepLinkRoute from "./views/RenderDeepLinkRoute";
import OpenArtifactRoute from "./views/OpenArtifactRoute";
// Side effect: fills the chat composer's upload-sheet slot.
import "./projects/upload/registerUploadUi";
import "./projects/registerWebSearchUi";
import { initBrandingFromConfig } from "./config/branding";
import { applyTheme, readThemePreference } from "./styles/themePreference";

// Load runtime branding from /config (logo, brand name)
initBrandingFromConfig();

// Apply the persisted theme before React mounts, so there is no flash of the
// wrong one. Named themes now, with a fallback to the old boolean — see
// readThemePreference.
applyTheme(readThemePreference());
const savedFontSize = localStorage.getItem('lab-font-size');
if (savedFontSize) {
  document.documentElement.setAttribute('data-font-size', savedFontSize);
}

// Path-based routing. The Lab UI is otherwise a single-view SPA, so a full
// router is overkill — we just dispatch the OIDC callback URLs to dedicated
// components and render the app shell for everything else. The callback
// components must mount OUTSIDE `AuthGate`, otherwise the gate would
// redirect the browser back to Cognito before the callback handler ran.
function pickRoot() {
  const pathname = window.location.pathname;

  if (pathname === "/auth/callback") {
    return <CallbackRoute />;
  }

  if (pathname === "/auth/silent-callback") {
    return <SilentCallbackRoute />;
  }

  // Read-only render deep-link (e.g. a Slack "Open interactive renderer" link).
  // Login-free where the artifacts CDN is public; signed-in where it is private
  // (RenderDeepLinkRoute decides from /config). Distinct path — `/render` is a proxy route.
  if (pathname === "/render-view") {
    return <RenderDeepLinkRoute />;
  }

  // Stable artifact link (`/open?key=artifacts/...`), e.g. from Slack: always signed in (sign-in comes back to
  // this same URL), then a redirect to a fresh presigned URL for the key.
  if (pathname === "/open") {
    return <AuthGate>{() => <OpenArtifactRoute />}</AuthGate>;
  }

  return (
    <AuthGate>
      {({ onShowLanding }) => <LabApp onShowLanding={onShowLanding} />}
    </AuthGate>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>{pickRoot()}</StrictMode>
);
