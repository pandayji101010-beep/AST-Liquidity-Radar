# AST Liquidity Radar Android wrapper

This app is a thin Android WebView wrapper around the existing live dashboard:

- URL: https://ast-liquidity-radar.onrender.com/
- Android package: com.ast.liquidityradar.debug
- Version: 1.0.0-debug

The website HTML, CSS, JavaScript, scanner, API, and Render backend are not copied or rewritten. The app loads the current website over HTTPS so the page continues to receive the same live updates as the browser version.

## Build

The repository workflow `.github/workflows/build-android-apk.yml` builds an installable debug APK using Java 17 and Gradle 8.9. It uploads the APK as a GitHub Actions artifact and publishes a GitHub Release for each successful main-branch build.

The APK is debug-signed by the Android Gradle plugin and is suitable for direct installation/testing. A separately managed private signing key would be needed for a Play Store release.

## Requirements

- Android 6.0 (API 23) or newer.
- Internet connection to load the live dashboard.
- Android System WebView or Chrome updated on the phone.

The APK installs the app shell on the phone; the live website and market data remain hosted on Render.
