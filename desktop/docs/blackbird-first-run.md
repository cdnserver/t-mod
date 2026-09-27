# Blackbird · Windows first run and notifications

The publisher scene holds the signature for ten seconds even with cached assets.
Its original artwork is revealed in staggered mask cells, followed by a soft
light pass. After ten seconds and real preparation it fades to black, then
holds this phase for 2.6 seconds (including the fade). Slow loading postpones
this phase, so the black pause is never skipped. A 2.4-second dissolve opens
the lunar keyboard-gated launch screen: fifteen seconds total with cached
assets. Keyboard input cannot bypass the minimum. Reduced motion shows the
complete signature without staggered movement but preserves the pause.
Appearance settings persist three original-artwork reveals: staggered cells,
whole-signature emergence from darkness, and a directional light reveal.
The preview button runs the actual startup scene and returns to settings after
keyboard continuation. The signature is smaller (59vw, capped at 1080px).
Graphics preparation falls back after 12 seconds; font, image and initial
connection preparation settle after 15 seconds rather than blocking indefinitely.

First-run setup connects the existing central account, chooses a preferred name,
idle-lock timeout, reduced motion and notification delivery/sound preferences.
It is recorded per account on this installation. Signing into another account
requires its own setup. The wizard can be reopened from Settings.
It displays only the enlarged Technologies signature, not the Blackbird bird.

The Windows control bar is 44px tall. Appearance settings can move it to a
48px right-hand rail. Embedded native service bounds use the same shared layout
dimensions, so services cannot overlap the controls. Public T-Mod retains its
existing layout. Lunar rendering feathers the silhouette in CSS pixels without
blurring the surface; the software fallback applies the same coverage curve.

Blackbird's central hub now enters local Atlas or Senate workspace overviews,
not a remote service directly. Each has its own navigation, permitted service
cards and workspace-filtered server events. Atlas contains AI and overlay tools;
Senate groups participation, institutions and operational systems. Switching
workspaces returns the native service view to `home` before a 3.4-second local
intro; moving between services in one workspace does not replay it. Reduced
motion uses a brief static ident. Intros can be skipped and keep keyboard focus
inside the transition while native service views remain hidden. Direct service
links and notifications infer the correct workspace; permission checks remain
in both renderer and native navigation. Public T-Mod navigation is unchanged.

Account creation still uses the verified existing `/account` identity flow.
The native wizard opens its fixed Discord registration destination and then
accepts the created login/PIN. This is not an independent native registration
endpoint. No PIN is stored in setup completion markers or preferences.

Notification delivery:

- Adaptive: custom card when Blackbird is focused; Windows notification in the background.
- Custom: separate non-focus-stealing window, independent of embedded service views.
- Windows: operating-system notifications, subject to Windows notification settings.
- Off: no popup; events remain in the server notification list.

The custom window queues up to eight pending cards; ordinary cards dismiss after
8.5 seconds and pause on hover. Critical cards require dismissal. Click opens
the related service (or notification centre), restoring a minimized client.
The centre provides text search, unread/important filters and server-backed
read acknowledgements using the existing authenticated, CSRF-protected API.
Initial history does not produce a popup flood. Locking hides active cards and
redacts subsequently delivered previews. Signing out clears queued cards.

Preview-only URLs (no real account creation or Windows OS delivery):

- `/?hub-preview=1&setup-preview=1&skip-launch=1`
- `/?login-preview=1&setup-preview=1&skip-launch=1`
- `/notification-preview.html?preview=1`

Production packages use `notification.html` with a restricted CSP and a dedicated
sandboxed preload. Only its own sender can dismiss/open the popup. Backend
access grants remain authoritative. Blackbird installer builds target Windows x64;
no macOS/Linux package and no Wine build are supported.
