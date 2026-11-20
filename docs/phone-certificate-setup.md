# Trust the COMMONS certificate on your phone

> **Only needed in `internal` TLS mode.** With the default plan (`COMMONS_TLS=desec`, Let's Encrypt) phones trust COMMONS automatically and nothing needs installing. Note that Element X does not accept the internal CA even when installed (see `docs/SECURITY.md`).

COMMONS runs on the Outpost's own box, not on the internet, so its HTTPS certificate comes from the box's **own certificate authority** instead of a public one. Your phone doesn't know that authority yet. You install its root certificate **once**; after that the browser trusts COMMONS like any other website.

You need:
- the file `commons-root.crt` (an admin sends it to you, see [For admins](#for-admins)),
- the **fingerprint** to compare, printed on the notice at the Outpost and shown below by the admin.

> The certificate is public and safe to share. Only install it if the fingerprint matches: a wrong certificate could let someone else read your traffic.

## iPhone / iPad

1. **Get the file onto the phone.** AirDrop it from a Mac, or open it as an email attachment in Apple Mail. (Messengers like Signal sometimes don't work: use "Save to Files", then tap it in the Files app.)
   iOS shows *Profile Downloaded*.
   <!-- screenshot: ios-1-profile-downloaded.png -->
2. Open **Settings**. Tap **Profile Downloaded** near the top, then **Install** (enter your passcode), **Install** again, **Done**.
   <!-- screenshot: ios-2-install-profile.png -->
3. **Turn on full trust** (people often miss this step): **Settings → General → About → Certificate Trust Settings**, then switch on **Caddy Local Authority** and confirm.
   <!-- screenshot: ios-3-certificate-trust-settings.png -->
4. Check the fingerprint: **Settings → General → VPN & Device Management → Caddy Local Authority → More Details → SHA-256**. It must match the published one.
5. Open `https://<COMMONS address>` in Safari. It loads without a warning.

**Remove it again:** Settings → General → VPN & Device Management → Caddy Local Authority → Remove Profile.

## Android

Menu names differ between brands (Pixel, Samsung, Xiaomi…). If you can't find a menu, search Settings for **"certificate"**.

1. **Get the file onto the phone** (email, Signal, Nearby Share, USB) and save it, e.g. to **Downloads**. Tapping the file does *not* install it on current Android versions.
2. Open **Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate**.
   <!-- screenshot: android-1-install-certificate.png -->
3. Android warns that your data may be visible to the certificate owner. Tap **Install anyway**, unlock, and pick `commons-root.crt`.
   <!-- screenshot: android-2-warning.png -->
4. Check it: **Encryption & credentials → Trusted credentials → User**. Tap **Caddy Local Authority** and compare the SHA-256 fingerprint.
5. Open `https://<COMMONS address>` in Chrome. It loads without a warning.

**Remove it again:** Encryption & credentials → Trusted credentials → User → Caddy Local Authority → Remove.

> **Known limitation:** Android apps only trust user-installed certificates if the app opts in. The browser works; whether **Element X on Android** does is being tested (issue #5).

## For admins

Export the certificate and its fingerprint from the running stack:

```bash
scripts/caddy_ca.sh commons-root.crt
```

- Publish the SHA-256 fingerprint somewhere independent of how you send the file (the printed notice at the Outpost, this repo's README on the box).
- The certificate stays the same as long as Caddy's `caddy-data` volume survives. **If that volume is lost, Caddy creates a new authority and every member must reinstall.** Back it up (see `docs/SECURITY.md`).
- The root certificate is valid for 10 years. Caddy renews the site certificates itself; nothing to do there.
