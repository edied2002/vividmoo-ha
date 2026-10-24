# Vividmoo litter box for Home Assistant

An unofficial cloud integration for Vividmoo (UBTech "airpet") self-cleaning litter boxes, built by
reverse-engineering the Vividmoo Pet Android app (2.0.0). Not affiliated with or endorsed by Vividmoo or UBTech.
Unless marked otherwise, everything below was verified against the live cloud with a real box.

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/eddiedean)

## Features
- **Sensors:** work mode and state (with live job timing), waste-bin level and uses since reset, last use,
  last cat, last weight, last visit duration, visits today, 7-day visit average, cat weight
  (`state_class: measurement`, so long-term statistics graph it), Wi-Fi network and cloud status.
- **Binary sensors:** online, waste bin full, and **cat inside** (pushed by the box in real time on entry and exit).
- **Buttons:** Clean, Level litter, Pause, Resume and Waste bin emptied (resets the cloud's use counter, like the app).
  Commands are byte-identical to the app's and wait for the box's acknowledgement.
- **Live updates (optional, off by default):** the cloud MQTT channel gives real-time state. On every
  (re)connect the integration asks the box for its full state, so the status is never unknown.
- **Dashboard card,** served by the integration itself (see below).

Settings writes (switches, schedules) aren't implemented.

**Tested with:** one box on the **EU** cloud (`https://apis-eu.airrobo-home.com`). Accounts in other regions
probably use a different API URL. It's editable in the setup form; please open an issue with yours if it differs.

## Install
[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=edied2002&repository=vividmoo-ha&category=integration)

**HACS:** click the button above (or add `https://github.com/edied2002/vividmoo-ha` as a custom repository,
category *Integration*), download **Vividmoo Litter Box**, and restart Home Assistant.

**Manual:** copy `custom_components/vividmoo` into your `config/custom_components/` and restart.

Then add the integration:

[![Open your Home Assistant instance and start setting up the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vividmoo)

or go to Settings → Devices & services → Add integration → **Vividmoo Litter Box**. You'll need your account and
password, plus the app's **App ID** and **App Key** (next section). Account type, password encoding and API
URL are pre-filled with the values the app uses.

## Getting the App ID and App Key
Every request to the cloud is signed with `X-UBT-Sign = md5(timestamp + AppKey + nonce + deviceId)`. The
App ID and App Key belong to the official app, so they aren't included here. In the app they're protected
by Ijiami (GameVMP), but they're plain strings at runtime. Two ways to read them:

- **Frida** (rooted Android emulator), with the script in `tools/`:
  ```sh
  adb install-multiple base.apk split_*.apk    # the split APKs carry the native libraries the app needs
  frida -U -f com.vividmoo.vividmoopet -l tools/vividmoo_hooks.js
  ```
  Log in. The `[keys]` lines show the App ID and App Key, and `statics()` re-reads them later. If the
  protection kills the app under Frida, attach after launch (`frida -U -n "Vividmoo Pet" ...`); `statics()`
  still works.
- **A logging build:** decompile the app with apktool, add a `Log.d` where the sign input is built
  (the class that MD5s `ts + key + nonce + deviceId`), rebuild, and sign the base and all split APKs with
  the same key. The key is the 32-hex-character block after the 13-digit timestamp.

## Use a separate account (recommended)
The cloud allows one active login per account: when the official app logs in again, it revokes the
integration's session (and vice versa). Create a second Vividmoo account, share the box to it from your main
account in the app, and use the second account in Home Assistant. A shared account can read everything and
send commands. If the token still keeps getting revoked, the integration backs off (5 → 10 → … → 60 min)
instead of logging straight back in. With live updates on, a kicked MQTT session waits 1 hour before retrying.

## Dashboard card
The card registers itself, so there's no HACS frontend or resource setup. Add a card:
```yaml
type: custom:vividmoo-card
device_id: <the litter box device>   # or pick it in the visual editor
```
It shows:
- **A status header:** "Cat inside" while the cat is in the box. During a job it shows a drum with a progress
  ring timed from the box's real start, pause and finish.
- **The waste-bin gauge.**
- **Visits:** the last visit (cat · weight · duration), "last seen", visits today and the 7-day average.
- **Buttons:** Clean, Level, Pause/Resume, and Bin emptied.

Tap any value to open its history.

If the Android companion app says "Custom element doesn't exist", it's probably showing a cached page:
clear the app's frontend cache and update *Android System WebView*. As a last resort, add a resource
`/vividmoo/vividmoo-card.js` (type *JavaScript module*).

## Development
```sh
pip install pytest-homeassistant-custom-component 'protobuf>=4.21' 'paho-mqtt>=1.6.1'
pytest tests                              # integration, protocol and REST client tests
node --test tests/frontend/card.test.mjs  # card tests
```
`tools/live_check.py` runs the read-only calls against the real cloud. It takes `account=`, `password=`,
`app_id=` and `app_key=` from `~/.config/vividmoo/credentials` (or `VIVIDMOO_*` environment variables),
and masks tokens and serial numbers in its output. `VIVIDMOO_MQTT_SECONDS=0` skips MQTT.

Layout:
- **`protocol/`:** signing, RISP framing (CRC-8/I-432-1), and the IM envelope. Its message classes are built
  at runtime from the protobuf descriptors in the app, so there's no protoc step. It has no Home Assistant
  imports.
- **`api.py`:** the REST client.
- **`mqtt.py`:** the IM/MQTT session (TLS, port 21000).
- **`coordinator.py`:** polling, push handling and commands.
- **`frontend/vividmoo-card.js`:** the card, a classic script that also loads as a module.

## Support
If this integration is useful to you, you can [buy me a coffee on Ko-fi](https://ko-fi.com/eddiedean). ☕

## License
MIT
