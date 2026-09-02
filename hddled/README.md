# HDD LED Monitor

A sleek, premium, always-on-top disk activity indicator widget that mirrors the physical motherboard HDD LED jumper signals.

## Features

*   **Compact UI:** Displays as a small, minimal grey circle that stays always on top.
*   **Disk Activity Sync:** 
    *   **Soft Red Glow** represents disk **Read** operations.
    *   **Soft Green Glow** represents disk **Write** operations.
*   **Dual Opacity States:**
    *   **50% Opacity** (default) during system idle states.
    *   **20% Opacity** during active disk activity (ensures non-obtrusive, transparent visual monitoring while working).
*   **Audio Feedback:**
    *   Plays faint, short audio tones to represent activity.
    *   Uses **600 Hz** for disk Reads.
    *   Uses **1100 Hz** for disk Writes.
    *   Includes automatic audio throttling to prevent overlapping noise or speaker spam.
*   **Full Context Menu:** Right-click the circle or the system tray icon to toggle:
    *   **Start / Stop:** Start or halt disk monitoring.
    *   **Lock Position:** Lock/unlock the widget's position.
    *   **Click Through:** Makes the widget click-transparent (input goes to windows underneath).
    *   **Sound On/Off:** Enable/disable tone beeps.
    *   **Exit:** Safely exit the application.
*   **Quick Interaction Shortcuts:**
    *   **Drag & Move:** Left-click and drag the circle anywhere on your screens (when position lock is disabled).

---

## Files in this Repository

*   [hddled.py](file:///e:/workspace/hddled/hddled.py): The complete Python source code using PyQt5 and `psutil`.
*   [hddled.exe](file:///e:/workspace/hddled/hddled.exe): The compiled, portable, standalone executable.
