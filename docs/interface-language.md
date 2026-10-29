# Interface language

*Moved from the README (Interface Language). The text is unchanged except for relative links.*

The interface — the app window, its dialogs and notifications, and the phone
page — is in English or Chinese.

- A new install follows the system language: on macOS, the language set for
  this app in System Settings, otherwise the preferred language; on Windows,
  the display language. A Chinese system language (Simplified or Traditional)
  gives the Chinese interface, anything else English, and Chinese if it
  cannot be detected.
- On a new install, if Chinese is anywhere in the macOS preferred-language
  list but isn't first (for example English, then Simplified Chinese), the
  interface is set to Chinese instead of following the system.
- An install that was already in use before the English interface existed
  stays in Chinese after updating.
- To switch, click 中文 or English at the top right of the home page, above
  the title. Or open **Settings → App Language** on the home page and choose
  System, 中文 or English. The window reloads once. Settings is hidden during
  a session, so switch between sessions.
- `python3 main.py --ui-lang en` (or `zh`) sets the language for one launch
  and saves nothing.
- On a new install with an English interface, captions are translated into
  English until you choose another language in the Captions in menu in the
  top bar.
- The Captions in menu sets only the language captions are translated into
  (Simplified Chinese, Traditional Chinese, English or Spanish). It doesn't
  change the interface, and App Language doesn't change the captions.
- Phone Viewing pages follow each phone's own language, with a toggle at the
  bottom of the page.
- The app name in the Dock, the menu bar and ⌘Tab follows the macOS system
  language (TikTok 直播同传 on a Chinese system). The file is always
  `TikTok Live Translator.app`.
- Terminal output, logs and the audit log are always in Chinese, as is the
  text `Start.command` and `Start.bat` print in their terminal window. The
  launchers' error dialogs, which appear before the app can read the
  language setting, show Chinese and English together. Streamer names,
  glossaries, captions and comments are data; the interface language never
  changes them.

Notes for maintainers: [docs/i18n.md](i18n.md) (in Chinese).
