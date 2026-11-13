# Installing harvesting-dune

There are three ways to install the tool. The ZIP file is the simplest, and the other two end up working the same way.

## From the ZIP file (recommended)

1. Download `harvesting-dune-<version>.zip` from the [Releases page](https://github.com/sm18lr88/harvesting-dune/releases).
2. Right-click the file and choose **Extract All**. Don't run `Install.cmd` from inside the ZIP.
3. Double-click `Install.cmd` in the extracted folder. You don't need admin rights. The first install takes a few minutes because it downloads Python.
4. When asked, paste your guild key and press Enter. The key stays hidden while you type. The installer checks it with the server, and if the server can't be reached, the key is saved and checked when the detector starts.
5. Close the companion (tray icon > Exit) and open it again.

The tool is installed in `%LOCALAPPDATA%\Programs\harvesting-dune`, so you can delete the ZIP and the extracted folder afterwards. The installer sets up uv (through winget, the uv installer, or a checked direct download), and uv brings its own copy of Python, so any other Python you have installed is left alone. From now on, the detector starts and stops along with the companion.

If something goes wrong, the window stays open, explains how to fix it and shows where the log is. Once you've fixed the problem, run `Install.cmd` again. Running it more than once does no harm.

## With the setup program

1. Download `harvesting-dune-setup-<version>.exe` from the [Releases page](https://github.com/sm18lr88/harvesting-dune/releases) and run it. You don't need admin rights.
2. Leave "Start harvesting-dune together with the gaming.tools companion" ticked.
3. If the companion is open, also tick "Restart the companion now".
4. Wait for the setup window to close. The first install downloads Python.

This also installs the tool in `%LOCALAPPDATA%\Programs\harvesting-dune`. If the setup reports an error, look in `data\install.log` inside that folder.

## From a cloned folder

1. Clone `https://github.com/sm18lr88/harvesting-dune`, or download the source ZIP and extract it, into a folder you're going to keep. Don't use a temporary folder, because the tool runs from this folder every time the companion starts.
2. Open PowerShell in that folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallUv
   ```

   `-InstallUv` installs uv with winget if you don't already have it. If the companion is open, add `-RestartCompanion` as well, because the companion only opens its DevTools port when it starts.
3. Open the companion as usual.

Nothing is installed for other users on the PC. From now on, the detector starts with the companion, opens its settings page at http://127.0.0.1:8765, and stops when you close the companion.

## Updates

If you installed from the ZIP or with the setup program, the tool keeps itself up to date without asking you anything.

- It checks for a new release when you start the game and then every 15 minutes, as long as autorun is on.
- It downloads and checks updates in the background but never installs one while the game is running. The update goes in after you close the game, and the detector restarts, which takes a few seconds. Your data and guild key are kept.
- If an update fails, it puts the previous version back and tries again 24 hours later. The details are in `data\update.log`.
- A copy installed with `git clone` never updates itself.

To update by hand, download the new ZIP, extract it and double-click its `Install.cmd`.

## Uninstalling

- **ZIP:** double-click `Uninstall.cmd`, either in the extracted folder or in `%LOCALAPPDATA%\Programs\harvesting-dune`.
- **Setup program:** remove it under "Installed apps" in Windows Settings. This removes autorun and the program files.
- **Cloned folder:** run the command below in the folder, restart the companion so that it closes the DevTools port, and then delete the folder.

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
  ```

Whichever way you uninstall, everything the tool has learned stays in the `data` folder. Delete that folder too if you want it all gone.
