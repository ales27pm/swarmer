# Emulator QA Runbook

Même si iPhone est cible principale, Android emulator peut valider rapidement l'UI React Native.

## Start

```bash
adb devices
cd mobile
npx expo run:android
```

## Launch app

```bash
adb shell cmd package resolve-activity --brief org.ales27pm.mongars
adb shell am start -n org.ales27pm.mongars/.MainActivity
```

## Dump UI tree

```bash
adb exec-out uiautomator dump /dev/tty > /tmp/mongars-ui.xml
```

## Screenshot

```bash
adb exec-out screencap -p > /tmp/mongars.png
```

## Logs

```bash
adb logcat -c
adb logcat -d > /tmp/mongars-logcat.txt
```

## Smoke path

1. Launch app.
2. Open Settings.
3. Enter server URL.
4. Pair device.
5. Return Chat.
6. Send test message.
7. Open Tasks.
8. Open Approvals.
9. Verify no crash.

## Failure artifacts

Attach:

- screenshot;
- UI tree;
- logcat;
- repro steps;
- app version/build.
