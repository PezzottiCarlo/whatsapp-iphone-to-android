# Third-party notices

This project as a whole is licensed under GPL-3.0 (see [LICENSE](LICENSE)). It uses or draws on the following work.

| Project | License | How it is used |
|---|---|---|
| [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) | GPL-3.0 | Reference for the crypt15 format. `tests/fixtures/encrypted_backup.key` and `tests/fixtures/msgstore-2.26.db.crypt15` are unmodified copies of its public test samples, used only by the tests and not included in the exe |
| [wa-ios-to-android](https://github.com/divyarthjain/wa-ios-to-android) | MIT | Reference for the Android database rules |
| [Whatsapp-Chat-Exporter](https://github.com/KnugiHK/Whatsapp-Chat-Exporter) | MIT | Reference for the iOS data mapping |
| [iphone_backup_decrypt](https://github.com/jsharkey13/iphone_backup_decrypt) | MIT | Runtime dependency, bundled in the exe |
| [PyCryptodome (pycryptodomex)](https://github.com/Legrandin/pycryptodome) | BSD 2-Clause and Public Domain | Runtime dependency, bundled in the exe |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0-or-later with a bootloader exception | Builds the exe |
| [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) | Google's terms | `adb` is downloaded from Google on first use and is not redistributed here |

WhatsApp is a trademark of WhatsApp LLC. iPhone is a trademark of Apple Inc. Android is a trademark of Google LLC. This project isn't affiliated with or endorsed by any of them.
