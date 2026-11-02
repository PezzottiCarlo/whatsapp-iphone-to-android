# WhatsApp Transfer: iPhone → Android

A Windows wizard that moves WhatsApp chats from an iPhone to any Android phone through a PC, without root. It explains every step you do on the phones and handles the conversion and copying itself.

> [!WARNING]
> **Still early.** It has been through one full transfer with real phones so far: 323,781 messages were converted, copied and restored, and everything works. Right after the restore, though, WhatsApp took a while to recover: it could not send messages, and received messages showed up in notifications but not in the chat. Waiting and then restarting the phone fixed it. If it happens to you, do the same, and don't delete WhatsApp from the iPhone until sending and receiving work. Reports are welcome, see [Reporting a problem](#reporting-a-problem).

If your Android phone is new or can be restored to factory settings, use WhatsApp's official transfer instead. It runs during the phone's first setup, with a USB-C to Lightning cable.

## Download

Get `WhatsAppTransfer.exe` from the [latest release](https://github.com/PezzottiCarlo/whatsapp-iphone-to-android/releases/latest), copy it to the PC and open it. Windows may show "Windows protected your PC" because the exe isn't signed: click *More info → Run anyway*.

You'll need:

- Windows 10 or 11
- Apple Devices or iTunes, to make a local iPhone backup (iCloud backups won't work)
- both phones with their cables, and the SIM to receive WhatsApp's verification SMS
- Internet on the PC: `adb` is downloaded from Google on first use
- free space on the PC and the phone, at least as much as your WhatsApp photos and videos

## How it works

1. It reads the local iPhone backup without changing it.
2. It takes a real backup from the Android phone, made by WhatsApp with the 64-digit key.
3. It adds the iPhone messages to that database and prepares the media with the names Android uses.
4. It encrypts the backup again with the same key and header.
5. It saves the existing WhatsApp files to the PC, resets WhatsApp on the phone and copies everything into place.
6. WhatsApp finds the backup and restores it from its normal screen.

Starting from the phone's real database means the schema and header always match the installed WhatsApp version. Columns are read at runtime.

**Transferred:** individual and group chats, texts, links, starred messages, replies, photos, videos, audio, voice notes, documents, GIFs, stickers, contacts and locations.

**Not transferred:** reactions, polls, view-once media, call history, status updates, system notices, wallpapers and per-chat settings.

**Not supported:** WhatsApp Business, and a second copy of WhatsApp in a dual-app or work profile.

## What has been tested

| Area | Status |
|---|---|
| crypt15 encryption | Tested on a real WhatsApp 2.26 backup: decrypts, refuses wrong keys, rebuilds byte for byte |
| Conversion | Tested on the real WhatsApp 2.26 schema with a **synthetic** iPhone backup |
| Phone commands | Tested on an Android 16 emulator: copy, permissions, reset, resume, undo, and a test app reading the files |
| Restore in real WhatsApp | Done once, with version 1.1 on a Nothing Phone: it works, after waiting and restarting the phone as described above |
| Real iPhone backup | Read once, in the same transfer, with no errors |
| **Version 2.0 on real phones** | **Not tried yet.** It adds the English interface and support for more brands. The conversion logic is the same as in 1.1 |
| **Password-protected iPhone backup** | Only with a simulated library |
| **Other Android brands** | **Not tested.** The wizard has instructions for the main brands, but they haven't been tried on those phones |
| **Android 10 and older** | **Only simulated.** The older `/sdcard/WhatsApp` folder is detected, but this hasn't been run on a real device |

Direct writing of texts into the database has been tested by others on a real phone with WhatsApp 2.26 (see Acknowledgements). Media and the root-free restore have no such confirmation.

The meaning of the `message.lookup_tables` column isn't documented. The program reuses the values it finds on the phone, which is why it asks you to send yourself a test photo and reply.

## Reporting a problem

The wizard's last page can put the phone back as it was and save an error report. The report holds technical counts and error messages, with no message content, phone numbers or keys. Please read it before attaching it to an [issue](https://github.com/PezzottiCarlo/whatsapp-iphone-to-android/issues), and include your phone's brand, its Android version and your WhatsApp version.

Files stay in `Documents\WhatsApp Transfer` unless you chose another folder. That folder holds **unencrypted copies of your chats**: never share it, and delete the copies from the wizard's last page when you're done.

## Development

```powershell
.\build.ps1                                   # installs, runs the tests, creates the exe
.\.venv\Scripts\python.exe -m pytest tests    # tests only
.\.venv\Scripts\python.exe app.py --page 3    # opens a specific page
```

- `wa_transfer/crypt15.py`: Android backup format
- `wa_transfer/ios_backup.py`: reading iPhone backups
- `wa_transfer/converter.py`: merging the databases and preparing the media
- `wa_transfer/android.py`: adb commands
- `wa_transfer/session.py`: the transfer operations
- `wa_transfer/diagnosis.py`: database measurements for the error report (counts only)
- `wa_transfer/gui.py`: the wizard

## Acknowledgements

- The Android database rules (`jid_map`, `jid.type`, `sort_id`) come from [wa-ios-to-android](https://github.com/divyarthjain/wa-ios-to-android) (MIT).
- The crypt15 format was reconstructed by reading [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) (GPL-3.0). The files in `tests/fixtures` are that project's test samples.
- The iOS data mapping is based on [Whatsapp-Chat-Exporter](https://github.com/KnugiHK/Whatsapp-Chat-Exporter) (MIT).
- Password-protected iPhone backups use [iphone_backup_decrypt](https://github.com/jsharkey13/iphone_backup_decrypt) (MIT).

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

GPL-3.0, see [LICENSE](LICENSE). Copyright © 2026 Carlo Pezzotti.

This is an unofficial project, not affiliated with WhatsApp or Meta. Use it only with your own account and your own phones. It comes with no warranty.
