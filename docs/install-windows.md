<!-- CODE-VERIFY: Verify the Windows installer filename and per-user installation, extension minimum browser version, hardware-key requirement, local port, first-run setup and return behavior, data path, and uninstall preservation against manifest, installer, launcher, provisioning and frontend source before changing this guide. -->

# Install on Windows

Install the desktop app and browser extension from the same published package. You do not need administrator rights or development tools.

## Requirements

- 64-bit Windows on a computer that supports hardware-backed device keys.
- Chrome, Edge, or another compatible Chromium browser version 132 or later.
- Port `17871` available on your computer.

## Download and install

1. Download the Windows installer and browser extension ZIP from the same published package. [Verify the files](verify-release.md) against the published checksums.
2. Run `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe`. The app installs in your Windows user account and adds **OnlyFans Conversational Analytics** to the Start menu.
3. Unpack the browser extension ZIP and add the unpacked folder through your browser's extensions page. The desktop installer does not add the extension for you.

## Set up Full analytics

1. Open **OnlyFans Conversational Analytics** from the Start menu. Setup opens in your browser.
2. Follow the setup page to choose Full analytics and grant the browser permissions it requests. The page skips requirements you have already completed.
3. When asked, sign in and confirm the OnlyFans account and computer you want to connect. Approve the connection in secure setup.
4. Complete your computer's passkey prompt and Full activation if setup requests them. Once the connection and activation are confirmed, the analytics dashboard opens.

The normal same-browser setup does not require copying a connection code or clicking **Check approval**. If you're completing setup in another browser or on another computer, use the setup-code option shown for that destination.

If you only want the seven-day Preview in your browser, you can enable it directly in the extension without installing the desktop app.

## If setup cannot continue

Follow the action shown on the current setup screen. Don't repeat a connection or activation request when its outcome is unconfirmed.

If another application already uses port `17871`, close that application and open Conversation Analytics again.

If Windows cannot provide a hardware-backed device key, the desktop app cannot complete installation setup on that computer.

## Local data and uninstalling

Your data is stored by default in `%LOCALAPPDATA%\OnlyFans Conversational Analytics`.

To uninstall, open **Windows Settings > Apps > Installed apps** and select **OnlyFans Conversational Analytics**. Removing the app leaves the local data folder in place; it does not delete your conversation data.
