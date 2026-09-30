# Network Troubleshooter — Beta User Guide

The Network Troubleshooter walks support agents through a selected network troubleshooting workflow. Use the copy provided in the team SharePoint location.

## Download and start

1. In SharePoint, download the **NetworkTroubleshooter** ZIP file provided for beta testing.
2. In File Explorer, right-click the downloaded ZIP file and select **Extract All**. Choose a folder on your computer that you can write to. Do not run the program from inside the ZIP file.
3. Open the extracted **NetworkTroubleshooter** folder. It should contain:
   - `NetworkTroubleshooter.exe`
   - `Network_yes_no.db`
   - The `_internal` folder (if included in the download)
4. Double-click `NetworkTroubleshooter.exe`. Keep the database and any `_internal` folder in the same extracted folder as the executable.

You do not need to install Python to run the packaged program.

## Use a workflow

1. Select a workflow from the numbered menu.
2. Enter the ticket or case number and any requested device, interface, or IP information.
3. Follow the instructions shown for each step. The program displays commands for you to use in the appropriate system; check the device and command before using them.
4. Record what you found and answer each decision question based on the result you observed.
5. At the end, review the summary displayed by the program. Copy any relevant findings into the case according to your team's process.

Follow the navigation choices shown on screen to go back, skip a step where allowed, or quit a workflow. If Q returns you to the workflow menu, select another workflow or use the menu's exit option to close the program.

## Keep your working copy together

Run your own extracted copy from your computer. The program may write session information to `Network_yes_no.db` and create a `troubleshooting_summaries` folder beside the executable. Keep these files together when returning to an unfinished beta test, and do not run a shared SQLite database directly from a SharePoint-synced team folder.

If you receive **Database not found**, check that `Network_yes_no.db` is in the same extracted folder as `NetworkTroubleshooter.exe`. If the executable does not start, confirm that the entire ZIP was extracted, including `_internal`, and contact the beta test owner with the exact error message.
