# Net LED Monitor

A sleek, always-on-top network activity indicator widget that mirrors incoming (download) and outgoing (upload) network activity.

## Features

*   **Compact UI:** Displays as a small, minimal grey circle that stays always on top.
*   **Network Activity Sync:** 
    *   **Soft Cyan Glow** represents network **Downloads** (bytes received).
    *   **Soft Magenta Glow** represents network **Uploads** (bytes sent).
*   **Dual Opacity States:**
    *   **50% Opacity** (default) during idle network states.
    *   **20% Opacity** during active network operations (remains completely non-obtrusive and semi-transparent while you work).
*   **Audio Feedback:**
    *   Plays faint, short audio tones to represent activity.
    *   Uses **700 Hz** for Downloads.
    *   Uses **1200 Hz** for Uploads.
    *   Includes automatic audio throttling to prevent overlapping noise or speaker spam.
*   **Full Context Menu:** Right-click the circle or the system tray icon to toggle:
    *   **Start / Stop:** Start or halt network monitoring.
    *   **Lock Position:** Lock/unlock the widget's position.
    *   **Click Through:** Makes the widget click-transparent (input goes to windows underneath).
    *   **Sound On/Off:** Enable/disable tone beeps.
    *   **Exit:** Safely exit the application.
*   **Quick Interaction Shortcuts:**
    *   **Drag & Move:** Left-click and drag the circle anywhere on your screens (when position lock is disabled).

---

## Files in this Repository

*   [netled.py](file:///e:/workspace/netled/netled.py): The complete Python source code using PyQt5 and `psutil`.
*   [netled.exe](file:///e:/workspace/netled/netled.exe): The compiled, portable, standalone executable.
