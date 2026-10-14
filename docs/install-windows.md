<!-- CODE-VERIFY: Verify the Windows installer filename and per-user installation, extension minimum browser version, hardware-key requirement, local port, first-run setup and return behavior, data path, and uninstall preservation against manifest, installer, launcher, provisioning and frontend source before changing this guide. -->

# Install on Windows

Install the desktop app and browser extension from the same published package. You do not need administrator rights or development tools.

## Requirements

- 64-bit Windows on a computer that supports hardware-backed device keys.
- Google Chrome version 132 or later for setup and the browser extension.
- Port `17871` available on your computer.

## Download and install

1. Download the Windows installer and browser extension ZIP from the same published package. [Verify the files](verify-release.md) against the published checksums.
2. Run `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe`. The app installs in your Windows user account and adds **Conversation Analytics** to the Start menu.
3. Unpack the browser extension ZIP and add the unpacked folder through your browser's extensions page. The desktop installer does not add the extension for you.

The installer checks for Microsoft Edge WebView2 Runtime and installs it if needed. Chrome is the browser used for setup tasks. You can install the app or the extension first.

## Set up the desktop app

1. Open **Conversation Analytics** from the installer or Start menu. Setup opens in the app.
2. Select **Set up browser access**. In Chrome, use the profile containing the extension and your OnlyFans creator account. Review access and grant the requested site permission.
3. Return to the app and select **Sign in**. Complete the browser sign-in and any required account or computer approval.
4. Set up a passkey on this computer, sign in, and connect Chrome when prompted. Open the dashboard when local setup is complete.

Full analytics activation is available in Settings after local setup. It does not block opening the dashboard.

Setup records progress automatically. Reopening the app resumes the accepted setup operation. Interrupted responses are recovered with this computer's existing device key; no recovery package is required. If you're transferring setup to another computer, choose **I have a setup code** on that destination.

If you only want the seven-day Preview in your browser, you can enable it directly in the extension without installing the desktop app.

## If setup cannot continue

Follow the action shown on the current setup screen. The app reconnects after temporary local-service interruptions and checks interrupted account setup automatically.

If another application already uses port `17871`, close that application and open Conversation Analytics again.

If Windows cannot provide a hardware-backed device key, the desktop app cannot complete installation setup on that computer.

## Open, close and quit

Closing the app window keeps the local service running in the Windows tray. Use the tray menu to reopen the app or select **Quit** to stop it.

Completed local setup remains available while offline under the locally held authorization. Signing in to the Provider, obtaining new approvals, and activating Full analytics require an internet connection. Expired or revoked authorization can limit the available actions.

## Local data and uninstalling

Your data is stored by default in `%LOCALAPPDATA%\OnlyFans Conversational Analytics`.

To uninstall, open **Windows Settings > Apps > Installed apps** and select **Conversation Analytics**. Removing the app leaves the local data folder in place; it does not delete your conversation data.
